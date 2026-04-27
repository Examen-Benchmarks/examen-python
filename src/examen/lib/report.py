"""Self-contained HTML report rendering for run payloads.

Pure-stdlib renderer: emits one HTML5 document with inline CSS, no external
resources, and no JavaScript. The output is a single portable file — designed
for the offline path where there's no Examen server in the loop.

The input is the same payload shape that backends receive from ``AsyncBench``
(``project`` / ``bench`` / ``experiment`` / ``case`` / ``version`` / ``run`` /
``metrics`` dicts). That makes the renderer reusable beyond the local backend:
load runs from any source and render.
"""

from __future__ import annotations

import html
import json
from collections import defaultdict
from datetime import UTC, datetime
from statistics import mean
from typing import Any

_CSS = """
:root {
  --bg: #ffffff;
  --fg: #111827;
  --muted: #6b7280;
  --border: #e5e7eb;
  --soft: #f9fafb;
  --ok: #16a34a;
  --err: #dc2626;
  --accent: #3b82f6;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0b0d12;
    --fg: #e5e7eb;
    --muted: #9ca3af;
    --border: #1f2937;
    --soft: #111827;
    --ok: #22c55e;
    --err: #f87171;
    --accent: #60a5fa;
  }
}
* { box-sizing: border-box; }
body {
  font: 14px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, Roboto, sans-serif;
  margin: 0; padding: 32px 24px;
  background: var(--bg); color: var(--fg);
}
body > * { max-width: 1100px; margin-left: auto; margin-right: auto; }
h1 { font-size: 22px; margin: 0 0 4px; font-weight: 600; }
h2 { font-size: 16px; margin: 28px 0 10px; padding-bottom: 6px;
     border-bottom: 1px solid var(--border); font-weight: 600; }
.subtitle { color: var(--muted); font-size: 13px; margin-bottom: 28px; }
.meta { color: var(--muted); font-size: 12px; }
.ok { color: var(--ok); font-weight: 500; }
.err { color: var(--err); font-weight: 500; }
table { width: 100%; border-collapse: collapse; font-size: 13px; margin: 8px 0; }
th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--border); }
th { font-weight: 600; color: var(--muted); font-size: 12px;
     text-transform: uppercase; letter-spacing: 0.04em; }
tr:hover td { background: var(--soft); }
details { margin: 6px 0; }
details > summary { cursor: pointer; padding: 6px 10px; background: var(--soft);
                    border: 1px solid var(--border); border-radius: 4px;
                    list-style: none; }
details > summary::-webkit-details-marker { display: none; }
details > summary::before { content: "\\25b8 "; color: var(--muted); }
details[open] > summary::before { content: "\\25be "; }
details details > summary { background: transparent; border: none; padding: 4px 8px; }
.run-body { padding: 8px 14px 4px; border-left: 2px solid var(--border);
            margin: 4px 0 8px 12px; }
.kv { margin: 4px 0; font-size: 13px; }
.kv .key { color: var(--muted); margin-right: 8px; font-size: 12px; }
.metric { display: inline-block; padding: 2px 8px; margin: 2px 4px 2px 0;
          background: var(--soft); border: 1px solid var(--border);
          border-radius: 12px; font-size: 12px; }
.error-msg { color: var(--err); padding: 8px 10px;
             background: rgba(220, 38, 38, 0.08);
             border: 1px solid rgba(220, 38, 38, 0.2);
             border-radius: 4px; margin: 6px 0;
             font-family: ui-monospace, monospace; font-size: 12px; }
pre { background: var(--soft); padding: 8px 10px; overflow-x: auto;
      font-size: 12px; border-radius: 4px; margin: 4px 0;
      font-family: ui-monospace, monospace; }
.runs { padding-top: 6px; }
.dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%;
       margin-right: 6px; vertical-align: middle; }
.dot-ok { background: var(--ok); }
.dot-err { background: var(--err); }
"""


def render_html(runs: list[dict[str, Any]]) -> str:
    """Render run payloads as a self-contained HTML5 document.

    Args:
        runs: payloads in the shape produced by ``AsyncBench`` — each dict has
            ``project``, ``bench``, ``experiment``, ``case``, ``version``,
            ``run``, ``metrics`` keys.

    Returns:
        A complete HTML document (UTF-8) suitable for writing to disk.
    """
    if not runs:
        return _empty_doc()

    project = str(runs[0]["project"]["name"])
    bench = str(runs[0]["bench"]["name"])
    total = len(runs)
    succeeded = sum(1 for r in runs if r["run"]["status"] == "succeeded")
    errored = total - succeeded
    success_rate = (succeeded / total) * 100 if total else 0.0

    by_experiment: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in runs:
        by_experiment[str(r["experiment"]["name"])].append(r)

    sections = "\n".join(
        _render_experiment(name, exp_runs)
        for name, exp_runs in by_experiment.items()
    )
    timestamp = datetime.now(UTC).isoformat(timespec="seconds")

    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        f"<title>{_esc(f'Examen — {project} / {bench}')}</title>\n"
        f"<style>{_CSS}</style>\n"
        "</head>\n"
        "<body>\n"
        "<header>\n"
        f'  <h1>{_esc(project)} <span class="meta">/</span> {_esc(bench)}</h1>\n'
        f'  <div class="subtitle">{total} runs · '
        f'<span class="ok">{succeeded} ok</span> · '
        f'<span class="err">{errored} errored</span> · '
        f"{success_rate:.1f}% succeeded · "
        f'<span class="meta">{_esc(timestamp)}</span></div>\n'
        "</header>\n"
        "<main>\n"
        f"{sections}\n"
        "</main>\n"
        "</body>\n"
        "</html>\n"
    )


def _render_experiment(name: str, runs: list[dict[str, Any]]) -> str:
    by_case: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in runs:
        by_case[str(r["case"]["name"])].append(r)

    succeeded = sum(1 for r in runs if r["run"]["status"] == "succeeded")
    errored = len(runs) - succeeded

    metric_names: list[str] = []
    for r in runs:
        for m in r.get("metrics", []):
            n = str(m["name"])
            if n not in metric_names:
                metric_names.append(n)

    metric_headers = "".join(f"<th>{_esc(n)}</th>" for n in metric_names)
    case_rows = "\n".join(
        _render_case_row(case_name, case_runs, metric_names)
        for case_name, case_runs in by_case.items()
    )
    run_blocks = "\n".join(_render_run(r) for r in runs)

    return (
        f'<section class="experiment">\n'
        f"<h2>{_esc(name)}</h2>\n"
        f'<div class="meta">{len(runs)} runs · '
        f'<span class="ok">{succeeded} ok</span> · '
        f'<span class="err">{errored} errored</span></div>\n'
        f"<table>\n"
        f"  <thead><tr><th>Case</th><th>Runs</th><th>Status</th>"
        f"{metric_headers}</tr></thead>\n"
        f"  <tbody>\n{case_rows}\n  </tbody>\n"
        f"</table>\n"
        f"<details>\n"
        f"  <summary>Per-run details ({len(runs)})</summary>\n"
        f'  <div class="runs">\n{run_blocks}\n  </div>\n'
        f"</details>\n"
        f"</section>"
    )


def _render_case_row(
    case_name: str,
    runs: list[dict[str, Any]],
    metric_names: list[str],
) -> str:
    succeeded = sum(1 for r in runs if r["run"]["status"] == "succeeded")
    errored = len(runs) - succeeded
    status_html = f'<span class="ok">{succeeded}</span>'
    if errored:
        status_html += f' / <span class="err">{errored}</span>'

    metric_cells: list[str] = []
    for name in metric_names:
        values = [
            float(m["value"])
            for r in runs
            for m in r.get("metrics", [])
            if m["name"] == name
        ]
        if values:
            cell = f"{mean(values):.3g}"
            if len(values) > 1:
                cell += f' <span class="meta">(n={len(values)})</span>'
        else:
            cell = '<span class="meta">—</span>'
        metric_cells.append(f"<td>{cell}</td>")

    return (
        f"    <tr>"
        f"<td><strong>{_esc(case_name)}</strong></td>"
        f"<td>{len(runs)}</td>"
        f"<td>{status_html}</td>"
        f"{''.join(metric_cells)}"
        f"</tr>"
    )


def _render_run(payload: dict[str, Any]) -> str:
    case_name = str(payload["case"]["name"])
    status = str(payload["run"]["status"])
    duration_html = _duration_html(payload["run"])
    dot_class = "dot-ok" if status == "succeeded" else "dot-err"
    summary = (
        f'<span class="dot {dot_class}"></span>'
        f"<strong>{_esc(case_name)}</strong> "
        f'<span class="meta">{_esc(status)}{duration_html}</span>'
    )

    parts: list[str] = []

    input_summary = payload["case"].get("input_summary")
    if input_summary:
        parts.append(
            f'<div class="kv"><span class="key">input</span>'
            f'<span class="val">{_esc(input_summary)}</span></div>'
        )
    output_summary = payload["run"].get("output_summary")
    if output_summary:
        parts.append(
            f'<div class="kv"><span class="key">output</span>'
            f'<span class="val">{_esc(output_summary)}</span></div>'
        )

    error_message = payload["run"].get("error_message")
    if error_message:
        parts.append(f'<div class="error-msg">{_esc(error_message)}</div>')

    metrics = payload.get("metrics", [])
    if metrics:
        chips = " ".join(
            f'<span class="metric"><strong>{_esc(m["name"])}</strong> = '
            f'{float(m["value"]):.3g} '
            f'<span class="meta">{_esc(m["kind"])}</span></span>'
            for m in metrics
        )
        parts.append(f'<div class="metrics">{chips}</div>')

    trace = payload["run"].get("trace") or {}
    steps = trace.get("steps") or []
    if steps:
        rows = "\n".join(
            f"<tr>"
            f'<td><span class="meta">{_esc(s.get("at", ""))}</span></td>'
            f"<td><strong>{_esc(s.get('name', ''))}</strong></td>"
            f"<td><pre>{_esc(json.dumps(s.get('fields', {}), indent=2, default=str))}</pre></td>"
            f"</tr>"
            for s in steps
        )
        parts.append(
            f"<details>"
            f"<summary>Trace ({len(steps)} steps)</summary>"
            f"<table><thead><tr><th>at</th><th>step</th><th>fields</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
            f"</details>"
        )

    body = "\n".join(parts) if parts else '<div class="meta">No details</div>'
    return (
        f"<details>"
        f"<summary>{summary}</summary>"
        f'<div class="run-body">{body}</div>'
        f"</details>"
    )


def _duration_html(run: dict[str, Any]) -> str:
    started = run.get("started_at")
    finished = run.get("finished_at")
    if not (started and finished):
        return ""
    try:
        d = datetime.fromisoformat(str(finished)) - datetime.fromisoformat(str(started))
        return f" · {d.total_seconds() * 1000:.0f}ms"
    except (TypeError, ValueError):
        return ""


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def _empty_doc() -> str:
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        "<title>Examen — empty</title>"
        f"<style>{_CSS}</style></head>"
        '<body><h1>No runs</h1>'
        '<div class="subtitle">This bench produced no runs.</div>'
        "</body></html>\n"
    )
