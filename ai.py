"""Turns the deterministic analysis into plain-language explanations (Claude if a key is set)."""
import json, os, re

SYSTEM = (
    "You are a senior engineer reviewing a code change. You get JSON with a commit, changed files (with diffs and "
    "changed symbols), and components found by static dependency analysis. Explain why each component may be affected. "
    "Be specific, cite symbol names, never invent files. Reply with JSON only: "
    '{"summary": "2-3 sentences", "components": {"<path>": "1-2 sentences"}, "watch_out": ["short bullets"], "consequences": ["what could concretely break"], "recommendations": ["concrete review or test actions"]}'
)


def fallback(ctx):
    changed = ", ".join(c["path"] for c in ctx["changed"][:3]) or "no Python files"
    comps = {}
    for a in ctx["affected"]:
        target = a["chain"][-1]
        if a["distance"] == 1 and a["uses"]:
            comps[a["path"]] = f"Imports {target} and references the changed code ({'; '.join(a['uses'][:2])})."
        elif a["distance"] == 1:
            comps[a["path"]] = f"Imports {target} directly, so behaviour changes there can flow through."
        else:
            comps[a["path"]] = f"Indirect: depends on {a['chain'][1]}, which depends on {target} ({a['distance']} hops)."
    n = len(ctx["affected"])
    return dict(summary=f"Changes touch {changed}. Static analysis found {n} dependent component(s); overall risk is {ctx['risk']}.",
                components=comps, watch_out=[],
                consequences=[f["text"] for f in ctx.get("factors", []) if f["severity"] != "Low"][:4],
                recommendations=["Run the recommended tests before merging.", "Review callers of the modified public symbols."])


def explain(ctx):
    base = fallback(ctx)
    if not os.getenv("ANTHROPIC_API_KEY"):
        return {**base, "source": "heuristic"}
    try:
        import anthropic
        msg = anthropic.Anthropic().messages.create(
            model=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5"), max_tokens=2000, system=SYSTEM,
            messages=[{"role": "user", "content": json.dumps(ctx)[:40000]}])
        text = re.sub(r"^```(?:json)?|```$", "", msg.content[0].text.strip(), flags=re.M).strip()
        data = json.loads(text)
        data["components"] = {**base["components"], **data.get("components", {})}
        return {**base, **data, "source": "claude"}
    except Exception as e:  # keep the report usable even if the API call fails
        return {**base, "source": "heuristic", "ai_error": str(e)}
