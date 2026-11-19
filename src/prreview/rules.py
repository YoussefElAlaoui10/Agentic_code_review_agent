"""A small deterministic reviewer used for two distinct purposes.

1. It backs `MockLLM`, so the agent loop, the orchestrator, the judge and the
   benchmarks are all runnable and testable with no API key and no network.
2. It is a legitimate *baseline* in the report: a rule-based linter is the thing
   an LLM reviewer has to beat, so the quality benchmark can score both and the
   comparison means something.

Every rule looks only at lines the diff *adds*, which is what a reviewer bot
should do: flagging pre-existing code on an unrelated PR is noise.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Iterator

from .models import FileDiff


@dataclass(frozen=True, slots=True)
class RuleHit:
    line: int
    severity: str
    category: str
    title: str
    justification: str
    confidence: float
    suggestion: str = ""
    rule_id: str = ""


@dataclass(frozen=True, slots=True)
class Rule:
    rule_id: str
    pattern: re.Pattern[str]
    severity: str
    category: str
    title: str
    justification: str
    confidence: float
    suggestion: str = ""
    languages: tuple[str, ...] = ("python",)
    # Extra predicate over (line_text, file_context); None means "pattern is enough".
    guard: Callable[[str, "FileContext"], bool] | None = None


@dataclass(slots=True)
class FileContext:
    """Cheap whole-file facts the rules need (is this line inside an `async def`?)."""

    path: str
    async_line_ranges: tuple[tuple[int, int], ...] = ()

    def in_async_def(self, line: int) -> bool:
        return any(lo <= line <= hi for lo, hi in self.async_line_ranges)


def _async_ranges(fd: FileDiff) -> tuple[tuple[int, int], ...]:
    """Find line spans that belong to an `async def`, by indentation.

    Operates on the diff's own context+added lines, so it is approximate: a hunk
    that starts mid-function cannot see the enclosing `async def`. Documented as
    a known limit rather than papered over.
    """
    ranges: list[tuple[int, int]] = []
    for hunk in fd.hunks:
        lineno = hunk.new_start
        open_at: int | None = None
        open_indent = 0
        for raw in hunk.lines:
            if raw.startswith("-"):
                continue
            text = raw[1:] if raw[:1] in "+ " else raw
            indent = len(text) - len(text.lstrip())
            stripped = text.strip()
            if stripped.startswith("async def "):
                if open_at is not None:
                    ranges.append((open_at, lineno - 1))
                open_at, open_indent = lineno, indent
            elif (open_at is not None and stripped
                  and indent <= open_indent
                  and not stripped.startswith(("#", ")", "]", "}"))):
                ranges.append((open_at, lineno - 1))
                open_at = None
            lineno += 1
        if open_at is not None:
            ranges.append((open_at, lineno))
    return tuple(ranges)


def _is_async(text: str, ctx: FileContext) -> bool:  # guard helper
    return True


RULES: tuple[Rule, ...] = (
    Rule("bare-except", re.compile(r"^\s*except\s*:"), "medium", "bug",
         "Bare `except:` swallows every exception",
         "A bare except also catches KeyboardInterrupt and SystemExit, so the "
         "process stops being interruptible and real errors are hidden.",
         0.86, "Catch the specific exception, or `except Exception:` at minimum."),
    Rule("eval-exec", re.compile(r"\b(eval|exec)\s*\("), "high", "bug",
         "`eval`/`exec` on runtime data is a code-execution risk",
         "If any part of the argument comes from user input, request data or a "
         "file, this executes arbitrary code in the process.",
         0.82, "Use `ast.literal_eval`, `json.loads`, or an explicit dispatch dict."),
    Rule("mutable-default", re.compile(r"def\s+\w+\s*\([^)]*=\s*(\[\]|\{\}|set\(\))"),
         "high", "bug",
         "Mutable default argument is shared across calls",
         "Default arguments are evaluated once at function definition, so the "
         "same list/dict is reused by every call and accumulates state.",
         0.9, "Default to `None` and build the container inside the function."),
    Rule("shell-true", re.compile(r"shell\s*=\s*True"), "high", "bug",
         "`shell=True` enables shell injection",
         "With shell=True the command string is interpreted by the shell, so any "
         "interpolated value can inject further commands.",
         0.84, "Pass a list of arguments and leave shell=False."),
    Rule("blocking-sleep-async", re.compile(r"\btime\.sleep\s*\("), "high", "perf",
         "Blocking `time.sleep` inside a coroutine stalls the event loop",
         "time.sleep blocks the thread running the loop, so every other task on "
         "that loop is frozen for the duration: the concurrency is fake.",
         0.88, "Use `await asyncio.sleep(...)`.",
         guard=lambda text, ctx: True),
    Rule("blocking-requests-async", re.compile(r"\brequests\.(get|post|put|delete|patch)\s*\("),
         "high", "perf",
         "Synchronous HTTP call inside a coroutine blocks the event loop",
         "`requests` is blocking; called from a coroutine it serialises all "
         "concurrent tasks on the same loop.",
         0.85, "Use an async client, or wrap the call in `asyncio.to_thread`.",
         guard=lambda text, ctx: True),
    Rule("open-no-with", re.compile(r"(?<![\w.])open\s*\("), "medium", "bug",
         "File opened without a context manager may leak the handle",
         "Without `with`, an exception before `.close()` leaves the descriptor "
         "open until the garbage collector runs.",
         0.6, "Wrap it in `with open(...) as fh:`.",
         guard=lambda text, ctx: "with " not in text and "=" in text),
    Rule("compare-none", re.compile(r"[!=]=\s*None"), "low", "style",
         "Compare to None with `is` / `is not`",
         "`== None` goes through __eq__, which a class can override; identity is "
         "the intended test and is what PEP 8 prescribes.",
         0.78, "Use `is None` / `is not None`."),
    Rule("assert-in-prod", re.compile(r"^\s*assert\s+"), "medium", "bug",
         "`assert` used for runtime validation is stripped under -O",
         "Running Python with -O removes assert statements entirely, so this "
         "check silently disappears in optimised deployments.",
         0.55, "Raise an explicit exception instead.",
         guard=lambda text, ctx: not ("test" in ctx.path or "/tests/" in f"/{ctx.path}")),
    Rule("lock-no-finally", re.compile(r"\.acquire\s*\(\s*\)"), "high", "bug",
         "Lock acquired without a guaranteed release",
         "If the guarded block raises, `.release()` is never reached and every "
         "other thread blocks on that lock forever: a deadlock.",
         0.72, "Use the lock as a context manager (`with lock:`) or try/finally."),
    Rule("print-debug", re.compile(r"^\s*print\s*\("), "low", "style",
         "Leftover `print` in library code",
         "Library code should emit through `logging`, so the caller controls "
         "verbosity and destination.",
         0.5, "Use a module-level `logging.getLogger(__name__)`.",
         guard=lambda text, ctx: not ("test" in ctx.path or "cli" in ctx.path
                                      or "__main__" in ctx.path)),
    Rule("todo-left", re.compile(r"#\s*(TODO|FIXME|XXX|HACK)\b"), "low", "style",
         "TODO/FIXME introduced by this change",
         "A marker added in the same PR usually means the change is knowingly "
         "incomplete; it should be an issue or be resolved before merge.",
         0.45, "Link a tracking issue or finish the case."),
    Rule("broad-timeout-missing",
         re.compile(r"urlopen\s*\([^)]*\)|\.request\s*\([^)]*\)"), "medium", "bug",
         "Network call without an explicit timeout",
         "With no timeout the call can hang indefinitely, pinning a worker or a "
         "coroutine until the peer closes the socket.",
         0.5, "Pass an explicit `timeout=`.",
         guard=lambda text, ctx: "timeout" not in text),
)


def _needs_async_ctx(rule: Rule) -> bool:
    return rule.rule_id in {"blocking-sleep-async", "blocking-requests-async"}


def analyse_file(fd: FileDiff) -> list[RuleHit]:
    """Run every rule over the lines this diff adds."""
    if fd.language != "python" or fd.status == "removed":
        return []
    ctx = FileContext(path=fd.path, async_line_ranges=_async_ranges(fd))
    hits: list[RuleHit] = []
    seen: set[tuple[str, int]] = set()

    for hunk in fd.hunks:
        for lineno, text in hunk.added_lines():
            code = text.split("#", 1)[0] if not text.lstrip().startswith("#") else text
            for rule in RULES:
                if (rule.rule_id, lineno) in seen:
                    continue
                target = text if rule.rule_id == "todo-left" else code
                if not rule.pattern.search(target):
                    continue
                if _needs_async_ctx(rule) and not ctx.in_async_def(lineno):
                    continue
                if rule.guard is not None and not rule.guard(text, ctx):
                    continue
                seen.add((rule.rule_id, lineno))
                hits.append(RuleHit(
                    line=lineno, severity=rule.severity, category=rule.category,
                    title=rule.title, justification=rule.justification,
                    confidence=rule.confidence, suggestion=rule.suggestion,
                    rule_id=rule.rule_id,
                ))
    hits.extend(_missing_test_hits(fd))
    return sorted(hits, key=lambda h: (h.line, h.rule_id))


_DEF_RE = re.compile(r"^\s*(?:async\s+)?def\s+([a-zA-Z_]\w*)")


def new_public_functions(fd: FileDiff) -> list[tuple[int, str]]:
    """Public functions introduced by this diff (used by the missing-test rule)."""
    out: list[tuple[int, str]] = []
    for hunk in fd.hunks:
        for lineno, text in hunk.added_lines():
            m = _DEF_RE.match(text)
            if m and not m.group(1).startswith("_"):
                out.append((lineno, m.group(1)))
    return out


def _missing_test_hits(fd: FileDiff) -> Iterator[RuleHit]:
    """Flag new public functions when the same diff touches no test file.

    The orchestrator knows the whole file list, so it refines this; on its own a
    single-file view can only say "this file adds an untested entry point".
    """
    if fd.is_test_file:
        return
    for lineno, name in new_public_functions(fd):
        yield RuleHit(
            line=lineno, severity="medium", category="test",
            title=f"New public function `{name}` has no test in this change",
            justification=(
                f"`{name}` is introduced by this PR and no test file in the same "
                "change references it, so the new behaviour is unverified."
            ),
            confidence=0.52,
            suggestion=f"Add a unit test covering `{name}`'s expected result and one edge case.",
            rule_id="missing-test",
        )
