"""Project documentation generator.

GitHub repo -> shallow clone -> collect facts (structure, languages, dependencies, entry points, API routes,
env vars, tests, CI) -> plain-language write-up (Claude if a key is set, rule-based otherwise) -> Markdown / HTML.
"""
import ast, json, os, re, shutil, stat, subprocess, tempfile, time
from collections import Counter
from html import escape as esc
from pathlib import Path

import github
from analyzer import AnalysisError, SKIP, parse_repo, run

IGN = SKIP | {"vendor", "target", ".idea", ".vscode", ".next", "coverage", ".mypy_cache", ".pytest_cache", "obj", "out"}
MAX_FILES, MAX_BYTES = 8000, 1_500_000
LANGS = {".py": "Python", ".js": "JavaScript", ".jsx": "JavaScript", ".ts": "TypeScript", ".tsx": "TypeScript", ".java": "Java",
         ".go": "Go", ".rs": "Rust", ".rb": "Ruby", ".php": "PHP", ".c": "C", ".h": "C", ".cpp": "C++", ".cc": "C++", ".hpp": "C++",
         ".cs": "C#", ".kt": "Kotlin", ".swift": "Swift", ".scala": "Scala", ".sh": "Shell", ".html": "HTML", ".css": "CSS",
         ".scss": "SCSS", ".vue": "Vue", ".dart": "Dart", ".r": "R", ".sql": "SQL", ".lua": "Lua", ".ipynb": "Jupyter Notebook"}
FRAMEWORKS = {"fastapi": "FastAPI", "flask": "Flask", "django": "Django", "starlette": "Starlette", "tornado": "Tornado", "streamlit": "Streamlit",
              "pytorch": "PyTorch", "torch": "PyTorch", "tensorflow": "TensorFlow", "scikit-learn": "scikit-learn", "pandas": "pandas", "numpy": "NumPy",
              "sqlalchemy": "SQLAlchemy", "celery": "Celery", "pydantic": "Pydantic", "anthropic": "Anthropic SDK", "openai": "OpenAI SDK",
              "langchain": "LangChain", "pytest": "pytest", "express": "Express", "react": "React", "next": "Next.js", "vue": "Vue",
              "angular": "Angular", "svelte": "Svelte", "nestjs": "NestJS", "@nestjs/core": "NestJS", "jest": "Jest", "mocha": "Mocha",
              "vitest": "Vitest", "tailwindcss": "Tailwind CSS", "typescript": "TypeScript", "webpack": "webpack", "vite": "Vite",
              "mongoose": "Mongoose", "prisma": "Prisma", "gin-gonic/gin": "Gin", "spring-boot": "Spring Boot", "rails": "Ruby on Rails",
              "laravel/framework": "Laravel", "actix-web": "Actix Web", "tokio": "Tokio", "uvicorn": "Uvicorn", "gunicorn": "Gunicorn"}
DIR_HINTS = {"src": "Main source code", "app": "Application code", "lib": "Library code", "api": "API layer", "tests": "Automated tests",
             "test": "Automated tests", "docs": "Documentation", "doc": "Documentation", "static": "Static front-end assets", "public": "Public/static assets",
             "templates": "HTML templates", "scripts": "Helper scripts", "config": "Configuration", "configs": "Configuration", "models": "Data models",
             "utils": "Shared utilities", "helpers": "Shared utilities", "services": "Service/business logic", "controllers": "Request handlers",
             "routes": "Route definitions", "components": "UI components", "pages": "UI pages", "assets": "Images and assets", "migrations": "Database migrations",
             "examples": "Usage examples", "benchmarks": "Benchmarks", "cmd": "Command-line entry points", "pkg": "Reusable packages", "internal": "Internal packages",
             "data": "Data files", "notebooks": "Notebooks", ".github": "GitHub workflows and templates", "docker": "Docker setup", "deploy": "Deployment files"}
ENTRY = ["main.py", "app.py", "manage.py", "wsgi.py", "asgi.py", "run.py", "server.py", "cli.py", "__main__.py", "index.js", "server.js", "app.js",
         "main.js", "index.ts", "main.ts", "server.ts", "main.go", "main.rs", "Main.java", "Application.java", "Program.cs", "index.php"]
MANIFESTS = ["requirements.txt", "pyproject.toml", "setup.py", "Pipfile", "package.json", "go.mod", "Cargo.toml", "pom.xml", "build.gradle",
             "Gemfile", "composer.json"]


# ───────────────────────────── fact collection ─────────────────────────────

def _rm(root):
    def fix(fn, path, _):  # Windows: git pack files are read-only
        os.chmod(path, stat.S_IWRITE)
        fn(path)
    shutil.rmtree(root, onerror=fix)


def shallow_clone(owner, name):
    tmp = tempfile.mkdtemp(prefix="aica_docs_")
    tok = os.getenv("GITHUB_TOKEN")
    auth = f"x-access-token:{tok}@" if tok else ""
    try:
        run(["git", "clone", "--quiet", "--depth", "1", f"https://{auth}github.com/{owner}/{name}.git", tmp + "/repo"], timeout=300)
    except AnalysisError as e:
        _rm(tmp)
        if any(k in str(e).lower() for k in ("not found", "authentication", "could not read", "terminal prompts")):
            raise AnalysisError(f"Cannot access {owner}/{name}. Check the URL, or set GITHUB_TOKEN for private repos.", 404)
        raise
    return tmp, Path(tmp) / "repo"


def scan(root):
    files = []
    for dp, dn, fn in os.walk(root):
        dn[:] = sorted(d for d in dn if d not in IGN and not d.endswith(".egg-info"))
        for f in sorted(fn):
            files.append((Path(dp) / f).relative_to(root).as_posix())
            if len(files) >= MAX_FILES:
                return files
    return files


def read(root, rel, limit=20000):
    try:
        if (root / rel).stat().st_size > MAX_BYTES:
            return ""
        return (root / rel).read_text("utf-8", "ignore")[:limit]
    except OSError:
        return ""


def languages(root, files):
    files_by, lines_by = Counter(), Counter()
    for f in files:
        lang = LANGS.get(Path(f).suffix.lower())
        if not lang or lang == "Jupyter Notebook":
            continue
        try:
            p = root / f
            if p.stat().st_size > MAX_BYTES:
                continue
            files_by[lang] += 1
            lines_by[lang] += p.read_bytes().count(b"\n") + 1
        except OSError:
            continue
    total = sum(lines_by.values()) or 1
    return [dict(name=k, files=files_by[k], lines=v, pct=round(100 * v / total, 1)) for k, v in lines_by.most_common(8)]


def tree(files, depth=2, cap=60):
    top = {}
    for f in files:
        d = top
        for part in f.split("/")[:-1]:
            d = d.setdefault(part, {})
        d.setdefault("\0f", []).append(f.split("/")[-1])
    out = []

    def render(d, prefix, left):
        entries = [(k, True) for k in sorted(k for k in d if k != "\0f")] + [(f, False) for f in d.get("\0f", [])]
        for i, (n, isdir) in enumerate(entries):
            if len(out) >= cap:
                if len(out) == cap:
                    out.append(prefix + "…")
                return
            last = i == len(entries) - 1
            out.append(f"{prefix}{'└── ' if last else '├── '}{n}{'/' if isdir else ''}")
            if isdir and left > 1:
                render(d[n], prefix + ("    " if last else "│   "), left - 1)
    render(top, "", depth)
    return "\n".join(out)


def parse_readme(text):
    """Title, intro paragraph and feature bullets from a README."""
    title, intro, feats, section = "", "", [], ""
    para = []
    for raw in text.splitlines():
        line = raw.strip()
        m = re.match(r"^(#{1,6})\s+(.*)", line)
        if m:
            if not title and len(m[1]) == 1:
                title = re.sub(r"[*_`]", "", m[2]).strip()
            section = m[2].lower()
            if para and not intro:
                intro = " ".join(para)
            para = []
            continue
        if not intro and line and not line.startswith(("![", "[![", "<", "|", "```", ">", "---", "===")):
            if not re.match(r"^[-*+]\s", line):
                para.append(re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", line))
        elif not line and para and not intro:
            intro = " ".join(para)
        if re.search(r"feature|highlight|capabilit|what it does|functionalit", section) and re.match(r"^[-*+]\s+", line):
            feats.append(re.sub(r"[*_`]", "", re.sub(r"^[-*+]\s+", "", line))[:200])
    intro = intro or " ".join(para)
    return title, re.sub(r"\s+", " ", intro)[:600], feats[:10]


def parse_deps(root, files):
    """-> (deps {manifest: [names]}, scripts {name: cmd}, meta {name, description, version})"""
    deps, scripts, meta = {}, {}, {}
    names = {Path(f).name: f for f in files if f.count("/") == 0}
    for fn in ("requirements.txt",):
        if fn in names:
            deps[fn] = [n for n in (re.split(r"[<>=!~\[;\s#]", l.strip(), 1)[0] for l in read(root, fn).splitlines())
                        if n and not n.startswith("-")][:60]
    for fn in ("pyproject.toml", "Cargo.toml"):
        if fn in names:
            try:
                import tomllib
                t = tomllib.loads(read(root, fn, 200000))
                proj = t.get("project") or t.get("package") or t.get("tool", {}).get("poetry", {}) or {}
                meta.setdefault("name", proj.get("name")); meta.setdefault("description", proj.get("description")); meta.setdefault("version", proj.get("version"))
                d = [re.split(r"[<>=!~\[;\s]", x, 1)[0] for x in proj.get("dependencies", []) if isinstance(x, str)] if isinstance(proj.get("dependencies"), list) else []
                d += list(t.get("dependencies", {}) or {}) + list(t.get("tool", {}).get("poetry", {}).get("dependencies", {}) or {})
                deps[fn] = [x for x in dict.fromkeys(d) if x != "python"][:60]
                if t.get("project", {}).get("scripts"):
                    scripts.update({k: v for k, v in t["project"]["scripts"].items()})
            except Exception:
                pass
    if "package.json" in names:
        try:
            j = json.loads(read(root, "package.json", 200000))
            meta.update({k: j.get(k) for k in ("name", "description", "version") if j.get(k)})
            deps["package.json"] = list(j.get("dependencies", {}))[:60]
            if j.get("devDependencies"):
                deps["package.json (dev)"] = list(j["devDependencies"])[:40]
            scripts.update(j.get("scripts", {}))
        except ValueError:
            pass
    if "go.mod" in names:
        t = read(root, "go.mod")
        m = re.search(r"^module\s+(\S+)", t, re.M)
        if m:
            meta.setdefault("name", m[1].split("/")[-1])
        deps["go.mod"] = re.findall(r"^\s*([\w.\-]+\.[\w.\-]+/[\w./\-]+)\s+v", t, re.M)[:50]
    if "Gemfile" in names:
        deps["Gemfile"] = re.findall(r"^\s*gem\s+['\"]([^'\"]+)", read(root, "Gemfile"), re.M)[:50]
    if "composer.json" in names:
        try:
            deps["composer.json"] = list(json.loads(read(root, "composer.json", 100000)).get("require", {}))[:50]
        except ValueError:
            pass
    if "pom.xml" in names:
        deps["pom.xml"] = list(dict.fromkeys(re.findall(r"<artifactId>([^<]+)</artifactId>", read(root, "pom.xml", 100000))))[:40]
    return deps, scripts, {k: v for k, v in meta.items() if v}


def py_info(root, files):
    """Module docstrings, top-level classes/functions and HTTP routes for Python files."""
    mods, routes = [], []
    http = {"get", "post", "put", "delete", "patch", "route", "head", "options"}
    for f in files:
        if not f.endswith(".py") or is_test(f):
            continue
        try:
            tree_ = ast.parse(read(root, f, 400000))
        except (SyntaxError, ValueError):
            continue
        doc = (ast.get_docstring(tree_) or "").strip().splitlines()
        cls = [n.name for n in tree_.body if isinstance(n, ast.ClassDef)]
        fns = [n.name for n in tree_.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and not n.name.startswith("_")]
        for n in ast.walk(tree_):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for dec in n.decorator_list:
                    c = dec.func if isinstance(dec, ast.Call) else dec
                    if isinstance(dec, ast.Call) and isinstance(c, ast.Attribute) and c.attr in http and dec.args \
                            and isinstance(dec.args[0], ast.Constant) and isinstance(dec.args[0].value, str):
                        routes.append(dict(method="ANY" if c.attr == "route" else c.attr.upper(), path=dec.args[0].value, file=f, handler=n.name))
        if doc or cls or fns:
            mods.append(dict(path=f, doc=doc[0][:160] if doc else "", classes=cls[:6], functions=fns[:8]))
    return mods[:40], routes


def is_test(p):
    path = Path(p)
    n = path.name
    return n.startswith("test_") or n.endswith(("_test.py", "_test.go")) or ".test." in n or ".spec." in n \
        or n == "conftest.py" or bool({"tests", "test", "__tests__", "spec"} & set(path.parts))


def js_routes(root, files):
    out = []
    rx = re.compile(r"\b(?:app|router|server|api)\.(get|post|put|delete|patch)\(\s*['\"`]([^'\"`]+)['\"`]")
    for f in files:
        if Path(f).suffix in (".js", ".ts", ".mjs", ".cjs") and not is_test(f):
            for m in rx.finditer(read(root, f, 200000)):
                out.append(dict(method=m[1].upper(), path=m[2], file=f, handler=""))
    return out


def env_vars(root, files):
    found = {}
    pats = [r"os\.getenv\(\s*['\"](\w+)", r"os\.environ(?:\.get)?\s*[\[(]\s*['\"](\w+)", r"process\.env\.([A-Z_][A-Z0-9_]*)",
            r"os\.Getenv\(\"(\w+)", r"env::var\(\"(\w+)", r"System\.getenv\(\"(\w+)"]
    for f in files:
        if Path(f).suffix.lower() in LANGS and not is_test(f):
            text = read(root, f, 200000)
            for p in pats:
                for m in re.finditer(p, text):
                    found.setdefault(m[1], f)
    for ex in (".env.example", ".env.sample", "example.env"):
        if ex in files:
            for l in read(root, ex).splitlines():
                m = re.match(r"^\s*([A-Z][A-Z0-9_]+)\s*=", l)
                if m:
                    found.setdefault(m[1], ex)
    return [dict(name=k, where=v) for k, v in sorted(found.items())][:30]


def commands(files, deps, scripts, entry, fw):
    names = {f for f in files if "/" not in f}
    flat = {d.lower() for v in deps.values() for d in v}
    inst, runc, test = [], [], []
    if "requirements.txt" in names:
        inst.append("pip install -r requirements.txt")
    elif "pyproject.toml" in names or "setup.py" in names:
        inst.append("pip install .")
    if "package.json" in names:
        inst.append("npm install")
        for k in ("dev", "start"):
            if k in scripts:
                runc.append(f"npm run {k}" if k != "start" else "npm start")
                break
        if "build" in scripts:
            runc.append("npm run build")
        if "test" in scripts:
            test.append("npm test")
    if "go.mod" in names:
        inst.append("go mod download"); runc.append("go run ."); test.append("go test ./...")
    if "Cargo.toml" in names:
        inst.append("cargo build"); runc.append("cargo run"); test.append("cargo test")
    if "pom.xml" in names:
        inst.append("mvn install"); test.append("mvn test")
    if "Gemfile" in names:
        inst.append("bundle install")
    if "composer.json" in names:
        inst.append("composer install")
    pys = [e for e in entry if e.endswith(".py")]
    if pys and not runc:
        e = pys[0]
        mod = e[:-3].replace("/", ".")
        if "fastapi" in flat or "starlette" in flat:
            runc.append(f"uvicorn {mod}:app --reload")
        elif "django" in flat or e.endswith("manage.py"):
            runc.append("python manage.py runserver")
        elif "streamlit" in flat:
            runc.append(f"streamlit run {e}")
        elif "flask" in flat:
            runc.append(f"flask --app {mod} run")
        else:
            runc.append(f"python {e}")
    if any(f.endswith(".py") and is_test(f) for f in files):
        test.append("pytest")
    if "Dockerfile" in names:
        runc.append("docker build -t app . && docker run --rm -p 8000:8000 app")
    return list(dict.fromkeys(inst)), list(dict.fromkeys(runc)), list(dict.fromkeys(test))


def collect(root, owner, name, gh=None):
    gh = gh or {}
    files = scan(root)
    deps, scripts, meta = parse_deps(root, files)
    langs = languages(root, files)
    rtext = ""
    for cand in ("README.md", "README.rst", "README.txt", "README", "readme.md"):
        if cand in files:
            rtext = read(root, cand, 12000)
            break
    title, intro, feats = parse_readme(rtext)
    mods, routes = py_info(root, files)
    routes = (routes + js_routes(root, files))[:40]
    entry = [f for f in files if Path(f).name in ENTRY and f.count("/") <= 2][:6]
    flat = {d.lower() for v in deps.values() for d in v}
    fw = list(dict.fromkeys(v for k, v in FRAMEWORKS.items() if k in flat))[:12]
    inst, runc, test = commands(files, deps, scripts, entry, fw)
    top = Counter(f.split("/")[0] for f in files if "/" in f)
    ci = [f for f in files if f.startswith(".github/workflows/")] + [f for f in files if f in (".gitlab-ci.yml", "Jenkinsfile", ".travis.yml", "azure-pipelines.yml")]
    infra = [f for f in files if f in ("Dockerfile", "docker-compose.yml", "docker-compose.yaml", "Makefile", "Procfile", "vercel.json", "netlify.toml", "render.yaml", "fly.toml", "serverless.yml")]
    lic = (gh.get("license") or {}).get("spdx_id") if isinstance(gh.get("license"), dict) else None
    if not lic or lic == "NOASSERTION":
        lf = next((f for f in files if re.match(r"(?i)^licen[sc]e(\.\w+)?$", f)), None)
        t = read(root, lf, 600).lower() if lf else ""
        lic = next((n for k, n in (("mit license", "MIT"), ("apache license", "Apache-2.0"), ("gnu general public", "GPL"), ("bsd", "BSD"), ("redistribution and use in source and binary", "BSD"), ("mozilla public", "MPL-2.0")) if k in t), "See LICENSE file" if lf else "")
    last = ""
    try:
        last = run(["git", "log", "-1", "--date=short", "--format=%h · %ad · %an · %s"], root).strip()
    except AnalysisError:
        pass
    heads = {e: read(root, e, 3500) for e in entry[:3]}
    return dict(
        name=meta.get("name") or name, repo=f"{owner}/{name}", url=f"https://github.com/{owner}/{name}",
        description=gh.get("description") or meta.get("description") or "", topics=gh.get("topics", []), stars=gh.get("stargazers_count"),
        homepage=gh.get("homepage") or "", default_branch=gh.get("default_branch") or "", license=lic, version=meta.get("version", ""),
        readme_title=title, readme_intro=intro, readme_features=feats, readme_excerpt=rtext[:5000],
        languages=langs, primary_language=langs[0]["name"] if langs else "", frameworks=fw, deps=deps, scripts=dict(list(scripts.items())[:12]),
        entry_points=entry, modules=mods, routes=routes, env_vars=env_vars(root, files),
        top_dirs=[dict(path=k, files=v, hint=DIR_HINTS.get(k.lower(), "")) for k, v in top.most_common(14)],
        root_files=[f for f in files if "/" not in f][:30], tree=tree(files),
        tests=dict(files=sum(1 for f in files if is_test(f) and Path(f).suffix in LANGS), command=test[0] if test else ""),
        ci=ci[:6], infra=infra, install=inst, run=runc, test_cmd=test, file_count=len(files),
        line_count=sum(l["lines"] for l in langs), last_commit=last, truncated=len(files) >= MAX_FILES, _heads=heads)


# ───────────────────────────── writing (AI + fallback) ─────────────────────────────

SYSTEM = (
    "You are a senior technical writer. You get JSON facts about a GitHub repository (README excerpt, file tree, dependencies, "
    "entry-point source, modules, API routes). Write clear project documentation for a newcomer. Use ONLY the facts provided; "
    "never invent features, files, commands or APIs. If something is unknown, say so briefly. Reply with JSON only: "
    '{"tagline": "one line", "overview": "2-4 sentences: what the project is", "purpose": "2-4 sentences: the problem it solves and who it is for", '
    '"features": ["short bullets of what it can do"], "architecture": "one short paragraph on how it is organised and how parts interact", '
    '"tech_notes": "1-2 sentences on the stack", "prerequisites": ["tools needed to run it"], "usage": "short paragraph on how to use it", '
    '"components": {"<path of a top-level directory or key file>": "one sentence"}, "notes": ["limitations, caveats or things unclear from the repo"]}'
)


def fallback(f):
    desc, intro = f["description"], f["readme_intro"]
    lang = f["primary_language"] or "multi-language"
    fw = f" using {', '.join(f['frameworks'][:4])}" if f["frameworks"] else ""
    overview = f"{f['name']} is a {lang} project{fw}." + (f" {desc}" if desc else "") + (f" {intro}" if intro and intro != desc else "")
    if not desc and not intro:
        overview += " The repository has no README or description, so its purpose is inferred from the code layout below."
    feats = list(f["readme_features"])
    if not feats and f["routes"]:
        feats = [f"Exposes {len(f['routes'])} HTTP endpoint(s) (see API Endpoints)."]
    comps = {d["path"] + "/": d["hint"] for d in f["top_dirs"] if d["hint"]}
    for m in f["modules"][:12]:
        if m["doc"]:
            comps[m["path"]] = m["doc"]
    ep = f" Entry point(s): {', '.join('`' + e + '`' for e in f['entry_points'][:3])}." if f["entry_points"] else ""
    arch = (f"The repository has {f['file_count']} files (about {f['line_count']:,} lines of code)" +
            (f", mostly {lang}" if f["primary_language"] else "") + "." +
            (f" Top-level folders: {', '.join(d['path'] + '/' for d in f['top_dirs'][:8])}." if f["top_dirs"] else "") + ep)
    prereq = []
    if f["primary_language"] == "Python" or any("requirements" in k or "pyproject" in k for k in f["deps"]):
        prereq.append("Python 3")
    if "package.json" in f["deps"]:
        prereq.append("Node.js and npm")
    if "go.mod" in f["deps"]:
        prereq.append("Go")
    if "Cargo.toml" in f["deps"]:
        prereq.append("Rust and Cargo")
    if "pom.xml" in f["deps"]:
        prereq.append("Java and Maven")
    if "Dockerfile" in f["infra"]:
        prereq.append("Docker (optional)")
    return dict(tagline=desc or f["readme_title"] or f"{f['name']} repository", overview=overview.strip(),
                purpose=intro or desc or "The repository does not state its purpose explicitly.", features=feats, architecture=arch,
                tech_notes="", prerequisites=prereq,
                usage=("Run the commands in Getting Started, then use the entry points listed above." if f["run"] else ""),
                components=comps, notes=[], source="heuristic")


def write(f):
    base = fallback(f)
    if not os.getenv("ANTHROPIC_API_KEY"):
        return base
    try:
        import anthropic
        ctx = {k: v for k, v in f.items() if k not in ("tree", "readme_intro", "readme_features", "_heads", "root_files")}
        ctx["tree"], ctx["entry_point_source"] = f["tree"], f["_heads"]
        msg = anthropic.Anthropic().messages.create(
            model=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5"), max_tokens=3500, system=SYSTEM,
            messages=[{"role": "user", "content": json.dumps(ctx)[:45000]}])
        text = re.sub(r"^```(?:json)?|```$", "", msg.content[0].text.strip(), flags=re.M).strip()
        data = json.loads(text)
        data["components"] = {**base["components"], **{k: v for k, v in (data.get("components") or {}).items() if isinstance(v, str)}}
        out = {**base, **{k: v for k, v in data.items() if v}, "source": "claude"}
        out["features"] = [x for x in out["features"] if isinstance(x, str)]
        return out
    except Exception as e:  # keep the document usable even if the API call fails
        return {**base, "ai_error": str(e)}


def generate(repo_url):
    owner, name = parse_repo(repo_url)
    try:
        gh = github._req(f"https://api.github.com/repos/{owner}/{name}")
    except AnalysisError as e:
        if e.status == 404 and not os.getenv("GITHUB_TOKEN"):
            gh = {}  # may be private; the clone below decides
        else:
            gh = {}
    tmp, root = shallow_clone(owner, name)
    try:
        facts = collect(root, owner, name, gh)
    finally:
        _rm(tmp)
    facts.pop("_heads_done", None)
    ai = write(facts)
    facts.pop("_heads", None)
    facts.pop("readme_excerpt", None)
    return dict(repo=f"{owner}/{name}", facts=facts, ai=ai, created=time.time())


# ───────────────────────────── rendering ─────────────────────────────

def _cell(x):
    return str(x).replace("|", "\\|").replace("\n", " ")


def _table(head, rows):
    return ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)] + ["| " + " | ".join(_cell(c) for c in r) + " |" for r in rows]


def _slug(t):
    return re.sub(r"[^\w\- ]", "", t.lower()).strip().replace(" ", "-")


def sections(d):
    f, ai = d["facts"], d["ai"]
    S = []
    S.append(("Overview", [ai["overview"]]))
    S.append(("Purpose", [ai["purpose"]]))
    if ai.get("features"):
        S.append(("Key Features", [f"- {x}" for x in ai["features"]]))
    ts = []
    if f["languages"]:
        ts += _table(["Language", "Files", "Lines", "Share"], [(l["name"], l["files"], f"{l['lines']:,}", f"{l['pct']}%") for l in f["languages"]]) + [""]
    if f["frameworks"]:
        ts += [f"**Frameworks & libraries:** {', '.join(f['frameworks'])}", ""]
    if ai.get("tech_notes"):
        ts += [ai["tech_notes"]]
    if f["infra"]:
        ts += ["", "**Tooling:** " + ", ".join(f"`{x}`" for x in f["infra"])]
    if ts:
        S.append(("Tech Stack", ts))
    S.append(("Architecture", [ai["architecture"]] + (["", "**Entry points:** " + ", ".join(f"`{e}`" for e in f["entry_points"])] if f["entry_points"] else [])))
    S.append(("Project Structure", ["```text", f["name"] + "/", f["tree"], "```"]))
    if ai["components"]:
        S.append(("Components and Modules", _table(["Path", "Role"], [(f"`{k}`", v) for k, v in list(ai["components"].items())[:30]])))
    elif f["modules"]:
        S.append(("Components and Modules", _table(["Path", "Role"], [(f"`{m['path']}`", m["doc"] or ", ".join(m["classes"] + m["functions"])) for m in f["modules"][:30]])))
    if f["routes"]:
        S.append(("API Endpoints", _table(["Method", "Route", "Defined in"], [(r["method"], f"`{r['path']}`", f"`{r['file']}`" + (f" → `{r['handler']}`" if r["handler"] else "")) for r in f["routes"]])))
    if f["env_vars"]:
        S.append(("Configuration", ["Environment variables referenced in the code:", ""] + _table(["Variable", "First seen in"], [(f"`{v['name']}`", f"`{v['where']}`") for v in f["env_vars"]])))
    gs = []
    if ai.get("prerequisites"):
        gs += ["**Prerequisites**", ""] + [f"- {x}" for x in ai["prerequisites"]] + [""]
    gs += [f"**Get the code**", "", "```bash", f"git clone {f['url']}.git", f"cd {f['url'].rsplit('/', 1)[-1]}", "```", ""]
    if f["install"]:
        gs += ["**Install**", "", "```bash"] + f["install"] + ["```", ""]
    if f["run"]:
        gs += ["**Run**", "", "```bash"] + f["run"] + ["```"]
    if not f["install"] and not f["run"]:
        gs += ["No standard install or run commands were detected; check the README for build instructions."]
    S.append(("Getting Started", gs))
    if ai.get("usage"):
        S.append(("Usage", [ai["usage"]]))
    if f["scripts"]:
        S.append(("Scripts", _table(["Name", "Command"], [(f"`{k}`", f"`{v}`") for k, v in f["scripts"].items()])))
    t = f["tests"]
    S.append(("Testing", [f"{t['files']} test file(s) detected." + (f" Run them with `{t['command']}`." if t["command"] else "")] if t["files"] else ["No automated tests were detected in this repository."]))
    if f["ci"] or f["infra"]:
        S.append(("CI/CD and Deployment", [f"- `{x}`" for x in f["ci"] + [i for i in f["infra"]]]))
    if f["deps"]:
        ds = []
        for k, v in f["deps"].items():
            if v:
                ds += [f"**{k}**", "", ", ".join(f"`{x}`" for x in v[:40]) + (" …" if len(v) > 40 else ""), ""]
        if ds:
            S.append(("Dependencies", ds))
    st = [("Files", f"{f['file_count']:,}" + ("+" if f["truncated"] else "")), ("Lines of code", f"{f['line_count']:,}")]
    for k, label in (("stars", "GitHub stars"), ("license", "License"), ("version", "Version"), ("default_branch", "Default branch"), ("last_commit", "Latest commit")):
        if f.get(k) not in (None, ""):
            st.append((label, f[k]))
    S.append(("Project Statistics", _table(["Metric", "Value"], st)))
    if ai.get("notes"):
        S.append(("Notes and Limitations", [f"- {x}" for x in ai["notes"]]))
    return S


def markdown(d):
    f, ai = d["facts"], d["ai"]
    S = sections(d)
    head = [f"# {f['name']}", "", f"> {ai['tagline']}", "", f"**Repository:** [{f['repo']}]({f['url']})  "]
    if f["primary_language"]:
        head.append(f"**Primary language:** {f['primary_language']}  ")
    if f["license"]:
        head.append(f"**License:** {f['license']}  ")
    if f["topics"]:
        head.append("**Topics:** " + ", ".join(f["topics"][:10]) + "  ")
    head += [f"**Documentation generated:** {time.strftime('%Y-%m-%d', time.localtime(d.get('created', time.time())))} · written by {ai.get('source', 'heuristic')}", "",
             "## Table of Contents", ""] + [f"{i}. [{t}](#{_slug(t)})" for i, (t, _) in enumerate(S, 1)] + [""]
    body = []
    for t, lines in S:
        body += [f"## {t}", ""] + lines + [""]
    foot = ["---", "*Generated automatically by AI Change Impact Analyzer from the repository contents. Verify details against the source.*"]
    return "\n".join(head + body + foot) + "\n"


def _inline(s):
    s = esc(s)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"\*([^*\s][^*]*)\*", r"<i>\1</i>", s)
    return re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+|#[\w\-]+)\)", r'<a href="\2">\1</a>', s)


def _md_to_html(md):
    out, lines, i, para = [], md.splitlines(), 0, []

    def flush():
        if para:
            out.append("<p>" + _inline(" ".join(para)) + "</p>")
            para.clear()
    while i < len(lines):
        l = lines[i]
        if l.startswith("```"):
            flush(); i += 1; buf = []
            while i < len(lines) and not lines[i].startswith("```"):
                buf.append(lines[i]); i += 1
            out.append("<pre>" + esc("\n".join(buf)) + "</pre>")
        elif re.match(r"^#{1,3} ", l):
            flush(); n = len(l.split(" ")[0]); t = l[n + 1:]
            out.append(f'<h{n} id="{_slug(t)}">{_inline(t)}</h{n}>')
        elif l.startswith("|"):
            flush(); rows = []
            while i < len(lines) and lines[i].startswith("|"):
                if not re.match(r"^\|[-| ]+\|$", lines[i]):
                    rows.append([c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", lines[i].strip().strip("|"))])
                i += 1
            i -= 1
            out.append("<div class='tw'><table><tr>" + "".join(f"<th>{_inline(c)}</th>" for c in rows[0]) + "</tr>" +
                       "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>" for r in rows[1:]) + "</table></div>")
        elif re.match(r"^(- |\d+\. )", l):
            flush(); tag = "ul" if l.startswith("- ") else "ol"; items = []
            while i < len(lines) and re.match(r"^(- |\d+\. )", lines[i]):
                items.append(re.sub(r"^(- |\d+\. )", "", lines[i])); i += 1
            i -= 1
            out.append(f"<{tag}>" + "".join(f"<li>{_inline(x)}</li>" for x in items) + f"</{tag}>")
        elif l.startswith("> "):
            flush(); out.append(f"<blockquote>{_inline(l[2:])}</blockquote>")
        elif l.startswith("---"):
            flush(); out.append("<hr>")
        elif not l.strip():
            flush()
        else:
            para.append(l.strip())
            if l.endswith("  "):
                para[-1] += " ·"
        i += 1
    flush()
    return "\n".join(out)


def html(d):
    f = d["facts"]
    return f"""<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<base target="_blank"><title>{esc(f['name'])} - Project Documentation</title>
<style>body{{font:15px/1.65 system-ui,-apple-system,Segoe UI,sans-serif;color:#14213d;max-width:900px;margin:0 auto;padding:32px 22px 64px}}
h1{{font-size:34px;margin:0 0 6px}}h2{{border-bottom:2px solid #cfd6e2;padding-bottom:4px;margin-top:36px;font-size:22px}}
blockquote{{margin:8px 0 16px;padding:6px 16px;border-left:5px solid #2b59c3;background:#f4f6fa;font-size:17px}}
a{{color:#2b59c3}}code{{font:13px ui-monospace,Menlo,Consolas,monospace;background:#eef1f5;padding:1px 5px;border-radius:4px;word-break:break-word}}
pre{{background:#14213d;color:#e8eefc;padding:12px 14px;border-radius:6px;overflow-x:auto;font:13px/1.5 ui-monospace,Menlo,Consolas,monospace}}
.tw{{overflow-x:auto}}table{{border-collapse:collapse;width:100%;margin:10px 0}}th,td{{border:1px solid #cfd6e2;padding:6px 10px;text-align:left;vertical-align:top}}th{{background:#eef1f5}}
hr{{border:0;border-top:1px solid #cfd6e2;margin:36px 0 12px}}li{{margin:3px 0}}@media print{{body{{max-width:none;padding:0}}pre{{white-space:pre-wrap}}}}</style>
<body>{_md_to_html(markdown(d))}</body></html>"""
