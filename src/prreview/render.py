"""Output renderers: terminal, markdown, GitHub review payload, SVG charts.

No matplotlib here on purpose -- the SVG is written directly, so the repo has no
dependency beyond the standard library and the charts drop straight into the
report.
"""

from __future__ import annotations

import html
import json
from typing import Any, Sequence

from .models import Finding, RunReport

SEVERITY_ICON = {"high": "!!", "medium": " !", "low": "  "}
ANSI = {"high": "\033[31m", "medium": "\033[33m", "low": "\033[90m",
        "reset": "\033[0m", "bold": "\033[1m", "dim": "\033[2m"}


def _c(text: str, key: str, colour: bool) -> str:
    return f"{ANSI[key]}{text}{ANSI['reset']}" if colour else text


def render_terminal(report: RunReport, *, colour: bool = True, verbose: bool = False) -> str:
    out: list[str] = []
    st = report.judge_stats
    out.append(_c(f"Review of {report.target}", "bold", colour))
    out.append(
        f"  model={report.model or 'n/a'}  mode={report.mode}  "
        f"concurrency={report.concurrency}  files={report.files_reviewed}  "
        f"tasks={st.get('tasks', 0)}"
    )
    out.append(
        f"  wall clock {report.wall_clock_s:.2f}s   "
        f"summed agent time {st.get('sum_task_time_s', 0):.2f}s   "
        f"peak overlap {st.get('max_observed_overlap', 0)}"
    )
    out.append(
        f"  {st.get('raw', 0)} raw -> {st.get('after_dedupe', 0)} deduped -> "
        f"{st.get('published', 0)} published "
        f"({st.get('merged_groups', 0)} merged, "
        f"{st.get('dropped_low_confidence', 0)} below threshold, "
        f"{st.get('corroborated', 0)} corroborated)"
    )
    failed = st.get("failed_reviewers") or []
    if failed:
        out.append(_c(f"  {len(failed)} reviewer task(s) failed:", "medium", colour))
        for f in failed[:5]:
            out.append(f"    - {f}")
    out.append("")

    if not report.findings:
        out.append("No findings above the confidence threshold.")
        return "\n".join(out)

    current = None
    for f in report.findings:
        if f.path != current:
            current = f.path
            out.append(_c(f"{f.path}", "bold", colour))
        agree = f" +{len(f.agreed_by)} agree" if f.agreed_by else ""
        out.append(
            f"  {_c(SEVERITY_ICON[f.severity], f.severity, colour)} "
            f"L{f.line:<5} {_c(f.severity, f.severity, colour)}/{f.category:<5} "
            f"conf {f.confidence:.2f}{agree}  {f.title}"
        )
        out.append(f"        {_c(f.justification, 'dim', colour)}")
        if f.suggestion:
            out.append(f"        {_c('-> ' + f.suggestion, 'dim', colour)}")
        if verbose:
            out.append(f"        {_c(f'[{f.reviewer}] {f.fingerprint()}', 'dim', colour)}")
    return "\n".join(out)


def render_markdown(report: RunReport) -> str:
    st = report.judge_stats
    out = [
        f"# Automated review of `{report.target}`", "",
        f"- Model: `{report.model or 'n/a'}`",
        f"- Mode: {report.mode}, concurrency {report.concurrency}",
        f"- {report.files_reviewed} files, {st.get('tasks', 0)} reviewer tasks",
        f"- Wall clock **{report.wall_clock_s:.2f}s** "
        f"(summed agent time {st.get('sum_task_time_s', 0):.2f}s, "
        f"peak overlap {st.get('max_observed_overlap', 0)})",
        f"- {st.get('raw', 0)} raw findings -> {st.get('published', 0)} published", "",
    ]
    if not report.findings:
        out.append("No findings above the confidence threshold.")
        return "\n".join(out)

    out += ["| File | Line | Severity | Category | Conf. | Finding |",
            "|---|---:|---|---|---:|---|"]
    for f in report.findings:
        title = f.title.replace("|", "\\|")
        out.append(
            f"| `{f.path}` | {f.line} | {f.severity} | {f.category} | "
            f"{f.confidence:.2f} | {title} |"
        )
    out += ["", "## Details", ""]
    for f in report.findings:
        agree = f" (corroborated by {', '.join(f.agreed_by)})" if f.agreed_by else ""
        out += [f"### `{f.path}:{f.line}` -- {f.title}",
                f"**{f.severity} / {f.category}**, confidence {f.confidence:.2f}"
                f", found by `{f.reviewer}`{agree}", "",
                f.justification, ""]
        if f.suggestion:
            out += [f"*Suggestion:* {f.suggestion}", ""]
    return "\n".join(out)


def render_github_review(report: RunReport, *, event: str = "COMMENT") -> dict[str, Any]:
    """A POST /pulls/{n}/reviews body. Produced, never sent: posting to a real
    PR is a side effect the user has to trigger themselves."""
    comments = [
        {"path": f.path, "line": max(1, f.line), "side": "RIGHT",
         "body": (f"**{f.severity.upper()} / {f.category}** "
                  f"(confidence {f.confidence:.2f})\n\n"
                  f"**{f.title}**\n\n{f.justification}"
                  + (f"\n\n*Suggestion:* {f.suggestion}" if f.suggestion else ""))}
        for f in report.findings
    ]
    st = report.judge_stats
    return {
        "event": event,
        "body": (f"Automated review: {st.get('published', 0)} findings from "
                 f"{st.get('tasks', 0)} parallel reviewer agents "
                 f"({st.get('raw', 0)} raw, deduplicated and filtered by the judge)."),
        "comments": comments,
    }


# --------------------------------------------------------------------------- #
# SVG charts (no plotting dependency)
# --------------------------------------------------------------------------- #

def svg_bar_chart(rows: Sequence[tuple[str, float]], *, title: str,
                  y_label: str = "", width: int = 640, height: int = 320,
                  value_fmt: str = "{:.2f}", ideal: Sequence[float] | None = None) -> str:
    """Horizontal-axis bar chart; optionally overlays an 'ideal' dashed line."""
    if not rows:
        return "<svg xmlns='http://www.w3.org/2000/svg'></svg>"
    pad_l, pad_r, pad_t, pad_b = 64, 24, 48, 56
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    vmax = max(max(v for _, v in rows), *(ideal or [0])) or 1.0
    vmax *= 1.15
    n = len(rows)
    slot = plot_w / n
    bar_w = slot * 0.56

    parts = [
        f"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 {width} {height}' "
        f"width='{width}' height='{height}' font-family='system-ui,sans-serif'>",
        "<rect width='100%' height='100%' fill='#ffffff'/>",
        f"<text x='{width / 2}' y='26' text-anchor='middle' font-size='15' "
        f"font-weight='600' fill='#111'>{html.escape(title)}</text>",
    ]
    for frac in (0, 0.25, 0.5, 0.75, 1.0):
        y = pad_t + plot_h - frac * plot_h
        parts.append(f"<line x1='{pad_l}' y1='{y:.1f}' x2='{pad_l + plot_w}' "
                     f"y2='{y:.1f}' stroke='#e5e7eb' stroke-width='1'/>")
        parts.append(f"<text x='{pad_l - 8}' y='{y + 4:.1f}' text-anchor='end' "
                     f"font-size='11' fill='#6b7280'>{frac * vmax:.1f}</text>")
    if y_label:
        parts.append(f"<text x='14' y='{pad_t + plot_h / 2}' font-size='11' "
                     f"fill='#6b7280' transform='rotate(-90 14 {pad_t + plot_h / 2})' "
                     f"text-anchor='middle'>{html.escape(y_label)}</text>")

    for i, (label, value) in enumerate(rows):
        h = max(1.0, (value / vmax) * plot_h)
        x = pad_l + i * slot + (slot - bar_w) / 2
        y = pad_t + plot_h - h
        parts.append(f"<rect x='{x:.1f}' y='{y:.1f}' width='{bar_w:.1f}' "
                     f"height='{h:.1f}' rx='3' fill='#2563eb'/>")
        parts.append(f"<text x='{x + bar_w / 2:.1f}' y='{y - 6:.1f}' "
                     f"text-anchor='middle' font-size='11' fill='#111'>"
                     f"{value_fmt.format(value)}</text>")
        parts.append(f"<text x='{pad_l + i * slot + slot / 2:.1f}' "
                     f"y='{pad_t + plot_h + 18:.1f}' text-anchor='middle' "
                     f"font-size='11' fill='#374151'>{html.escape(label)}</text>")

    if ideal:
        pts = " ".join(
            f"{pad_l + i * slot + slot / 2:.1f},{pad_t + plot_h - (v / vmax) * plot_h:.1f}"
            for i, v in enumerate(ideal)
        )
        parts.append(f"<polyline points='{pts}' fill='none' stroke='#dc2626' "
                     f"stroke-width='2' stroke-dasharray='5 4'/>")
        parts.append(f"<text x='{pad_l + plot_w}' y='{pad_t - 10}' "
                     f"text-anchor='end' font-size='11' fill='#dc2626'>"
                     f"ideal (linear)</text>")
    parts.append("</svg>")
    return "\n".join(parts)


def svg_timeline(metrics: Sequence[Any], *, title: str, width: int = 760) -> str:
    """Gantt of task start/end windows -- the visual proof of overlap."""
    if not metrics:
        return "<svg xmlns='http://www.w3.org/2000/svg'></svg>"
    t0 = min(m.start for m in metrics)
    t1 = max(m.end for m in metrics)
    span = max(1e-6, t1 - t0)
    row_h = 16
    pad_l, pad_t, pad_r, pad_b = 170, 44, 24, 40
    height = pad_t + row_h * len(metrics) + pad_b
    plot_w = width - pad_l - pad_r
    colours = {"correctness": "#2563eb", "performance": "#059669",
               "tests": "#d97706", "style": "#7c3aed"}

    parts = [
        f"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 {width} {height}' "
        f"width='{width}' height='{height}' font-family='system-ui,sans-serif'>",
        "<rect width='100%' height='100%' fill='#ffffff'/>",
        f"<text x='{width / 2}' y='24' text-anchor='middle' font-size='15' "
        f"font-weight='600' fill='#111'>{html.escape(title)}</text>",
    ]
    ordered = sorted(metrics, key=lambda m: (m.start, m.path))
    for i, m in enumerate(ordered):
        y = pad_t + i * row_h
        x = pad_l + ((m.start - t0) / span) * plot_w
        w = max(1.5, ((m.end - m.start) / span) * plot_w)
        label = f"{m.path.rsplit('/', 1)[-1]} [{m.role}]"[:26]
        parts.append(f"<text x='{pad_l - 8}' y='{y + 11}' text-anchor='end' "
                     f"font-size='10' fill='#374151'>{html.escape(label)}</text>")
        parts.append(f"<rect x='{x:.1f}' y='{y + 3}' width='{w:.1f}' "
                     f"height='{row_h - 6}' rx='2' "
                     f"fill='{colours.get(m.role, '#6b7280')}' "
                     f"opacity='{0.95 if m.ok else 0.4}'/>")
    for frac in (0, 0.25, 0.5, 0.75, 1.0):
        x = pad_l + frac * plot_w
        parts.append(f"<line x1='{x:.1f}' y1='{pad_t}' x2='{x:.1f}' "
                     f"y2='{height - pad_b + 4}' stroke='#e5e7eb'/>")
        parts.append(f"<text x='{x:.1f}' y='{height - pad_b + 20}' "
                     f"text-anchor='middle' font-size='10' fill='#6b7280'>"
                     f"{frac * span:.2f}s</text>")
    parts.append("</svg>")
    return "\n".join(parts)


def markdown_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    align = ["---"] * len(headers)
    out = ["| " + " | ".join(str(h) for h in headers) + " |",
           "| " + " | ".join(align) + " |"]
    for r in rows:
        out.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(out)


def dump_json(obj: Any, path: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
