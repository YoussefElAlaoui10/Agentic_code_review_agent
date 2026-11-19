"""Unified-diff parsing.

Two entry points:
  * `parse_hunks(patch)`   -- the per-file `patch` field the GitHub API returns.
  * `split_unified_diff(t)`-- a whole `git diff` / `.patch` file into FileDiffs,
                              used by the offline `--diff-file` mode.
"""

from __future__ import annotations

import re

from .models import FileDiff, Hunk

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_DIFF_GIT_RE = re.compile(r"^diff --git a/(.+?) b/(.+)$")


def parse_hunks(patch: str) -> tuple[Hunk, ...]:
    """Split a single-file patch body into hunks with absolute line numbers."""
    if not patch:
        return ()
    hunks: list[Hunk] = []
    header: tuple[int, int, int, int] | None = None
    body: list[str] = []

    def flush() -> None:
        if header is not None:
            hunks.append(Hunk(header[0], header[1], header[2], header[3], tuple(body)))

    for line in patch.splitlines():
        m = _HUNK_RE.match(line)
        if m:
            flush()
            body = []
            header = (
                int(m.group(1)), int(m.group(2) or 1),
                int(m.group(3)), int(m.group(4) or 1),
            )
            continue
        if header is None:
            continue  # preamble (index/---/+++ lines)
        if line.startswith(("+++", "---")):
            continue
        if line.startswith("\\"):
            continue  # "\ No newline at end of file"
        body.append(line)
    flush()
    return tuple(hunks)


def split_unified_diff(text: str) -> list[FileDiff]:
    """Parse a multi-file unified diff into one FileDiff per changed file."""
    files: list[FileDiff] = []
    path: str | None = None
    status = "modified"
    buf: list[str] = []

    def flush() -> None:
        if path is None:
            return
        patch = "\n".join(buf)
        hunks = parse_hunks(patch)
        additions = sum(1 for ln in buf if ln.startswith("+") and not ln.startswith("+++"))
        deletions = sum(1 for ln in buf if ln.startswith("-") and not ln.startswith("---"))
        files.append(FileDiff(
            path=path, status=status, additions=additions,
            deletions=deletions, patch=patch, hunks=hunks,
        ))

    for line in text.splitlines():
        m = _DIFF_GIT_RE.match(line)
        if m:
            flush()
            path = m.group(2)
            status = "modified"
            buf = []
            continue
        if path is None:
            continue
        if line.startswith("new file mode"):
            status = "added"
        elif line.startswith("deleted file mode"):
            status = "removed"
        elif line.startswith("rename to "):
            status = "renamed"
        buf.append(line)
    flush()
    return files


def numbered_patch(fd: FileDiff, *, max_lines: int = 400) -> str:
    """Render the patch with new-file line numbers so the LLM can cite real lines.

    Without this the model invents line numbers: it only sees `+`/`-` markers and
    has no way to know where in the file a hunk lands.
    """
    out: list[str] = []
    budget = max_lines
    for h in fd.hunks:
        if budget <= 0:
            out.append("... (patch truncated)")
            break
        out.append(h.header())
        lineno = h.new_start
        for raw in h.lines:
            if budget <= 0:
                out.append("... (hunk truncated)")
                break
            if raw.startswith("+"):
                out.append(f"{lineno:>6} + {raw[1:]}")
                lineno += 1
            elif raw.startswith("-"):
                out.append(f"{'':>6} - {raw[1:]}")
            else:
                out.append(f"{lineno:>6}   {raw[1:] if raw.startswith(' ') else raw}")
                lineno += 1
            budget -= 1
    return "\n".join(out)
