"""Typed data model shared by the reviewers, the judge and the benchmarks.

Everything the agents exchange is a plain dataclass with strict validation at the
boundary: an LLM returns free-form JSON, `Finding.from_raw` is the only place
where that untrusted shape is turned into a typed object.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal

Severity = Literal["high", "medium", "low"]
Category = Literal["bug", "style", "test", "perf"]

SEVERITIES: tuple[Severity, ...] = ("high", "medium", "low")
CATEGORIES: tuple[Category, ...] = ("bug", "style", "test", "perf")

SEVERITY_RANK: dict[str, int] = {"high": 3, "medium": 2, "low": 1}

# Filler words only; anything that could identify a symbol is kept.
_STOPWORDS: frozenset[str] = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in", "is",
    "it", "its", "of", "on", "or", "that", "the", "this", "to", "with",
})


class InvalidFinding(ValueError):
    """Raised when an LLM answer cannot be coerced into a Finding."""


@dataclass(frozen=True, slots=True)
class Hunk:
    """One @@ block of a unified diff, with absolute line numbers in the new file."""

    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: tuple[str, ...]

    def added_lines(self) -> list[tuple[int, str]]:
        """Return (line_number_in_new_file, text) for every added line."""
        out: list[tuple[int, str]] = []
        lineno = self.new_start
        for raw in self.lines:
            if raw.startswith("+"):
                out.append((lineno, raw[1:]))
                lineno += 1
            elif raw.startswith("-"):
                continue
            else:
                lineno += 1
        return out

    def header(self) -> str:
        return f"@@ -{self.old_start},{self.old_count} +{self.new_start},{self.new_count} @@"


@dataclass(frozen=True, slots=True)
class FileDiff:
    """The unit of work: one changed file, reviewed by exactly one reviewer agent."""

    path: str
    status: str = "modified"
    additions: int = 0
    deletions: int = 0
    patch: str = ""
    hunks: tuple[Hunk, ...] = ()

    @property
    def language(self) -> str:
        ext = self.path.rsplit(".", 1)[-1].lower() if "." in self.path else ""
        return {
            "py": "python", "pyi": "python", "js": "javascript", "ts": "typescript",
            "tsx": "typescript", "jsx": "javascript", "rs": "rust", "go": "go",
            "c": "c", "h": "c", "cpp": "cpp", "java": "java", "rb": "ruby",
            "sh": "shell", "md": "markdown", "qmd": "markdown", "yml": "yaml",
            "yaml": "yaml", "json": "json", "toml": "toml",
        }.get(ext, ext or "text")

    @property
    def is_test_file(self) -> bool:
        base = self.path.rsplit("/", 1)[-1]
        return base.startswith("test_") or base.endswith("_test.py") or "/tests/" in f"/{self.path}"

    @property
    def cost_hint(self) -> int:
        """Rough work estimate, used to schedule the biggest files first."""
        return self.additions + self.deletions + len(self.patch) // 80


@dataclass(frozen=True, slots=True)
class Finding:
    """A single review remark. `confidence` drives the judge's filtering."""

    path: str
    line: int
    severity: Severity
    category: Category
    title: str
    justification: str
    confidence: float = 0.5
    suggestion: str = ""
    reviewer: str = "unknown"
    agreed_by: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.severity not in SEVERITIES:
            raise InvalidFinding(f"severity {self.severity!r} not in {SEVERITIES}")
        if self.category not in CATEGORIES:
            raise InvalidFinding(f"category {self.category!r} not in {CATEGORIES}")
        if not 0.0 <= self.confidence <= 1.0:
            raise InvalidFinding(f"confidence {self.confidence!r} outside [0, 1]")
        if not self.title.strip():
            raise InvalidFinding("title must not be empty")
        if self.line < 0:
            raise InvalidFinding(f"line {self.line} must be >= 0")

    @property
    def severity_rank(self) -> int:
        return SEVERITY_RANK[self.severity]

    def dedupe_key(self) -> str:
        """Identity used by the judge: same file, same category, same normalised title.

        Word order and grammar vary between reviewers describing one defect, so
        the title is reduced to a sorted bag of significant words. Only known
        filler words are removed: an earlier version dropped every token of
        three characters or fewer, which silently merged distinct findings whose
        titles differed only by a short identifier ("missing test for `foo`" vs
        "... for `bar`").
        """
        norm = re.sub(r"[^a-z0-9 ]+", " ", self.title.lower())
        words = sorted(w for w in norm.split() if w not in _STOPWORDS)
        return f"{self.path}|{self.category}|{' '.join(words)}"

    def fingerprint(self) -> str:
        return hashlib.sha256(
            f"{self.path}|{self.line}|{self.category}|{self.title}".encode()
        ).hexdigest()[:12]

    def to_dict(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        d["agreed_by"] = list(self.agreed_by)
        return d

    @classmethod
    def from_raw(cls, raw: Any, *, path: str, reviewer: str = "unknown") -> "Finding":
        """Coerce one LLM-produced object into a Finding, or raise InvalidFinding."""
        if not isinstance(raw, dict):
            raise InvalidFinding(f"expected an object, got {type(raw).__name__}")

        def pick(*names: str, default: Any = None) -> Any:
            for n in names:
                if n in raw and raw[n] is not None:
                    return raw[n]
            return default

        severity = str(pick("severity", "level", default="medium")).strip().lower()
        severity = {"critical": "high", "major": "high", "minor": "low",
                    "info": "low", "warning": "medium"}.get(severity, severity)

        category = str(pick("category", "type", "kind", default="bug")).strip().lower()
        category = {"bugs": "bug", "defect": "bug", "correctness": "bug",
                    "tests": "test", "testing": "test", "missing_test": "test",
                    "performance": "perf", "efficiency": "perf",
                    "readability": "style", "lint": "style", "nit": "style"}.get(category, category)

        try:
            line = int(pick("line", "line_number", "lineno", default=0) or 0)
        except (TypeError, ValueError):
            line = 0

        try:
            confidence = float(pick("confidence", "score", default=0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        confidence = min(1.0, max(0.0, confidence))

        title = str(pick("title", "summary", "message", default="")).strip()
        justification = str(pick("justification", "rationale", "explanation",
                                 "description", default="")).strip()
        if not title and justification:
            title = justification.split(".")[0][:90]

        return cls(
            path=str(pick("path", "file", default=path)) or path,
            line=max(0, line),
            severity=severity,  # type: ignore[arg-type]
            category=category,  # type: ignore[arg-type]
            title=title,
            justification=justification,
            confidence=confidence,
            suggestion=str(pick("suggestion", "fix", default="")).strip(),
            reviewer=reviewer,
        )


@dataclass(slots=True)
class ReviewResult:
    """What one reviewer agent returns for one file, including its own trace."""

    path: str
    findings: list[Finding] = field(default_factory=list)
    reviewer: str = "unknown"
    steps: int = 0
    tool_calls: list[str] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_s: float = 0.0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "reviewer": self.reviewer,
            "steps": self.steps,
            "tool_calls": self.tool_calls,
            "latency_s": round(self.latency_s, 4),
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "error": self.error,
            "findings": [f.to_dict() for f in self.findings],
        }


@dataclass(slots=True)
class RunReport:
    """Everything one `review` invocation produced, serialisable to JSON."""

    target: str
    mode: str
    concurrency: int
    wall_clock_s: float
    files_reviewed: int
    results: list[ReviewResult] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    judge_stats: dict[str, Any] = field(default_factory=dict)
    model: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "mode": self.mode,
            "model": self.model,
            "concurrency": self.concurrency,
            "wall_clock_s": round(self.wall_clock_s, 4),
            "files_reviewed": self.files_reviewed,
            "judge_stats": self.judge_stats,
            "findings": [f.to_dict() for f in self.findings],
            "results": [r.to_dict() for r in self.results],
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)


def sort_findings(findings: Iterable[Finding]) -> list[Finding]:
    """Most severe first, then most confident, then by file and line."""
    return sorted(
        findings,
        key=lambda f: (-f.severity_rank, -f.confidence, f.path, f.line),
    )
