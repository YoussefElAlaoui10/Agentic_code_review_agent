"""Command line entry point.

    prreview review  <target>   -- review a PR or a local diff
    prreview speedup <target>   -- sequential vs parallel scaling table
    prreview quality            -- precision/recall/F1 against labelled cases
    prreview serve-mcp          -- expose the reviewer as an MCP server (stdio)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Sequence

from . import __version__
from .bench import (bench_quality, bench_speedup, load_labels, quality_markdown,
                    speedup_markdown)
from .ghclient import (TargetError, fetch_pr_files, load_diff_file, parse_pr_target)
from .judge import JudgeConfig
from .llm import MockLLM, build_client, endpoints_from_env
from .models import FileDiff
from .orchestrator import OrchestratorConfig, ReviewOrchestrator
from .render import (dump_json, render_github_review, render_markdown,
                     render_terminal, svg_bar_chart, svg_timeline)
from .reviewer import DEFAULT_ROLES, ROLES


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--mock", action="store_true",
                   help="use the offline deterministic model (no API, no network)")
    p.add_argument("--model", default=None, help="model name sent to the endpoint")
    p.add_argument("--mock-latency", type=float, default=0.30,
                   help="simulated per-call latency for --mock (seconds)")
    p.add_argument("--roles", default=",".join(DEFAULT_ROLES),
                   help=f"comma-separated reviewer roles from {sorted(ROLES)}")
    p.add_argument("--repo-root", default=None,
                   help="local checkout, enabling the grep/context tools")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="prreview",
        description="Agentic pull-request reviewer: parallel reviewer agents + a judge.",
    )
    parser.add_argument("--version", action="version", version=f"prreview {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    rev = sub.add_parser("review", help="review a pull request or a local diff")
    rev.add_argument("target", help="PR URL, owner/repo#N, or a path to a .diff/.patch")
    rev.add_argument("-c", "--concurrency", type=int, default=6)
    rev.add_argument("--sequential", action="store_true",
                     help="disable the fan-out (the benchmark baseline)")
    rev.add_argument("--min-confidence", type=float, default=0.45)
    rev.add_argument("--max-findings", type=int, default=25)
    rev.add_argument("--max-files", type=int, default=40)
    rev.add_argument("--polish", action="store_true",
                     help="let the judge rewrite titles with one extra LLM call")
    rev.add_argument("--format", choices=["text", "markdown", "json", "github"],
                     default="text")
    rev.add_argument("-o", "--output", default=None, help="write the report to a file")
    rev.add_argument("--save-run", default=None, help="write the full run JSON here")
    rev.add_argument("-v", "--verbose", action="store_true")
    rev.add_argument("--no-colour", action="store_true")
    _add_common(rev)

    sp = sub.add_parser("speedup", help="measure sequential vs parallel scaling")
    sp.add_argument("target", help="PR URL, owner/repo#N, or a .diff/.patch path")
    sp.add_argument("--concurrencies", default="1,2,4,8")
    sp.add_argument("--repeats", type=int, default=1,
                    help="runs per level; median is reported")
    sp.add_argument("--out-dir", default="results")
    _add_common(sp)

    q = sub.add_parser("quality", help="score findings against labelled defects")
    q.add_argument("--labels", action="append", default=None,
                   help="labels JSON file (repeatable); defaults to bench/*.labels.json")
    q.add_argument("--thresholds", default="0.0,0.45,0.6,0.75")
    q.add_argument("-c", "--concurrency", type=int, default=6)
    q.add_argument("--out-dir", default="results")
    _add_common(q)

    mcp = sub.add_parser("serve-mcp", help="run as an MCP server over stdio")
    _add_common(mcp)
    return parser


async def _resolve_target(target: str, *, max_files: int) -> tuple[list[FileDiff], str]:
    """A path on disk means offline mode; anything else is a GitHub PR."""
    if os.path.exists(target):
        return load_diff_file(target), f"{target} (local diff)"
    ref = parse_pr_target(target)
    files = await fetch_pr_files(ref, max_files=max_files)
    return files, ref.slug


def _client_from_args(args: argparse.Namespace):
    return build_client(mock=True if args.mock else None, model=args.model,
                        latency=args.mock_latency)


def _roles_from_args(args: argparse.Namespace) -> tuple[str, ...]:
    roles = tuple(r.strip() for r in args.roles.split(",") if r.strip())
    unknown = [r for r in roles if r not in ROLES]
    if unknown:
        raise SystemExit(f"unknown role(s) {unknown}; known roles: {sorted(ROLES)}")
    return roles or DEFAULT_ROLES


def _announce(client, stream=sys.stderr) -> None:
    urls = endpoints_from_env()
    if isinstance(client, MockLLM):
        why = ("--mock requested" if urls else
               "no PRREVIEW_ENDPOINTS / PRREVIEW_BASE_URL set")
        print(f"[transport] offline mock model ({why})", file=stream)
    else:
        print(f"[transport] {client.name} -> model {client.model} "
              f"({len(urls)} endpoint(s))", file=stream)


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #

async def cmd_review(args: argparse.Namespace) -> int:
    files, label = await _resolve_target(args.target, max_files=args.max_files)
    if not files:
        print("Nothing reviewable in this target.", file=sys.stderr)
        return 1

    client = _client_from_args(args)
    _announce(client)
    cfg = OrchestratorConfig(
        concurrency=max(1, args.concurrency),
        roles=_roles_from_args(args),
        max_files=args.max_files,
        sequential=args.sequential,
        polish=args.polish,
        judge=JudgeConfig(min_confidence=args.min_confidence,
                          max_findings=args.max_findings),
    )
    orch = ReviewOrchestrator(client, cfg)
    report = await orch.run(
        files, target=label,
        repo_root=Path(args.repo_root) if args.repo_root else None,
    )

    if args.format == "text":
        body = render_terminal(report, colour=not args.no_colour and sys.stdout.isatty(),
                               verbose=args.verbose)
    elif args.format == "markdown":
        body = render_markdown(report)
    elif args.format == "github":
        body = json.dumps(render_github_review(report), indent=2, ensure_ascii=False)
    else:
        body = report.to_json()

    if args.output:
        Path(args.output).write_text(body + "\n", encoding="utf-8")
        print(f"wrote {args.output}", file=sys.stderr)
    else:
        print(body)

    if args.save_run:
        dump_json(report.to_dict(), args.save_run)
        print(f"wrote {args.save_run}", file=sys.stderr)

    await client.aclose()
    # Exit 1 when a high-severity finding survived: usable as a CI gate.
    return 1 if any(f.severity == "high" for f in report.findings) else 0


async def cmd_speedup(args: argparse.Namespace) -> int:
    files, label = await _resolve_target(args.target, max_files=40)
    concurrencies = tuple(int(c) for c in args.concurrencies.split(",") if c.strip())
    roles = _roles_from_args(args)

    def factory():
        return _client_from_args(args)

    _announce(factory())
    print(f"[speedup] {len(files)} files x {len(roles)} roles = "
          f"{len(files) * len(roles)} tasks, levels {list(concurrencies)}, "
          f"repeats={args.repeats}", file=sys.stderr)

    report = await bench_speedup(
        factory, files, concurrencies=concurrencies, roles=roles,
        repeats=args.repeats,
        repo_root=Path(args.repo_root) if args.repo_root else None,
    )

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    table = speedup_markdown(report)
    print()
    print(f"### Scaling on {label}")
    print()
    print(table)
    print()

    dump_json(report.to_dict(), str(out_dir / "speedup.json"))
    (out_dir / "speedup.md").write_text(
        f"### Scaling on {label}\n\n{table}\n", encoding="utf-8")

    parallel = [r for r in report.rows if r.mode == "parallel"]
    chart = svg_bar_chart(
        [(str(r.concurrency), report.speedup(r)) for r in parallel],
        title=f"Speedup vs sequential ({len(files)} files x {len(roles)} roles)",
        y_label="speedup", value_fmt="{:.2f}x",
        ideal=[float(r.concurrency) for r in parallel],
    )
    (out_dir / "speedup.svg").write_text(chart, encoding="utf-8")

    if report.timeline:
        class _M:
            def __init__(self, d): self.__dict__.update(d)
        tl = svg_timeline([_M(d) for d in report.timeline],
                          title="Reviewer tasks over time (peak concurrency)")
        (out_dir / "timeline.svg").write_text(tl, encoding="utf-8")

    print(f"wrote {out_dir}/speedup.{{json,md,svg}} and timeline.svg", file=sys.stderr)
    return 0


async def cmd_quality(args: argparse.Namespace) -> int:
    label_files = args.labels or sorted(str(p) for p in Path("bench").glob("*.labels.json"))
    if not label_files:
        print("No labels file. Pass --labels bench/<name>.labels.json", file=sys.stderr)
        return 1

    cases = []
    for lf in label_files:
        labels, meta = load_labels(lf)
        diff_path = meta.get("diff")
        if not diff_path:
            print(f"{lf}: missing a top-level \"diff\" key", file=sys.stderr)
            return 1
        resolved = (Path(lf).parent / diff_path) if not Path(diff_path).exists() else Path(diff_path)
        files = load_diff_file(str(resolved))
        cases.append((meta.get("target", lf), files, labels))
        print(f"[quality] {meta.get('target', lf)}: {len(files)} files, "
              f"{len(labels)} labelled defects", file=sys.stderr)

    thresholds = tuple(float(t) for t in args.thresholds.split(",") if t.strip())

    def factory():
        return _client_from_args(args)

    _announce(factory())
    result = await bench_quality(factory, cases, thresholds=thresholds,
                                 roles=_roles_from_args(args),
                                 concurrency=args.concurrency)
    table = quality_markdown(result)
    print()
    print("### Review quality against labelled defects")
    print()
    print(table)
    print()
    best = result.get("best_f1")
    if best:
        print(f"Best F1: {best['f1']:.2f} at threshold {best['threshold']:.2f} "
              f"(precision {best['precision']:.2f}, recall {best['recall']:.2f})")
        if best["missed"]:
            print(f"\nStill missed ({len(best['missed'])}):")
            for m in best["missed"][:10]:
                print(f"  - {m}")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    dump_json(result, str(out_dir / "quality.json"))
    (out_dir / "quality.md").write_text(
        f"### Review quality against labelled defects\n\n{table}\n", encoding="utf-8")

    rows = [(f"{r['threshold']:.2f}", r["f1"]) for r in result["sweep"]]
    (out_dir / "quality.svg").write_text(
        svg_bar_chart(rows, title="F1 vs judge confidence threshold",
                      y_label="F1", value_fmt="{:.2f}"),
        encoding="utf-8")
    print(f"\nwrote {out_dir}/quality.{{json,md,svg}}", file=sys.stderr)
    return 0


async def cmd_serve_mcp(args: argparse.Namespace) -> int:
    from .mcp_server import serve
    await serve(_client_from_args(args), roles=_roles_from_args(args))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {
        "review": cmd_review, "speedup": cmd_speedup,
        "quality": cmd_quality, "serve-mcp": cmd_serve_mcp,
    }
    try:
        return asyncio.run(handlers[args.command](args))
    except TargetError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
