"""Fetching the review target.

`GET /repos/{owner}/{repo}/pulls/{n}/files` is the documented endpoint for a PR's
per-file diffs. It paginates at 100 files per page, and the `patch` field is
omitted for binary files and for very large diffs -- both handled here.

An offline path (`load_diff_file`) reads a `.patch`/`.diff` from disk so the demo
and the benchmarks never depend on network access or on a GitHub token.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass

from .diffparse import parse_hunks, split_unified_diff
from .models import FileDiff

GITHUB_API = os.environ.get("GITHUB_API", "https://api.github.com")
_PR_URL_RE = re.compile(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)")
_PR_SHORT_RE = re.compile(r"^([^/]+)/([^/#]+)(?:#|/pull/)(\d+)$")

# Reviewing a lockfile or a minified bundle burns tokens for nothing.
SKIP_PATTERNS = (
    r"(^|/)(package-lock\.json|yarn\.lock|poetry\.lock|uv\.lock|Cargo\.lock)$",
    r"\.(min\.js|min\.css|map|svg|png|jpg|jpeg|gif|ico|pdf|woff2?|ttf)$",
    r"(^|/)(dist|build|node_modules|vendor|\.venv)/",
)
_SKIP_RE = re.compile("|".join(SKIP_PATTERNS))


class TargetError(RuntimeError):
    """The review target could not be resolved or fetched."""


@dataclass(frozen=True, slots=True)
class PRRef:
    owner: str
    repo: str
    number: int

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}#{self.number}"


def parse_pr_target(target: str) -> PRRef:
    """Accept a full URL, `owner/repo#123` or `owner/repo/pull/123`."""
    target = target.strip()
    m = _PR_URL_RE.search(target) or _PR_SHORT_RE.match(target)
    if not m:
        raise TargetError(
            f"cannot parse PR target {target!r}; "
            "use https://github.com/owner/repo/pull/123 or owner/repo#123"
        )
    return PRRef(m.group(1), m.group(2).removesuffix(".git"), int(m.group(3)))


def should_skip(path: str) -> bool:
    return bool(_SKIP_RE.search(path))


def _request_json(url: str, token: str | None) -> tuple[list | dict, dict[str, str]]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "prreview-agent/0.1",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            return payload, dict(resp.headers)
    except urllib.error.HTTPError as exc:  # pragma: no cover - network path
        detail = exc.read().decode("utf-8", "replace")[:300]
        if exc.code == 403 and "rate limit" in detail.lower():
            raise TargetError(
                "GitHub rate limit reached. Set GITHUB_TOKEN to raise it "
                "(60 req/h anonymous vs 5000 authenticated)."
            ) from exc
        if exc.code == 404:
            raise TargetError(
                f"{url} returned 404: PR not found, or the repo is private and "
                "GITHUB_TOKEN is unset."
            ) from exc
        raise TargetError(f"GitHub API {exc.code} for {url}: {detail}") from exc
    except urllib.error.URLError as exc:  # pragma: no cover - network path
        raise TargetError(f"cannot reach {url}: {exc.reason}") from exc


def fetch_pr_files_sync(ref: PRRef, *, token: str | None = None,
                        max_files: int = 100) -> list[FileDiff]:
    """Fetch a PR's changed files, following pagination."""
    token = token or os.environ.get("GITHUB_TOKEN") or None
    files: list[FileDiff] = []
    page = 1
    while len(files) < max_files:
        url = (f"{GITHUB_API}/repos/{ref.owner}/{ref.repo}/pulls/{ref.number}"
               f"/files?per_page=100&page={page}")
        payload, _ = _request_json(url, token)
        if not isinstance(payload, list):
            raise TargetError(f"unexpected payload for {url}: {type(payload).__name__}")
        if not payload:
            break
        for item in payload:
            path = item.get("filename", "")
            patch = item.get("patch") or ""
            if not path or should_skip(path):
                continue
            if not patch:
                # Binary file, or a diff too large for the API to inline.
                continue
            files.append(FileDiff(
                path=path,
                status=item.get("status", "modified"),
                additions=int(item.get("additions") or 0),
                deletions=int(item.get("deletions") or 0),
                patch=patch,
                hunks=parse_hunks(patch),
            ))
            if len(files) >= max_files:
                break
        if len(payload) < 100:
            break
        page += 1
    return files


async def fetch_pr_files(ref: PRRef, *, token: str | None = None,
                         max_files: int = 100) -> list[FileDiff]:
    """Async wrapper: the blocking urllib call runs off the event loop."""
    return await asyncio.to_thread(
        fetch_pr_files_sync, ref, token=token, max_files=max_files
    )


def load_diff_file(path: str) -> list[FileDiff]:
    """Offline mode: parse a local unified diff (`git diff > x.patch`)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError as exc:
        raise TargetError(f"cannot read diff file {path!r}: {exc}") from exc
    files = [f for f in split_unified_diff(text) if not should_skip(f.path)]
    if not files:
        raise TargetError(
            f"no reviewable file found in {path!r}. "
            "Expected a unified diff with 'diff --git a/... b/...' headers."
        )
    return files
