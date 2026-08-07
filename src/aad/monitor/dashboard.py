"""Dashboard rendering.

One self-contained HTML page — no CDN, no build step, no external fonts. A workshop
terminal with no internet still renders it, which is the same constraint that shaped
the rest of the system.

The layout puts the hallucination rate first and largest, because that is the number
the whole design exists to hold at zero. Faithfulness, citation accuracy and the
abstention-versus-grounding split follow, then the per-task breakdown that says *where*
the system is failing to ground.
"""

from __future__ import annotations

import html
import json
from datetime import UTC, datetime
from typing import Any

from aad.monitor.store import MonitorStore

_CSS = """
:root {
  --bg:#0f1115; --panel:#171a21; --line:#252a34; --text:#e6e9ef; --muted:#98a2b3;
  --good:#2ecc71; --warn:#f5a623; --bad:#ff4d4f; --info:#4d9fff;
}
@media (prefers-color-scheme: light) {
  :root { --bg:#f6f7f9; --panel:#fff; --line:#e3e6ec; --text:#12151b; --muted:#5b6472; }
}
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--text); font:15px/1.5 ui-sans-serif,
  system-ui, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
main { max-width:1180px; margin:0 auto; padding:28px 20px 64px; }
h1 { font-size:22px; margin:0 0 4px; letter-spacing:-0.01em; }
h2 { font-size:14px; text-transform:uppercase; letter-spacing:.08em; color:var(--muted);
  margin:32px 0 12px; font-weight:600; }
.sub { color:var(--muted); font-size:13px; margin:0 0 24px; }
.grid { display:grid; gap:12px; grid-template-columns:repeat(auto-fit,minmax(190px,1fr)); }
.card { background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:16px; }
.card .label { font-size:12px; color:var(--muted); text-transform:uppercase;
  letter-spacing:.06em; }
.card .value { font-size:30px; font-weight:650; margin-top:6px; letter-spacing:-0.02em; }
.card .foot { font-size:12px; color:var(--muted); margin-top:4px; }
.hero .value { font-size:44px; }
.good { color:var(--good); } .warn { color:var(--warn); } .bad { color:var(--bad); }
.muted { color:var(--muted); }
table { width:100%; border-collapse:collapse; background:var(--panel);
  border:1px solid var(--line); border-radius:10px; overflow:hidden; font-size:14px; }
th, td { text-align:left; padding:10px 12px; border-bottom:1px solid var(--line);
  white-space:nowrap; }
th { font-size:12px; text-transform:uppercase; letter-spacing:.05em; color:var(--muted);
  font-weight:600; }
tr:last-child td { border-bottom:none; }
td.wrap { white-space:normal; }
.scroll { overflow-x:auto; }
.bar { display:flex; height:22px; border-radius:6px; overflow:hidden; border:1px solid var(--line); }
.bar span { display:block; }
.legend { display:flex; gap:16px; flex-wrap:wrap; font-size:12px; color:var(--muted);
  margin-top:8px; }
.dot { display:inline-block; width:9px; height:9px; border-radius:2px; margin-right:5px; }
.pill { display:inline-block; padding:2px 8px; border-radius:999px; font-size:12px;
  font-weight:600; }
.pill.grounded { background:rgba(46,204,113,.15); color:var(--good); }
.pill.abstained { background:rgba(245,166,35,.15); color:var(--warn); }
.pill.escalated { background:rgba(77,159,255,.15); color:var(--info); }
.pill.blocked { background:rgba(255,77,79,.15); color:var(--bad); }
.pill.no_claims { background:rgba(152,162,179,.15); color:var(--muted); }
.spark { display:flex; align-items:flex-end; gap:3px; height:56px; margin-top:10px; }
.spark div { flex:1; background:var(--info); border-radius:2px 2px 0 0; min-height:2px; }
.spark div.has-block { background:var(--bad); }
.empty { color:var(--muted); font-size:14px; background:var(--panel);
  border:1px dashed var(--line); border-radius:10px; padding:20px; }
code { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:13px; }
"""


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def _num(value: float | None, digits: int = 2) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def _rate_class(value: float | None, *, good_is_low: bool, warn: float = 0.05) -> str:
    """Colour a rate. Absent data is grey, never green — see MonitorStore._ratio."""
    if value is None:
        return "muted"
    if good_is_low:
        return "good" if value == 0 else ("warn" if value <= warn else "bad")
    return "good" if value >= 0.9 else ("warn" if value >= 0.6 else "bad")


def _card(label: str, value: str, foot: str = "", cls: str = "", hero: bool = False) -> str:
    return (
        f'<div class="card{" hero" if hero else ""}"><div class="label">{html.escape(label)}</div>'
        f'<div class="value {cls}">{value}</div>'
        f'<div class="foot">{html.escape(foot)}</div></div>'
    )


def _verdict_bar(verdicts: dict[str, int]) -> str:
    order = [
        ("grounded", "var(--good)"),
        ("abstained", "var(--warn)"),
        ("escalated", "var(--info)"),
        ("blocked", "var(--bad)"),
        ("no_claims", "var(--muted)"),
    ]
    total = sum(verdicts.values())
    if not total:
        return '<div class="empty">No monitored outputs yet.</div>'
    segments = "".join(
        f'<span style="width:{verdicts.get(name, 0) / total * 100:.2f}%;background:{colour}"'
        f' title="{name}: {verdicts.get(name, 0)}"></span>'
        for name, colour in order
        if verdicts.get(name)
    )
    legend = "".join(
        f'<span><i class="dot" style="background:{colour}"></i>'
        f"{name.replace('_', ' ')} · {verdicts.get(name, 0)}</span>"
        for name, colour in order
    )
    return f'<div class="bar">{segments}</div><div class="legend">{legend}</div>'


def _sparkline(series: list[dict[str, Any]]) -> str:
    peak = max((b["events"] for b in series), default=0)
    if not peak:
        return '<div class="empty">No activity in the charted window.</div>'
    bars = "".join(
        '<div class="{cls}" style="height:{h:.1f}%" title="{when}: {n} event(s), '
        '{blocked} blocked"></div>'.format(
            cls="has-block" if b["blocked"] else "",
            h=max(b["events"] / peak * 100, 3),
            when=datetime.fromtimestamp(b["bucket_start"], UTC).strftime("%Y-%m-%d"),
            n=b["events"],
            blocked=b["blocked"],
        )
        for b in series
    )
    return f'<div class="spark">{bars}</div>'


def _task_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return '<div class="empty">No monitored outputs yet.</div>'
    body = "".join(
        "<tr>"
        f"<td>{html.escape(r['task_type'])}</td>"
        f"<td>{r['events']}</td>"
        f"<td class=\"{_rate_class(r['grounding_rate'], good_is_low=False)}\">"
        f"{_pct(r['grounding_rate'])}</td>"
        f"<td>{_pct(r['abstention_rate'])}</td>"
        f"<td class=\"{_rate_class(r['hallucination_rate'], good_is_low=True)}\">"
        f"{_pct(r['hallucination_rate'])}</td>"
        f"<td>{_num(r['mean_faithfulness'])}</td>"
        f"<td>{_num(r['mean_citation_accuracy'])}</td>"
        f"<td>{_num(r['mean_semantic_consistency'])}</td>"
        "</tr>"
        for r in rows
    )
    return (
        '<div class="scroll"><table><thead><tr>'
        "<th>Task type</th><th>Events</th><th>Grounded</th><th>Abstained</th>"
        "<th>Hallucinated</th><th>Faithfulness</th><th>Citation acc.</th><th>Semantic</th>"
        f"</tr></thead><tbody>{body}</tbody></table></div>"
    )


def _reason_table(reasons: dict[str, int]) -> str:
    if not reasons:
        return '<div class="empty">No abstentions recorded.</div>'
    total = sum(reasons.values())
    body = "".join(
        f"<tr><td>{html.escape(name.replace('_', ' '))}</td><td>{count}</td>"
        f"<td>{count / total * 100:.1f}%</td></tr>"
        for name, count in reasons.items()
    )
    return (
        '<div class="scroll"><table><thead><tr><th>Trigger</th><th>Count</th><th>Share</th>'
        f"</tr></thead><tbody>{body}</tbody></table></div>"
    )


def _events_table(events: list[dict[str, Any]]) -> str:
    if not events:
        return '<div class="empty">No monitored outputs yet.</div>'
    body = ""
    for e in events:
        when = datetime.fromtimestamp(e["created_at"], UTC).strftime("%Y-%m-%d %H:%M")
        reason = e["abstention_reason"] or ""
        body += (
            "<tr>"
            f"<td>{when}</td>"
            f'<td><span class="pill {html.escape(e["verdict"])}">'
            f"{html.escape(e['verdict'])}</span></td>"
            f"<td>{html.escape(e['task_type'])}</td>"
            f"<td>{html.escape(e['vehicle'] or '—')}</td>"
            f"<td class=\"wrap\">{html.escape((e['question'] or '')[:110])}</td>"
            f"<td>{e['claim_count']}</td>"
            f"<td class=\"{'bad' if e['fabrication_count'] else 'muted'}\">"
            f"{e['fabrication_count']}</td>"
            f"<td>{html.escape(reason.replace('_', ' '))}</td>"
            f"<td>{'yes' if e['needs_human_review'] and not e['reviewed_at'] else '—'}</td>"
            "</tr>"
        )
    return (
        '<div class="scroll"><table><thead><tr>'
        "<th>When (UTC)</th><th>Verdict</th><th>Task</th><th>Vehicle</th><th>Question</th>"
        "<th>Claims</th><th>Fabricated</th><th>Trigger</th><th>Awaiting review</th>"
        f"</tr></thead><tbody>{body}</tbody></table></div>"
    )


def _fabrication_list(items: list[dict[str, Any]]) -> str:
    if not items:
        return (
            '<div class="empty">No fabrications recorded. Every claim in every monitored '
            "output appeared verbatim in a cited source.</div>"
        )
    rows = ""
    for item in items:
        when = datetime.fromtimestamp(item["created_at"], UTC).strftime("%Y-%m-%d %H:%M")
        for claim in item["claims"]:
            value = f"{claim.get('value', '')}{claim.get('unit') or ''}"
            rows += (
                "<tr>"
                f"<td>{when}</td>"
                f"<td>{html.escape(item['task_type'])}</td>"
                f"<td><code>{html.escape(str(claim.get('type', '')))}</code></td>"
                f"<td class=\"bad\"><code>{html.escape(value)}</code></td>"
                f"<td class=\"wrap\">{html.escape(str(claim.get('text', ''))[:160])}</td>"
                "</tr>"
            )
    return (
        '<div class="scroll"><table><thead><tr>'
        "<th>When (UTC)</th><th>Task</th><th>Claim type</th><th>Asserted value</th>"
        f"<th>Sentence</th></tr></thead><tbody>{rows}</tbody></table></div>"
    )


def dashboard_data(store: MonitorStore, *, since: float | None = None) -> dict[str, Any]:
    """Everything the dashboard shows, as JSON. The HTML is one renderer of this."""
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "summary": store.summary(since=since),
        "by_task_type": store.by_task_type(since=since),
        "abstention_reasons": store.abstention_reasons(since=since),
        "timeseries": store.timeseries(),
        "recent": store.recent(limit=25),
        "fabrications": store.fabrications(limit=25),
        "review_queue": store.review_queue(limit=25),
    }


def render_dashboard(data: dict[str, Any]) -> str:
    s = data["summary"]
    halluc_cls = _rate_class(s["hallucination_rate"], good_is_low=True, warn=0.0)
    claim_events = s["claim_events"]

    cards = "".join(
        [
            _card(
                "Hallucination rate",
                _pct(s["hallucination_rate"]),
                f"{s['verdicts']['blocked']} blocked of {claim_events} claim-bearing outputs",
                halluc_cls,
                hero=True,
            ),
            _card(
                "Mean faithfulness",
                _num(s["mean_faithfulness"]),
                "share of claims found in a cited source",
                _rate_class(s["mean_faithfulness"], good_is_low=False),
            ),
            _card(
                "Citation accuracy",
                _num(s["mean_citation_accuracy"]),
                "citations pointing at a supplied source",
                _rate_class(s["mean_citation_accuracy"], good_is_low=False),
            ),
            _card(
                "Semantic consistency",
                _num(s["mean_semantic_consistency"]),
                "claim vs. best-matching cited source",
            ),
        ]
    )

    routing = "".join(
        [
            _card(
                "Grounded",
                _pct(s["grounding_rate"]),
                f"{s['verdicts']['grounded']} answered and verified",
                _rate_class(s["grounding_rate"], good_is_low=False),
            ),
            _card(
                "Abstained / escalated",
                _pct(s["abstention_rate"]),
                f"{s['verdicts']['abstained']} abstained · {s['verdicts']['escalated']} escalated",
            ),
            _card("Awaiting human review", str(s["pending_review"]), "unresolved in the queue"),
            _card(
                "Fabricated claims",
                f"{s['fabrications']} / {s['claims']}",
                f"claim-level rate {_pct(s['fabricated_claim_rate'])}",
                "bad" if s["fabrications"] else "good",
            ),
        ]
    )

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Grounding monitor — auto mechanic diagnostic assistant</title>
<style>{_CSS}</style></head>
<body><main>
<h1>Grounding monitor</h1>
<p class="sub">Zero-tolerance verification of every specification the assistant states.
A claim is grounded only when its literal value appears in a source the answer cited.
Generated {html.escape(data["generated_at"])} · {s["events"]} monitored output(s).</p>

<div class="grid">{cards}</div>

<h2>Routing</h2>
<div class="grid">{routing}</div>

<h2>Verdict mix</h2>
{_verdict_bar(s["verdicts"])}

<h2>Activity (daily; red bars contain a block)</h2>
{_sparkline(data["timeseries"])}

<h2>By task type</h2>
{_task_table(data["by_task_type"])}

<h2>Abstention triggers</h2>
{_reason_table(data["abstention_reasons"])}

<h2>Fabrications caught</h2>
{_fabrication_list(data["fabrications"])}

<h2>Recent outputs</h2>
{_events_table(data["recent"])}

<p class="sub" style="margin-top:32px">Rates over an empty denominator render as “—”,
never 0%. Machine-readable form of this page: <code>GET /api/v1/monitor/dashboard.json</code>.</p>
</main></body></html>"""


def render_from_store(store: MonitorStore, *, since: float | None = None) -> str:
    return render_dashboard(dashboard_data(store, since=since))


def dashboard_json(store: MonitorStore, *, since: float | None = None) -> str:
    return json.dumps(dashboard_data(store, since=since), indent=2, default=str)
