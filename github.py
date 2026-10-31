"""GitHub REST helpers (PR listing, PR comments) and webhook signature check."""
import hashlib, hmac, json, os, urllib.error, urllib.request
from analyzer import AnalysisError


def _req(url, data=None):
    h = {"User-Agent": "ai-change-impact-analyzer", "Accept": "application/vnd.github+json"}
    if os.getenv("GITHUB_TOKEN"):
        h["Authorization"] = "Bearer " + os.environ["GITHUB_TOKEN"]
    req = urllib.request.Request(url, json.dumps(data).encode() if data else None, h)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        msg = {403: "GitHub API rate limit reached. Set GITHUB_TOKEN to raise it.", 404: "Repository not found on GitHub."}
        raise AnalysisError(msg.get(e.code, f"GitHub API error {e.code}"), 404 if e.code == 404 else 502)
    except urllib.error.URLError:
        raise AnalysisError("Could not reach GitHub.", 502)


def list_pulls(owner, name):
    return [dict(number=p["number"], title=p["title"], author=p["user"]["login"],
                 state="merged" if p.get("merged_at") else p["state"], updated=p["updated_at"][:10])
            for p in _req(f"https://api.github.com/repos/{owner}/{name}/pulls?state=all&per_page=30")]


def post_comment(owner, name, pr, body):
    if os.getenv("GITHUB_TOKEN"):
        _req(f"https://api.github.com/repos/{owner}/{name}/issues/{pr}/comments", {"body": body})


def valid_signature(body, header):
    secret = os.getenv("GITHUB_WEBHOOK_SECRET")
    if not secret or not header:
        return False
    want = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, header)
