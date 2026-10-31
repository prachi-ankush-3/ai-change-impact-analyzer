"""Core engine: clone repo -> read commit -> index Python code -> dependency graph -> impact + tests."""
import ast, os, re, subprocess, tempfile
from collections import defaultdict, deque
from pathlib import Path

CACHE = Path(tempfile.gettempdir()) / "aica_repos"
SKIP = {".git", "venv", ".venv", "node_modules", "site-packages", "__pycache__", "build", "dist", ".tox"}
CONFIG = {"requirements.txt", "pyproject.toml", "setup.py", "setup.cfg", "Pipfile", "poetry.lock", "tox.ini", "Dockerfile"}
MAX_DEPTH = 3
HTTP = {"get", "post", "put", "delete", "patch", "route"}


class AnalysisError(Exception):
    def __init__(self, msg, status=400):
        super().__init__(msg)
        self.status = status


def run(cmd, cwd=None, timeout=180):
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        raise AnalysisError("Git operation timed out. The repository may be too large.", 504)
    if r.returncode:
        raise AnalysisError(re.sub(r"x-access-token:[^@]+@", "", r.stderr.strip()) or "git command failed")
    return r.stdout


def parse_repo(url):
    u = url.strip()
    m = re.search(r"github\.com[/:]([\w.-]+)/([\w.-]+?)(?:\.git)?(?:/.*)?$", u) or re.match(r"^([\w.-]+)/([\w.-]+)$", u)
    if not m:
        raise AnalysisError("Enter a GitHub URL such as https://github.com/owner/repo", 422)
    return m[1], m[2]


def clone(url):
    owner, name = parse_repo(url)
    dest = CACHE / f"{owner}__{name}"
    tok = os.getenv("GITHUB_TOKEN")
    try:
        if dest.exists():
            run(["git", "fetch", "--all", "--quiet"], dest)
        else:
            CACHE.mkdir(exist_ok=True)
            auth = f"x-access-token:{tok}@" if tok else ""
            run(["git", "clone", "--quiet", "--filter=blob:none", f"https://{auth}github.com/{owner}/{name}.git", str(dest)], timeout=600)
    except AnalysisError as e:
        if any(k in str(e).lower() for k in ("not found", "authentication", "could not read", "terminal prompts")):
            raise AnalysisError(f"Cannot access {owner}/{name}. Check the URL, or set GITHUB_TOKEN for private repos.", 404)
        raise
    return dest


def list_commits(repo, n=30):
    out = run(["git", "log", f"-n{n}", "--date=short", "--format=%H%x1f%an%x1f%ad%x1f%s", "origin/HEAD"], repo)
    keys = ("sha", "author", "date", "subject")
    return [dict(zip(keys, l.split("\x1f"))) for l in out.splitlines()]


def is_test(p):
    path = Path(p)
    return path.name.startswith("test_") or path.name.endswith("_test.py") or path.name == "conftest.py" \
        or bool({"tests", "test"} & set(path.parts))


def names_in(node):
    return {x.id for x in ast.walk(node) if isinstance(x, ast.Name)} | \
           {x.attr for x in ast.walk(node) if isinstance(x, ast.Attribute)}


class Index:
    """Parses every .py file: definitions (with line ranges + names used) and imports."""

    def __init__(self, root):
        self.root, self.files = Path(root), {}
        for p in self.root.rglob("*.py"):
            rel = p.relative_to(self.root)
            if SKIP & set(rel.parts):
                continue
            try:
                self.files[rel.as_posix()] = self._scan(ast.parse(p.read_text("utf-8", "ignore")))
            except (SyntaxError, ValueError):
                continue
        self.mods, self.tail = {}, {}
        for f in self.files:
            self.mods[self._mod(f)] = f
        for m, f in self.mods.items():
            parts = m.split(".")
            for i in range(len(parts)):
                self.tail.setdefault(".".join(parts[i:]), f)
        self.rdeps = defaultdict(set)  # file -> files that import it
        for f, info in self.files.items():
            for t in self._resolve(f, info["imports"]):
                if t != f:
                    self.rdeps[t].add(f)

    @staticmethod
    def _scan(tree):
        defs, imports = {}, []

        def visit(body, prefix):
            for n in body:
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    q = prefix + n.name
                    route = None
                    for dec in getattr(n, "decorator_list", []):
                        f = dec.func if isinstance(dec, ast.Call) else dec
                        if isinstance(dec, ast.Call) and isinstance(f, ast.Attribute) and f.attr in HTTP and dec.args \
                                and isinstance(dec.args[0], ast.Constant):
                            route = f"{'ANY' if f.attr == 'route' else f.attr.upper()} {dec.args[0].value}"
                    defs[q] = dict(kind="class" if isinstance(n, ast.ClassDef) else "function",
                                   start=n.lineno, end=n.end_lineno, uses=names_in(n), route=route)
                    visit(n.body, q + ".")
        visit(tree.body, "")
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                imports += [(a.name, None, 0) for a in n.names]
            elif isinstance(n, ast.ImportFrom):
                imports += [(n.module or "", a.name, n.level) for a in n.names]
        return dict(defs=defs, imports=imports, uses=names_in(tree))

    @staticmethod
    def _mod(f):
        m = f[:-3].replace("/", ".")
        m = m.removesuffix(".__init__")
        return m.removeprefix("src.")

    def _lookup(self, m):
        return (self.mods.get(m) or self.tail.get(m)) if m else None

    def _resolve(self, f, imports):
        pkg = self._mod(f).split(".")
        if not f.endswith("__init__.py"):
            pkg = pkg[:-1]
        out = set()
        for mod, name, lvl in imports:
            if lvl:
                base = pkg[:max(0, len(pkg) - (lvl - 1))]
                full = ".".join(base + ([mod] if mod else []))
            else:
                full = mod
            for cand in (full, f"{full}.{name}" if name else None):
                t = self._lookup(cand)
                if t:
                    out.add(t)
        return out


def level(score):
    return "High" if score >= 65 else "Medium" if score >= 40 else "Low"


def _rng(sha, base):
    return ["git", "diff", base, sha] if base else ["git", "show", "--format=", sha]


def read_changes(repo, sha, base=None):
    out = []
    for line in run([*_rng(sha, base), "--name-status", "-M"], repo).splitlines():
        parts = line.split("\t")
        out.append(dict(status=parts[0][0], path=parts[-1]))
    return out


def read_hunks(repo, sha, path, base=None):
    patch = run([*_rng(sha, base), "-U0", "--", path], repo)
    lines, adds, dels = set(), 0, 0
    for l in patch.splitlines():
        m = re.match(r"@@ -\S+ \+(\d+)(?:,(\d+))? @@", l)
        if m:
            s, c = int(m[1]), 1 if m[2] is None else int(m[2])
            lines.update(range(s, s + max(c, 1)))
        elif l.startswith("+") and not l.startswith("+++"):
            adds += 1
        elif l.startswith("-") and not l.startswith("---"):
            dels += 1
    return lines, adds, dels, patch


def analyze(repo, sha=None, pr=None):
    base = None
    if pr:
        try:
            run(["git", "fetch", "origin", f"pull/{pr}/head"], repo)
        except AnalysisError:
            raise AnalysisError(f"Pull request #{pr} was not found in this repository.", 404)
        sha = "FETCH_HEAD"
    if not sha:
        raise AnalysisError("Provide a commit SHA or a pull request number.", 422)
    try:
        sha = run(["git", "rev-parse", "--verify", f"{sha}^{{commit}}"], repo).strip()
    except AnalysisError:
        raise AnalysisError(f"Commit '{sha}' was not found in this repository.", 404)
    if pr:
        base = run(["git", "merge-base", "origin/HEAD", sha], repo).strip()
    run(["git", "checkout", "-q", "--force", sha], repo)
    idx = Index(repo)
    h, author, date, msg = run(["git", "show", "-s", "--date=short", "--format=%H%x1f%an%x1f%ad%x1f%B", sha], repo).split("\x1f")
    commit = dict(sha=h, author=author, date=date, message=msg.strip(), pr=pr, base=base)

    # 1. Changed files -> changed symbols
    changed, short, churn, diffs = [], {}, {}, {}
    for c in read_changes(repo, sha, base):
        p = c["path"]
        rec = dict(path=p, status=c["status"], symbols=[], adds=0, dels=0, fan_in=0)
        if p.endswith(".py") and p in idx.files:
            lines, rec["adds"], rec["dels"], diffs[p] = read_hunks(repo, sha, p, base)
            defs = idx.files[p]["defs"]
            hit = [q for q, d in defs.items() if any(d["start"] <= l <= d["end"] for l in lines)]
            hit = [q for q in hit if not any(o != q and o.startswith(q + ".") for o in hit)]
            rec["symbols"] = hit or (["<module>"] if lines else [])
            short[p] = {q.split(".")[-1] for q in hit}
            churn[p] = rec["adds"] + rec["dels"]
            rec["fan_in"] = len(idx.rdeps[p])
            score = 20 + min(rec["fan_in"] * 4, 30) + min(churn[p] // 10, 20) + \
                (10 if any(not s.split(".")[-1].startswith("_") and s != "<module>" for s in rec["symbols"]) else 0)
            rec["score"] = 10 if is_test(p) else min(score, 100)
            rec["kind"] = "test" if is_test(p) else "source"
        elif Path(p).name in CONFIG:
            rec.update(kind="config", score=55, note="Dependency/build config: can affect the whole project.")
        elif c["status"] == "D" and p.endswith(".py"):
            rec.update(kind="source", score=60, note="File deleted: importers may break (not traced in this version).")
        else:
            rec.update(kind="other", score=5)
        rec["level"] = level(rec["score"])
        changed.append(rec)

    # 2. Reverse-dependency BFS (who imports the changed files, up to MAX_DEPTH hops)
    roots = [c["path"] for c in changed if c["path"] in short]
    dist, parent, root, q = {}, {}, {}, deque()
    for r in roots:
        dist[r], root[r] = 0, r
        q.append(r)
    while q:
        x = q.popleft()
        if dist[x] >= MAX_DEPTH:
            continue
        for y in sorted(idx.rdeps[x]):
            if y not in dist:
                dist[y], parent[y], root[y] = dist[x] + 1, x, root[x]
                q.append(y)

    def chain(y):
        c = [y]
        while c[-1] in parent:
            c.append(parent[c[-1]])
        return c

    def refs(f, r):
        names = short.get(r, set())
        return [k for k, d in idx.files[f]["defs"].items() if d["uses"] & names] if names else []

    # 3. Affected source components + impact score
    affected = []
    for y, d in dist.items():
        if d == 0 or is_test(y):
            continue
        direct = [r for r in roots if y in idx.rdeps[r]]
        uses = sorted({f"{k} uses {', '.join(sorted(short[r] & idx.files[y]['uses']))}"
                       for r in direct for k in refs(y, r)})
        s = {1: 35, 2: 20, 3: 10}[d] + (20 if uses else 0) + min(len(idx.rdeps[y]) * 3, 15) + min(churn[root[y]] // 20, 15)
        affected.append(dict(path=y, distance=d, chain=chain(y), uses=uses[:6], fan_in=len(idx.rdeps[y]),
                             score=s, level=level(s)))
    affected.sort(key=lambda a: -a["score"])

    # 4. Test recommendations
    rec = {}

    def add(t, prio, reason, nodes=(), d=9):
        r = rec.setdefault(t, dict(path=t, priority="should", reasons=[], nodes=[], d=d))
        r["priority"] = "must" if "must" in (prio, r["priority"]) else "should"
        r["d"] = min(r["d"], d)
        if reason not in r["reasons"]:
            r["reasons"].append(reason)
        r["nodes"] += [n for n in nodes if n not in r["nodes"]]

    for c in changed:
        if c.get("kind") == "test" and c["path"] in idx.files:
            add(c["path"], "must", "Modified in this commit", d=0)
    for y, d in dist.items():
        if d and is_test(y) and y.endswith(".py"):
            direct = [r for r in roots if y in idx.rdeps[r]]
            if direct:
                nodes = [k.replace(".", "::") and f"{y}::{k.replace('.', '::')}" for r in direct for k in refs(y, r)
                         if k.split(".")[-1].startswith("test")]
                add(y, "must", f"Imports {', '.join(direct)}", nodes, d)
            else:
                add(y, "should", "Reaches changed code via " + " → ".join(chain(y)), d=d)
    for r in roots:
        stem = Path(r).stem
        for t in idx.files:
            if is_test(t) and Path(t).name in (f"test_{stem}.py", f"{stem}_test.py"):
                add(t, "must", f"Naming convention matches {r}", d=1)
    tests = sorted(rec.values(), key=lambda t: (t["priority"] != "must", t["d"], t["path"]))[:25]
    musts = [t for t in tests if t["priority"] == "must"] or tests[:5]
    targets = [n for t in musts for n in (t["nodes"][:5] or [t["path"]])]
    command = ("pytest -q " + " ".join(targets)) if targets else "pytest -q  # no targeted tests found: run the full suite"

    # 5. APIs, risk factors, impact graph, overall risk
    symmap = {c["path"]: set(c["symbols"]) for c in changed}
    apath = {a["path"]: a for a in affected}
    apis = []
    for f in sorted(set(roots) | set(apath)):
        for q, d in idx.files[f]["defs"].items():
            if d.get("route"):
                if q in symmap.get(f, ()):
                    apis.append(dict(route=d["route"], handler=q, file=f, status="changed"))
                elif f in apath and any(u.startswith(q + " uses") for u in apath[f]["uses"]):
                    apis.append(dict(route=d["route"], handler=q, file=f, status="affected"))

    factors = []
    F = lambda sev, text: factors.append(dict(severity=sev, text=text))
    src = [c for c in changed if c.get("kind") == "source"]
    top = max(src, key=lambda c: c["fan_in"], default=None)
    if top and top["fan_in"]:
        F("High" if top["fan_in"] >= 5 else "Medium", f"{top['path']} is imported by {top['fan_in']} file(s), so behaviour changes ripple outward")
    total = sum(c["adds"] + c["dels"] for c in changed)
    if total >= 30:
        F("High" if total >= 150 else "Medium", f"Large change: {total} lines added or removed")
    pub = [f"{s_}" for c in src for s_ in c["symbols"] if s_ != "<module>" and not s_.split(".")[-1].startswith("_")]
    if pub:
        F("Medium", f"Public function/class modified: {', '.join(pub[:3])}{'…' if len(pub) > 3 else ''}")
    if [a for a in apis if a["status"] == "changed"]:
        F("High", "API endpoint handler changed: " + ", ".join(a["route"] for a in apis if a["status"] == "changed")[:120])
    if [a for a in apis if a["status"] == "affected"]:
        F("Medium", "API endpoints depend on the changed code: " + ", ".join(a["route"] for a in apis if a["status"] == "affected")[:120])
    if any(c.get("kind") == "config" for c in changed):
        F("Medium", "Dependency or build configuration changed")
    if any(c["status"] == "D" and c["path"].endswith(".py") for c in changed):
        F("High", "Python files were deleted; importers may break")
    if src and not [t for t in tests if t["priority"] == "must"]:
        F("High", "No test file imports or is named after the changed code")
    deep = sum(a["distance"] >= 2 for a in affected)
    if deep >= 3:
        F("Medium", f"{deep} components are reachable only through indirect dependencies, which are easy to miss")

    scores = [c["score"] for c in changed] + [a["score"] for a in affected]
    risk_level = level(max(scores, default=0))
    if risk_level != "High" and sum(a["level"] != "Low" for a in affected) >= 6:
        risk_level = "High"
    if risk_level == "Low" and any(f["severity"] == "High" for f in factors):
        risk_level = "Medium"

    nodes = {r: dict(id=r, layer=0, level=next(c["level"] for c in changed if c["path"] == r)) for r in roots}
    for a in affected[:60]:
        nodes[a["path"]] = dict(id=a["path"], layer=1 if a["distance"] == 1 else 2, level=a["level"])
    for t in tests[:12]:
        if t["path"] in dist and t["path"] not in nodes:
            nodes[t["path"]] = dict(id=t["path"], layer=3, level="Test")
    edges = [dict(s=parent[n], t=n) for n in nodes if n in parent and parent[n] in nodes]

    ctx = dict(commit=commit,
               changed=[dict(path=c["path"], status=c["status"], symbols=c["symbols"], diff=diffs.get(c["path"], "")[:3000]) for c in changed[:8]],
               affected=[{k: a[k] for k in ("path", "level", "distance", "chain", "uses")} for a in affected[:20]],
               tests=[t["path"] for t in tests[:10]], risk=risk_level, factors=factors, apis=apis)
    return dict(commit=commit, risk=dict(level=risk_level, score=max(scores, default=0)), changed=changed,
                affected=affected, tests=tests, command=command, apis=apis, factors=factors,
                graph=dict(nodes=list(nodes.values()), edges=edges),
                stats=dict(py_files=len(idx.files), edges=sum(len(v) for v in idx.rdeps.values())), context=ctx)
