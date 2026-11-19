"""The judge agent: turn N reviewers' raw findings into one publishable list.

Four reviewers over M files produce overlapping, partly speculative output. The
judge is what makes that usable:

  1. deduplicate   -- the same problem reported by several roles, or twice by one
  2. corroborate   -- agreement across independent roles raises confidence
  3. filter        -- drop what is below the confidence floor
  4. cap           -- a review with 80 comments gets ignored by humans

Steps 1-4 are deterministic and testable. An optional LLM pass can then rewrite
the surviving titles, but it is never allowed to invent or re-rank findings:
`llm_polish` matches rewrites back to fingerprints and discards anything else.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .llm import LLMClient, LLMError
from .models import Finding, ReviewResult, SEVERITY_RANK, sort_findings

LINE_WINDOW = 3


@dataclass(slots=True)
class JudgeConfig:
    min_confidence: float = 0.45
    max_findings: int = 25
    agreement_bonus: float = 0.12
    # A role reporting outside its specialty is weaker evidence.
    off_role_penalty: float = 0.0
    keep_high_severity: bool = True
    line_window: int = LINE_WINDOW


@dataclass(slots=True)
class JudgeStats:
    raw: int = 0
    after_dedupe: int = 0
    after_filter: int = 0
    published: int = 0
    dropped_low_confidence: int = 0
    dropped_over_cap: int = 0
    merged_groups: int = 0
    corroborated: int = 0
    by_category: dict[str, int] = field(default_factory=dict)
    by_severity: dict[str, int] = field(default_factory=dict)
    failed_reviewers: list[str] = field(default_factory=list)
    llm_polished: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw": self.raw,
            "after_dedupe": self.after_dedupe,
            "after_filter": self.after_filter,
            "published": self.published,
            "dropped_low_confidence": self.dropped_low_confidence,
            "dropped_over_cap": self.dropped_over_cap,
            "merged_groups": self.merged_groups,
            "corroborated": self.corroborated,
            "by_category": self.by_category,
            "by_severity": self.by_severity,
            "failed_reviewers": self.failed_reviewers,
            "llm_polished": self.llm_polished,
        }


class JudgeAgent:
    """Deterministic fusion, with an optional and tightly-bounded LLM pass."""

    def __init__(self, config: JudgeConfig | None = None,
                 client: LLMClient | None = None) -> None:
        self.config = config or JudgeConfig()
        self.client = client

    # -- public API -------------------------------------------------------- #

    def judge(self, results: Sequence[ReviewResult]) -> tuple[list[Finding], JudgeStats]:
        stats = JudgeStats()
        findings: list[Finding] = []
        for r in results:
            if not r.ok:
                stats.failed_reviewers.append(f"{r.path} [{r.reviewer}]: {r.error}")
            findings.extend(r.findings)
        stats.raw = len(findings)

        merged = self._dedupe(findings, stats)
        stats.after_dedupe = len(merged)

        kept = self._filter(merged, stats)
        stats.after_filter = len(kept)

        ordered = sort_findings(kept)
        if len(ordered) > self.config.max_findings:
            stats.dropped_over_cap = len(ordered) - self.config.max_findings
            ordered = ordered[: self.config.max_findings]

        stats.published = len(ordered)
        for f in ordered:
            stats.by_category[f.category] = stats.by_category.get(f.category, 0) + 1
            stats.by_severity[f.severity] = stats.by_severity.get(f.severity, 0) + 1
        return ordered, stats

    async def polish(self, findings: Sequence[Finding],
                     stats: JudgeStats) -> list[Finding]:
        """Optionally let the model tighten titles. Cannot add or drop findings."""
        if self.client is None or not findings:
            return list(findings)
        payload = [
            {"id": f.fingerprint(), "title": f.title,
             "justification": f.justification, "category": f.category}
            for f in findings
        ]
        messages = [
            {"role": "system",
             "content": ("You are a review editor. Rewrite each finding's title to "
                         "be one specific, actionable sentence. Keep the meaning and "
                         "the id. Do not add, remove, merge or reorder findings. "
                         'Answer JSON only: {"findings":[{"id":"...","title":"..."}]}')},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        try:
            response = await self.client.chat(messages, temperature=0.0, max_tokens=1200)
            data = json.loads(response.text)
        except (LLMError, json.JSONDecodeError, TypeError, ValueError):
            return list(findings)  # polishing is cosmetic; never fail the run on it

        rewrites: dict[str, str] = {}
        for item in (data.get("findings") if isinstance(data, dict) else None) or []:
            if isinstance(item, dict) and item.get("id") and item.get("title"):
                rewrites[str(item["id"])] = str(item["title"]).strip()

        out: list[Finding] = []
        for f in findings:
            new_title = rewrites.get(f.fingerprint())
            if new_title and new_title != f.title:
                out.append(Finding(**{**f.to_dict(), "title": new_title,
                                      "agreed_by": f.agreed_by}))
                stats.llm_polished += 1
            else:
                out.append(f)
        return out

    # -- internals --------------------------------------------------------- #

    def _dedupe(self, findings: Iterable[Finding], stats: JudgeStats) -> list[Finding]:
        """Group findings that describe the same problem, keep the best one.

        Grouped when they share file + category and either the normalised title
        or a line within `line_window` -- roles cite slightly different lines for
        the same defect, so exact-line matching would under-merge.
        """
        groups: list[list[Finding]] = []
        for f in findings:
            for group in groups:
                head = group[0]
                if head.path != f.path or head.category != f.category:
                    continue
                same_title = head.dedupe_key() == f.dedupe_key()
                close_line = abs(head.line - f.line) <= self.config.line_window
                if same_title or close_line:
                    group.append(f)
                    break
            else:
                groups.append([f])

        merged: list[Finding] = []
        for group in groups:
            if len(group) > 1:
                stats.merged_groups += 1
            merged.append(self._merge_group(group, stats))
        return merged

    def _merge_group(self, group: list[Finding], stats: JudgeStats) -> Finding:
        """Keep the most severe/confident member; raise confidence on agreement."""
        best = max(group, key=lambda f: (f.severity_rank, f.confidence,
                                         len(f.justification)))
        reviewers = {f.reviewer for f in group}
        others = tuple(sorted(reviewers - {best.reviewer}))

        confidence = best.confidence
        if others:
            stats.corroborated += 1
            # Each independent corroborating role adds a bounded bonus; the
            # result is capped so agreement can never fabricate certainty.
            confidence = min(0.99, confidence + self.config.agreement_bonus * len(others))

        severity = max(group, key=lambda f: f.severity_rank).severity
        return Finding(
            path=best.path, line=best.line, severity=severity,
            category=best.category, title=best.title,
            justification=best.justification, confidence=round(confidence, 3),
            suggestion=best.suggestion or next((f.suggestion for f in group
                                                if f.suggestion), ""),
            reviewer=best.reviewer, agreed_by=others,
        )

    def _filter(self, findings: Sequence[Finding], stats: JudgeStats) -> list[Finding]:
        kept: list[Finding] = []
        for f in findings:
            if f.confidence >= self.config.min_confidence:
                kept.append(f)
            elif self.config.keep_high_severity and f.severity == "high" and f.agreed_by:
                # A high-severity defect two roles independently found is worth a
                # human glance even when neither was individually confident.
                kept.append(f)
            else:
                stats.dropped_low_confidence += 1
        return kept
