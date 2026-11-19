"""The reviewer agent: one agent instance per (file, role).

The loop is the point of the project. Each step the model may either call a tool
or answer; it keeps control of the order and of when it has seen enough. The
loop enforces only the invariants the model cannot be trusted with: a step
budget, a tool-call budget, JSON validation, and a self-check pass that drops
findings whose cited line the diff never touched.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any, Sequence

from .diffparse import numbered_patch
from .llm import LLMClient, LLMError
from .models import Finding, FileDiff, InvalidFinding, ReviewResult
from .tools import ToolContext, execute_tool, openai_tool_schemas

MAX_STEPS = 6
MAX_TOOL_CALLS = 8

ROLES: dict[str, str] = {
    "correctness": (
        "You hunt for behaviour that is simply wrong: logic errors, off-by-one, "
        "unhandled None, wrong operator, resource leaks, race conditions, "
        "exceptions that escape or get swallowed."
    ),
    "performance": (
        "You hunt for cost: needless O(n^2), repeated work in a loop, blocking "
        "calls inside coroutines (time.sleep, requests, file I/O on the event "
        "loop), unbounded memory growth, missing batching."
    ),
    "tests": (
        "You hunt for unverified behaviour: new or changed logic with no test, "
        "edge cases left uncovered, assertions that cannot fail. Always call "
        "find_tests_for before reporting a missing test."
    ),
    "style": (
        "You hunt for things that will cost the next reader time: misleading "
        "names, dead code, duplicated logic, comparisons to None, leftover "
        "prints and TODOs. Never report formatting a linter would fix."
    ),
}

DEFAULT_ROLES: tuple[str, ...] = ("correctness", "performance", "tests")

_SYSTEM = """You are a precise code reviewer. [role:{role}]

{role_brief}

You are reviewing ONE file of a pull request. Work like this:
1. Call tools to gather what you need. You decide which and how many.
2. Before claiming a missing test, call find_tests_for and use what it returns.
3. Only then answer.

Rules that are not negotiable:
- Review ONLY lines this diff adds or changes. Pre-existing code is out of scope.
- `line` must be a line number shown in the annotated diff, never a guess.
- Report nothing rather than something speculative. An empty list is a valid,
  good answer for a clean diff.
- `confidence` is your own probability that a maintainer would act on the
  finding. Be calibrated: use below 0.5 when you are unsure.

Answer with JSON only, no prose and no code fence:
{{"findings": [{{"path": "...", "line": 12, "severity": "high|medium|low",
  "category": "bug|style|test|perf", "title": "one line",
  "justification": "why this is a problem, concretely",
  "confidence": 0.0, "suggestion": "what to do instead"}}]}}"""

_USER = """File: {path}
Status: {status} (+{additions}/-{deletions})
Language: {language}

Annotated diff (line numbers are those of the new file):
{patch}

Review this file now."""


class ReviewerAgent:
    """One agent: a role, a model, a tool loop, and a validated list of findings."""

    def __init__(
        self,
        client: LLMClient,
        *,
        role: str = "correctness",
        max_steps: int = MAX_STEPS,
        max_tool_calls: int = MAX_TOOL_CALLS,
        temperature: float = 0.1,
    ) -> None:
        if role not in ROLES:
            raise ValueError(f"unknown role {role!r}; known: {sorted(ROLES)}")
        self.client = client
        self.role = role
        self.max_steps = max_steps
        self.max_tool_calls = max_tool_calls
        self.temperature = temperature

    async def review(self, fd: FileDiff, ctx: ToolContext) -> ReviewResult:
        """Run the loop for one file. Never raises: failures land in `error`."""
        started = time.perf_counter()
        result = ReviewResult(path=fd.path, reviewer=self.role)
        messages: list[dict[str, Any]] = [
            {"role": "system",
             "content": _SYSTEM.format(role=self.role, role_brief=ROLES[self.role])},
            {"role": "user",
             "content": _USER.format(
                 path=fd.path, status=fd.status, additions=fd.additions,
                 deletions=fd.deletions, language=fd.language,
                 patch=numbered_patch(fd))},
        ]
        schemas = openai_tool_schemas()
        tool_calls_made = 0

        try:
            for step in range(1, self.max_steps + 1):
                result.steps = step
                offer_tools = schemas if tool_calls_made < self.max_tool_calls else None
                response = await self.client.chat(
                    messages, tools=offer_tools, temperature=self.temperature
                )
                result.prompt_tokens += response.prompt_tokens
                result.completion_tokens += response.completion_tokens

                if response.wants_tools and offer_tools is not None:
                    messages.append({
                        "role": "assistant",
                        "content": response.text or None,
                        "tool_calls": [
                            {"id": tc.id, "type": "function",
                             "function": {"name": tc.name,
                                          "arguments": json.dumps(tc.arguments)}}
                            for tc in response.tool_calls
                        ],
                    })
                    for tc in response.tool_calls:
                        tool_calls_made += 1
                        result.tool_calls.append(tc.name)
                        output = execute_tool(ctx, tc.name, tc.arguments)
                        messages.append({"role": "tool", "tool_call_id": tc.id,
                                         "name": tc.name, "content": output})
                    continue

                parsed = _parse_findings(response.text)
                if parsed is None:
                    messages.append({"role": "assistant", "content": response.text})
                    messages.append({
                        "role": "user",
                        "content": ("That was not valid JSON. Reply with only the "
                                    'JSON object {"findings": [...]}, nothing else.'),
                    })
                    continue

                result.findings = self._validate(parsed, fd)
                break
            else:
                result.error = f"step budget exhausted after {self.max_steps} steps"

        except LLMError as exc:
            result.error = str(exc)
        except Exception as exc:  # one bad file must not sink the whole run
            result.error = f"{type(exc).__name__}: {exc}"

        result.latency_s = time.perf_counter() - started
        return result

    def _validate(self, raw_findings: Sequence[Any], fd: FileDiff) -> list[Finding]:
        """Coerce, then drop findings the diff cannot support.

        The self-check that matters: a line number the diff never touched means
        the model is reviewing imagined code, so the finding goes. This is the
        single biggest source of false positives in a naive implementation.
        """
        touched = {ln for h in fd.hunks for ln, _ in h.added_lines()}
        out: list[Finding] = []
        for raw in raw_findings:
            try:
                finding = Finding.from_raw(raw, path=fd.path, reviewer=self.role)
            except InvalidFinding:
                continue
            if finding.path != fd.path:
                finding = Finding(**{**finding.to_dict(), "path": fd.path,
                                     "agreed_by": ()})
            if touched and finding.line not in touched:
                nearest = min(touched, key=lambda t: abs(t - finding.line))
                if abs(nearest - finding.line) > 3:
                    continue  # hallucinated location
                finding = Finding(**{**finding.to_dict(), "line": nearest,
                                     "agreed_by": ()})
            out.append(finding)
        return out


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def _parse_findings(text: str) -> list[Any] | None:
    """Pull the findings list out of a model answer, tolerating the usual noise."""
    if not text or not text.strip():
        return []
    candidates: list[str] = []
    fenced = _FENCE_RE.search(text)
    if fenced:
        candidates.append(fenced.group(1))
    candidates.append(text)
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        candidates.append(text[start:end + 1])
    start, end = text.find("["), text.rfind("]")
    if 0 <= start < end:
        candidates.append(text[start:end + 1])

    for candidate in candidates:
        try:
            data = json.loads(candidate.strip())
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict):
            for key in ("findings", "issues", "comments", "results"):
                if isinstance(data.get(key), list):
                    return data[key]
            if {"severity", "category"} & set(data):
                return [data]
            if not data:
                return []
        elif isinstance(data, list):
            return data
    return None
