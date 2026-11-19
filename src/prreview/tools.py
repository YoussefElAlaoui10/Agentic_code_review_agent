"""Tools the reviewer agent may call.

This is what makes the assistant *agentic* rather than a fixed pipeline: the
model decides which of these to call, with what arguments, and when it has seen
enough to answer. A pipeline would instead paste a fixed context and ask once.

Every tool is read-only and sandboxed to the checkout root: a review agent that
can write files or run commands is a remote-code-execution hole, and the diff it
reads is untrusted input.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from .diffparse import numbered_patch
from .models import FileDiff

MAX_TOOL_OUTPUT = 6000


class ToolError(Exception):
    """Returned to the model as a tool result; never raised to the caller."""


@dataclass(slots=True)
class ToolContext:
    """What the tools are allowed to see for one review run."""

    files: dict[str, FileDiff]
    repo_root: Path | None = None

    def diff_for(self, path: str) -> FileDiff | None:
        return self.files.get(path)

    def resolve(self, path: str) -> Path:
        """Resolve `path` inside the checkout, refusing traversal."""
        if self.repo_root is None:
            raise ToolError("no local checkout available; use get_diff_hunks instead")
        root = self.repo_root.resolve()
        candidate = (root / path).resolve()
        if not candidate.is_relative_to(root):
            raise ToolError(f"path {path!r} escapes the repository root")
        if not candidate.exists():
            raise ToolError(f"{path!r} does not exist in the checkout")
        if not candidate.is_file():
            raise ToolError(f"{path!r} is not a file")
        return candidate


def _truncate(text: str, limit: int = MAX_TOOL_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... (truncated, {len(text) - limit} more characters)"


# --------------------------------------------------------------------------- #
# Tool implementations
# --------------------------------------------------------------------------- #

def tool_get_diff_hunks(ctx: ToolContext, *, path: str) -> str:
    """The diff for one file, with real new-file line numbers."""
    fd = ctx.diff_for(path)
    if fd is None:
        known = ", ".join(sorted(ctx.files)[:20]) or "none"
        raise ToolError(f"{path!r} is not part of this change. Files: {known}")
    return _truncate(
        f"File: {fd.path} ({fd.status}, +{fd.additions}/-{fd.deletions})\n"
        f"{numbered_patch(fd)}"
    )


def tool_get_file_context(ctx: ToolContext, *, path: str, around_line: int = 1,
                          radius: int = 40) -> str:
    """Surrounding source from the checkout, to judge a hunk in context.

    Falls back to the diff when no checkout is available, so the agent loop
    behaves identically in the offline demo.
    """
    try:
        resolved = ctx.resolve(path)
    except ToolError:
        fd = ctx.diff_for(path)
        if fd is None:
            raise
        return _truncate(
            "No local checkout; showing the diff instead.\n" + numbered_patch(fd)
        )
    radius = max(1, min(200, int(radius)))
    around_line = max(1, int(around_line))
    lines = resolved.read_text(encoding="utf-8", errors="replace").splitlines()
    lo = max(0, around_line - radius - 1)
    hi = min(len(lines), around_line + radius)
    body = "\n".join(f"{i + 1:>6}  {lines[i]}" for i in range(lo, hi))
    return _truncate(f"{path} lines {lo + 1}-{hi} of {len(lines)}:\n{body}")


def tool_grep_repo(ctx: ToolContext, *, pattern: str, max_results: int = 25,
                   path_glob: str = "") -> str:
    """Search the checkout; the agent uses it to find callers and existing tests."""
    if ctx.repo_root is None:
        raise ToolError("no local checkout available; grep_repo is unavailable")
    try:
        rx = re.compile(pattern)
    except re.error as exc:
        raise ToolError(f"invalid regular expression {pattern!r}: {exc}") from exc

    max_results = max(1, min(100, int(max_results)))
    root = ctx.repo_root.resolve()
    skip_dirs = {".git", "node_modules", ".venv", "__pycache__", "dist", "build", ".mypy_cache"}
    hits: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        for fn in filenames:
            full = Path(dirpath) / fn
            rel = full.relative_to(root).as_posix()
            if path_glob and not Path(rel).match(path_glob):
                continue
            if full.suffix.lower() in {".png", ".jpg", ".pdf", ".zip", ".so", ".pyc"}:
                continue
            try:
                if full.stat().st_size > 2_000_000:
                    continue
                text = full.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for i, line in enumerate(text.splitlines(), start=1):
                if rx.search(line):
                    hits.append(f"{rel}:{i}: {line.strip()[:200]}")
                    if len(hits) >= max_results:
                        return _truncate("\n".join(hits))
    return _truncate("\n".join(hits)) if hits else f"no match for {pattern!r}"


def tool_find_tests_for(ctx: ToolContext, *, symbol: str) -> str:
    """Does any test in the change (or the checkout) reference `symbol`?

    This is the tool that lets the agent *verify* a "missing test" claim instead
    of asserting it, which is the difference between a finding and a guess.
    """
    symbol = symbol.strip()
    if not symbol:
        raise ToolError("symbol must not be empty")
    rx = re.compile(rf"\b{re.escape(symbol)}\b")

    in_diff: list[str] = []
    for fd in ctx.files.values():
        if not fd.is_test_file:
            continue
        for hunk in fd.hunks:
            for lineno, text in hunk.added_lines():
                if rx.search(text):
                    in_diff.append(f"{fd.path}:{lineno}: {text.strip()[:160]}")

    report = [
        f"References to `{symbol}` in test files touched by this change: "
        f"{len(in_diff)}"
    ]
    report.extend(f"  {h}" for h in in_diff[:10])

    if ctx.repo_root is not None:
        try:
            existing = tool_grep_repo(ctx, pattern=rx.pattern, max_results=15,
                                      path_glob="**/test_*.py")
            report.append(f"\nExisting tests in the checkout:\n{existing}")
        except ToolError as exc:
            report.append(f"\n(checkout search unavailable: {exc})")
    return _truncate("\n".join(report))


def tool_list_changed_files(ctx: ToolContext) -> str:
    """The whole change, so the agent can tell whether tests were added at all."""
    rows = [
        f"{fd.path} ({fd.status}, +{fd.additions}/-{fd.deletions})"
        f"{' [test]' if fd.is_test_file else ''}"
        for fd in sorted(ctx.files.values(), key=lambda f: f.path)
    ]
    tests = sum(1 for fd in ctx.files.values() if fd.is_test_file)
    return _truncate(
        f"{len(rows)} reviewable files, {tests} of them test files:\n" + "\n".join(rows)
    )


# --------------------------------------------------------------------------- #
# Registry + JSON schemas advertised to the model
# --------------------------------------------------------------------------- #

@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    fn: Callable[..., str]


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "get_diff_hunks",
        "Return the diff of one changed file, annotated with the real line "
        "numbers of the new file. Use it to cite correct line numbers.",
        {"type": "object",
         "properties": {"path": {"type": "string", "description": "Path of a changed file."}},
         "required": ["path"]},
        tool_get_diff_hunks,
    ),
    ToolSpec(
        "get_file_context",
        "Return source lines around a position in the current checkout, to see "
        "the code surrounding a hunk (imports, enclosing function, callers).",
        {"type": "object",
         "properties": {
             "path": {"type": "string"},
             "around_line": {"type": "integer", "description": "Line to centre on."},
             "radius": {"type": "integer", "description": "Lines of context each side (max 200)."},
         },
         "required": ["path"]},
        tool_get_file_context,
    ),
    ToolSpec(
        "grep_repo",
        "Search the checkout with a Python regular expression. Use it to find "
        "callers of a changed function before claiming a breaking change.",
        {"type": "object",
         "properties": {
             "pattern": {"type": "string"},
             "max_results": {"type": "integer"},
             "path_glob": {"type": "string", "description": "Optional glob, e.g. '**/test_*.py'."},
         },
         "required": ["pattern"]},
        tool_grep_repo,
    ),
    ToolSpec(
        "find_tests_for",
        "Check whether any test references a symbol. Call this before reporting "
        "a missing test, otherwise the claim is unverified.",
        {"type": "object",
         "properties": {"symbol": {"type": "string", "description": "Function or class name."}},
         "required": ["symbol"]},
        tool_find_tests_for,
    ),
    ToolSpec(
        "list_changed_files",
        "List every file in this change, marking test files. Use it to judge "
        "whether the change includes tests at all.",
        {"type": "object", "properties": {}},
        tool_list_changed_files,
    ),
)

TOOLS_BY_NAME: dict[str, ToolSpec] = {t.name: t for t in TOOLS}


def openai_tool_schemas(names: Sequence[str] | None = None) -> list[dict[str, Any]]:
    """The `tools` payload for /v1/chat/completions."""
    selected = TOOLS if names is None else [TOOLS_BY_NAME[n] for n in names]
    return [
        {"type": "function",
         "function": {"name": t.name, "description": t.description,
                      "parameters": t.parameters}}
        for t in selected
    ]


def execute_tool(ctx: ToolContext, name: str, arguments: dict[str, Any]) -> str:
    """Run a tool, turning every failure into text the model can recover from.

    Raising here would abort the whole file's review over a hallucinated
    argument; handing the error back lets the agent correct itself on the next
    step, which is the behaviour the loop is built around.
    """
    spec = TOOLS_BY_NAME.get(name)
    if spec is None:
        return (f"ERROR: unknown tool {name!r}. "
                f"Available: {', '.join(sorted(TOOLS_BY_NAME))}")
    allowed = set(spec.parameters.get("properties", {}))
    unknown = set(arguments) - allowed
    kwargs = {k: v for k, v in arguments.items() if k in allowed}
    missing = [r for r in spec.parameters.get("required", []) if r not in kwargs]
    if missing:
        return (f"ERROR: tool {name!r} requires {missing}; "
                f"got {sorted(kwargs)}. Schema: {json.dumps(spec.parameters)}")
    try:
        out = spec.fn(ctx, **kwargs)
    except ToolError as exc:
        return f"ERROR: {exc}"
    except Exception as exc:  # defensive: a tool bug must not kill the review
        return f"ERROR: tool {name!r} failed with {type(exc).__name__}: {exc}"
    if unknown:
        out = f"(ignored unknown arguments: {sorted(unknown)})\n{out}"
    return out
