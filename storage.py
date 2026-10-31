"""SQLite-backed analysis history."""
import json, os, sqlite3, time
from pathlib import Path

DB = Path(os.getenv("AICA_DB", Path(__file__).parent / "history.db"))


def _conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE IF NOT EXISTS analyses(id INTEGER PRIMARY KEY, repo TEXT, sha TEXT, pr INTEGER,
                 title TEXT, risk TEXT, score INTEGER, source TEXT, created REAL, data TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS docs(id INTEGER PRIMARY KEY, repo TEXT, title TEXT, source TEXT, created REAL, data TEXT)""")
    return c


def save(res, source="manual"):
    c = res["commit"]
    with _conn() as db:
        cur = db.execute("INSERT INTO analyses(repo,sha,pr,title,risk,score,source,created,data) VALUES(?,?,?,?,?,?,?,?,?)",
                         (res["repo"], c["sha"], c.get("pr"), c["message"].split("\n")[0], res["risk"]["level"],
                          res["risk"]["score"], source, res["created"], json.dumps(res)))
        return cur.lastrowid


def history(limit=100):
    with _conn() as db:
        return [dict(r) for r in db.execute(
            "SELECT id,repo,sha,pr,title,risk,score,source,created FROM analyses ORDER BY id DESC LIMIT ?", (limit,))]


def get(i):
    with _conn() as db:
        r = db.execute("SELECT data FROM analyses WHERE id=?", (i,)).fetchone()
    if not r:
        return None
    d = json.loads(r["data"]); d["id"] = i
    return d


def delete(i):
    with _conn() as db:
        return db.execute("DELETE FROM analyses WHERE id=?", (i,)).rowcount > 0


def save_doc(d):
    with _conn() as db:
        return db.execute("INSERT INTO docs(repo,title,source,created,data) VALUES(?,?,?,?,?)",
                          (d["repo"], d["facts"]["name"], d["ai"].get("source", "heuristic"), d["created"], json.dumps(d))).lastrowid


def docs_history(limit=100):
    with _conn() as db:
        return [dict(r) for r in db.execute("SELECT id,repo,title,source,created FROM docs ORDER BY id DESC LIMIT ?", (limit,))]


def get_doc(i):
    with _conn() as db:
        r = db.execute("SELECT data FROM docs WHERE id=?", (i,)).fetchone()
    if not r:
        return None
    d = json.loads(r["data"]); d["id"] = i
    return d


def delete_doc(i):
    with _conn() as db:
        return db.execute("DELETE FROM docs WHERE id=?", (i,)).rowcount > 0
