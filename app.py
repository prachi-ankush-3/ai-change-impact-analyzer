import os, re, threading, time
from pathlib import Path
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator

import ai, analyzer, docgen, github, report, storage
from analyzer import AnalysisError

app = FastAPI(title="AI Change Impact Analyzer", version="2.0")
lock = threading.Lock()  # analysis checks out commits in a shared clone


@app.exception_handler(AnalysisError)
async def _err(_, e: AnalysisError):
    return JSONResponse({"detail": str(e)}, e.status)


class AnalyzeRequest(BaseModel):
    repo: str
    sha: Optional[str] = None
    pr: Optional[int] = None

    @field_validator("sha")
    @classmethod
    def _sha(cls, v):
        if v and not re.fullmatch(r"[0-9a-fA-F]{7,40}", v.strip()):
            raise ValueError("Commit SHA must be 7-40 hex characters")
        return v.strip() if v else v


def run_analysis(repo, sha=None, pr=None, source="manual"):
    owner, name = analyzer.parse_repo(repo)
    with lock:
        res = analyzer.analyze(analyzer.clone(repo), sha, pr)
    res["ai"] = ai.explain(res.pop("context"))
    res.update(repo=f"{owner}/{name}", created=time.time())
    res["id"] = storage.save(res, source)
    return res


@app.get("/api/health")
def health():
    return dict(ai=bool(os.getenv("ANTHROPIC_API_KEY")), github_token=bool(os.getenv("GITHUB_TOKEN")),
                webhook=bool(os.getenv("GITHUB_WEBHOOK_SECRET")))


@app.get("/api/commits")
def commits(repo: str):
    with lock:
        return analyzer.list_commits(analyzer.clone(repo))


@app.get("/api/pulls")
def pulls(repo: str):
    return github.list_pulls(*analyzer.parse_repo(repo))


@app.post("/api/analyze")
def analyze(req: AnalyzeRequest):
    if not req.sha and not req.pr:
        raise HTTPException(422, "Provide a commit SHA or a pull request number.")
    return run_analysis(req.repo, req.sha, req.pr)


@app.get("/api/history")
def history():
    return storage.history()


@app.get("/api/history/{i}")
def history_item(i: int):
    return storage.get(i) or _missing()


@app.delete("/api/history/{i}")
def history_delete(i: int):
    return {"deleted": storage.delete(i) or _missing()}


@app.get("/api/history/{i}/report")
def get_report(i: int, format: str = "html", download: bool = False):
    a = storage.get(i) or _missing()
    md = format == "md"
    name = f"impact-{a['repo'].replace('/', '-')}-{a['commit']['sha'][:7]}.{'md' if md else 'html'}"
    headers = {"Content-Disposition": f'attachment; filename="{name}"'} if download else {}
    return Response(report.markdown(a) if md else report.html(a), media_type="text/markdown" if md else "text/html", headers=headers)


class DocRequest(BaseModel):
    repo: str


@app.post("/api/docs")
def make_docs(req: DocRequest):
    d = docgen.generate(req.repo)  # uses its own temporary shallow clone, so no lock needed
    d["id"] = storage.save_doc(d)
    return d


@app.get("/api/docs")
def docs_list():
    return storage.docs_history()


@app.get("/api/docs/{i}")
def docs_item(i: int):
    return storage.get_doc(i) or _missing()


@app.delete("/api/docs/{i}")
def docs_delete(i: int):
    return {"deleted": storage.delete_doc(i) or _missing()}


@app.get("/api/docs/{i}/file")
def docs_file(i: int, format: str = "html", download: bool = False):
    d = storage.get_doc(i) or _missing()
    md = format == "md"
    headers = {"Content-Disposition": f'attachment; filename="docs-{d["repo"].replace("/", "-")}.{"md" if md else "html"}"'} if download else {}
    return Response(docgen.markdown(d) if md else docgen.html(d), media_type="text/markdown" if md else "text/html", headers=headers)


def _missing():
    raise HTTPException(404, "Analysis not found.")


def _webhook_job(repo, pr):
    try:
        res = run_analysis(repo, pr=pr, source="webhook")
        github.post_comment(*analyzer.parse_repo(repo), pr, report.markdown(res))
    except Exception as e:  # background task: nothing to return to, so log it
        print(f"webhook analysis failed for {repo}#{pr}: {e}")


@app.post("/api/webhook/github")
async def webhook(request: Request, tasks: BackgroundTasks):
    body = await request.body()
    if not github.valid_signature(body, request.headers.get("X-Hub-Signature-256")):
        raise HTTPException(401, "Invalid or missing signature (is GITHUB_WEBHOOK_SECRET set?)")
    ev, p = request.headers.get("X-GitHub-Event"), await request.json()
    if ev == "pull_request" and p.get("action") in ("opened", "synchronize", "reopened"):
        tasks.add_task(_webhook_job, p["repository"]["html_url"], p["number"])
        return {"status": "queued", "pr": p["number"]}
    return {"status": "ignored", "event": ev}


app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True))
