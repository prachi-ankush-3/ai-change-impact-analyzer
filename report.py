"""Downloadable impact-analysis reports (Markdown and standalone printable HTML)."""
import time
from html import escape as e


def _ref(a):
    c = a["commit"]
    return f"PR #{c['pr']}" if c.get("pr") else c["sha"][:7]


def _when(a):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(a.get("created", time.time())))


def markdown(a):
    c, ai = a["commit"], a["ai"]
    L = [f"# Impact Analysis Report", f"**Repository:** {a['repo']}  ", f"**Change:** {_ref(a)} — {c['message'].splitlines()[0]}  ",
         f"**Author:** {c['author']} ({c['date']})  ", f"**Risk:** {a['risk']['level']} ({a['risk']['score']}/100)  ",
         f"**Generated:** {_when(a)} · explanations by {ai.get('source', 'heuristic')}", "", "## Summary", ai["summary"], "", "## Risk factors"]
    L += [f"- **{f['severity']}**: {f['text']}" for f in a["factors"]] or ["- None detected"]
    L += ["", "## Possible consequences"] + [f"- {x}" for x in ai.get("consequences", [])]
    L += ["", "## Affected components", "| Level | Component | Path | Why |", "|---|---|---|---|"]
    L += [f"| {x['level']} | {'direct' if x['distance'] == 1 else 'indirect'} | `{x['path']}` | {ai['components'].get(x['path'], '')} |" for x in a["affected"][:40]]
    if a["apis"]:
        L += ["", "## API endpoints", "| Status | Route | Handler |", "|---|---|---|"] + [f"| {x['status']} | `{x['route']}` | `{x['file']}::{x['handler']}` |" for x in a["apis"]]
    L += ["", "## Recommended tests", f"```\n{a['command']}\n```"] + [f"- **{t['priority']}** `{t['path']}`: {'; '.join(t['reasons'])}" for t in a["tests"]]
    L += ["", "## Recommendations"] + [f"- {x}" for x in ai.get("recommendations", [])]
    return "\n".join(L) + "\n"


def html(a):
    c, ai, lv = a["commit"], a["ai"], a["risk"]["level"]
    col = {"High": "#c2331f", "Medium": "#c98512", "Low": "#2f7d5b"}
    li = lambda xs: "".join(f"<li>{x}</li>" for x in xs) or "<li>None</li>"
    rows = "".join(f"<tr><td><b style='color:{col[x['level']]}'>{x['level']}</b></td><td><code>{e(x['path'])}</code><br>"
                   f"<small>{e(' → '.join(x['chain']))}</small></td><td>{e(ai['components'].get(x['path'], ''))}</td></tr>" for x in a["affected"][:40])
    apis = "".join(f"<tr><td>{x['status']}</td><td><code>{e(x['route'])}</code></td><td><code>{e(x['file'])}::{e(x['handler'])}</code></td></tr>" for x in a["apis"])
    return f"""<!doctype html><html lang="en"><meta charset="utf-8"><title>Impact report {e(a['repo'])} {e(_ref(a))}</title>
<style>body{{font:15px/1.55 system-ui,sans-serif;color:#14213d;max-width:900px;margin:40px auto;padding:0 20px}}
h1{{margin:0}}h2{{border-bottom:2px solid #cfd6e2;padding-bottom:4px;margin-top:32px}}.risk{{border-left:8px solid {col[lv]};padding:8px 16px;background:#f4f6fa;margin:16px 0}}
.risk b{{color:{col[lv]};font-size:26px}}table{{width:100%;border-collapse:collapse}}td,th{{border-bottom:1px solid #cfd6e2;padding:6px 8px;text-align:left;vertical-align:top}}
code{{font-size:13px;word-break:break-all}}pre{{background:#14213d;color:#fff;padding:10px;border-radius:6px;white-space:pre-wrap}}small{{color:#5b6780}}
@media print{{body{{margin:0}}}}</style>
<h1>Impact Analysis Report</h1><p><small>{e(a['repo'])} · {e(_ref(a))} · {e(c['author'])} · generated {_when(a)}</small></p>
<p><b>{e(c['message'].splitlines()[0])}</b></p>
<div class="risk"><b>{lv} risk</b> ({a['risk']['score']}/100)<p>{e(ai['summary'])}</p></div>
<h2>Risk factors</h2><ul>{li(f"<b>{f['severity']}</b>: {e(f['text'])}" for f in a['factors'])}</ul>
<h2>Possible consequences</h2><ul>{li(e(x) for x in ai.get('consequences', []))}</ul>
<h2>Affected components</h2><table><tr><th>Level</th><th>Component</th><th>Why</th></tr>{rows}</table>
{f'<h2>API endpoints</h2><table>{apis}</table>' if apis else ''}
<h2>Recommended tests</h2><pre>{e(a['command'])}</pre><ul>{li(f"<b>{t['priority']}</b> <code>{e(t['path'])}</code>: {e('; '.join(t['reasons']))}" for t in a['tests'])}</ul>
<h2>Recommendations</h2><ul>{li(e(x) for x in ai.get('recommendations', []))}</ul>
<p><small>Dependencies found by static analysis (Python AST); explanations by {e(ai.get('source', 'heuristic'))}. Heuristic risk scores, not a guarantee.</small></p></html>"""
