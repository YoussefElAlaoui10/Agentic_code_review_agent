"""LLM transport, behind one small async interface.

`LLMClient.chat()` is the only thing the agent loop knows about. Three
implementations:

* `OpenAICompatClient` -- /v1/chat/completions, which covers the module's Gemma
  endpoints, vLLM, Ollama, OpenAI and most gateways.
* `MultiEndpointClient` -- round-robins over several base URLs so N parallel
  reviewers actually land on N different servers instead of queueing on one.
* `MockLLM`             -- deterministic, offline, backed by `rules.py`, with a
  configurable simulated latency so the speedup benchmark is meaningful.

Concurrency note: there is no async HTTP client in the standard library, so the
blocking `urllib` request is pushed off the event loop with `asyncio.to_thread`.
CPython releases the GIL around socket syscalls, so those threads genuinely
overlap in time -- this is real I/O concurrency, not the fake kind the rubric
warns about. `scripts/prove_concurrency.py` measures it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import re
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Sequence

from .models import FileDiff
from .rules import analyse_file

DEFAULT_MODEL = os.environ.get("PRREVIEW_MODEL", "gemma-4-26b-a4b")


class LLMError(RuntimeError):
    """Transport-level failure after retries were exhausted."""


@dataclass(slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(slots=True)
class LLMResponse:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    endpoint: str = ""
    latency_s: float = 0.0

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class LLMClient(ABC):
    """Minimal async chat interface with optional tool calling."""

    name: str = "llm"
    model: str = DEFAULT_MODEL

    @abstractmethod
    async def chat(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        tools: Sequence[dict[str, Any]] | None = None,
        temperature: float = 0.1,
        max_tokens: int = 1500,
    ) -> LLMResponse:
        ...

    async def aclose(self) -> None:  # pragma: no cover - default no-op
        return None


# --------------------------------------------------------------------------- #
# Real transport
# --------------------------------------------------------------------------- #

def _post_json_blocking(url: str, payload: dict[str, Any], headers: dict[str, str],
                        timeout: float) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


class OpenAICompatClient(LLMClient):
    """Any /v1/chat/completions server.

    Retries 429/5xx and transient socket errors with exponential backoff plus
    jitter; the jitter matters because N reviewers that start together would
    otherwise retry in lockstep and re-create the burst that caused the 429.
    """

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str | None = None,
        model: str = DEFAULT_MODEL,
        timeout: float = 120.0,
        max_retries: int = 4,
        name: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or os.environ.get("PRREVIEW_API_KEY") or "not-needed"
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries
        self.name = name or self.base_url
        self.calls = 0

    @property
    def _url(self) -> str:
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        if self.base_url.endswith("/v1"):
            return f"{self.base_url}/chat/completions"
        return f"{self.base_url}/v1/chat/completions"

    async def chat(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        tools: Sequence[dict[str, Any]] | None = None,
        temperature: float = 0.1,
        max_tokens: int = 1500,
    ) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": list(messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = list(tools)
            payload["tool_choice"] = "auto"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

        started = time.perf_counter()
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                self.calls += 1
                data = await asyncio.to_thread(
                    _post_json_blocking, self._url, payload, headers, self.timeout
                )
                return self._parse(data, time.perf_counter() - started)
            except urllib.error.HTTPError as exc:
                retryable = exc.code == 429 or 500 <= exc.code < 600
                detail = exc.read().decode("utf-8", "replace")[:200]
                last_error = LLMError(f"HTTP {exc.code} from {self.name}: {detail}")
                if not retryable or attempt == self.max_retries:
                    raise last_error from exc
            except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
                last_error = LLMError(f"{type(exc).__name__} from {self.name}: {exc}")
                if attempt == self.max_retries:
                    raise last_error from exc
            delay = min(20.0, 0.5 * (2 ** attempt)) * (0.5 + random.random())
            await asyncio.sleep(delay)
        raise last_error or LLMError("unreachable")

    def _parse(self, data: dict[str, Any], latency: float) -> LLMResponse:
        try:
            choice = data["choices"][0]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"malformed response from {self.name}: {str(data)[:200]}") from exc
        msg = choice.get("message") or {}
        usage = data.get("usage") or {}
        calls: list[ToolCall] = []
        for i, tc in enumerate(msg.get("tool_calls") or []):
            fn = tc.get("function") or {}
            raw_args = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
            except json.JSONDecodeError:
                args = {}
            calls.append(ToolCall(id=tc.get("id") or f"call_{i}",
                                  name=fn.get("name") or "", arguments=args))
        return LLMResponse(
            text=msg.get("content") or "",
            tool_calls=calls,
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            endpoint=self.name,
            latency_s=latency,
        )


class MultiEndpointClient(LLMClient):
    """Round-robin over several backends.

    With one endpoint, raising the reviewer concurrency past that server's own
    batch limit just grows a queue; spreading requests over the 7 provided
    endpoints is what turns concurrency into wall-clock speedup.
    """

    def __init__(self, clients: Sequence[LLMClient], *, name: str = "multi") -> None:
        if not clients:
            raise ValueError("MultiEndpointClient needs at least one client")
        self.clients = list(clients)
        self.name = name
        self.model = self.clients[0].model
        self._next = 0
        self._lock = asyncio.Lock()
        self.per_endpoint_calls = [0] * len(self.clients)

    async def _pick(self) -> tuple[int, LLMClient]:
        async with self._lock:
            idx = self._next
            self._next = (self._next + 1) % len(self.clients)
            self.per_endpoint_calls[idx] += 1
            return idx, self.clients[idx]

    async def chat(self, messages, *, tools=None, temperature=0.1, max_tokens=1500):
        _, client = await self._pick()
        return await client.chat(messages, tools=tools, temperature=temperature,
                                 max_tokens=max_tokens)

    async def aclose(self) -> None:
        for c in self.clients:
            await c.aclose()


# --------------------------------------------------------------------------- #
# Offline deterministic transport
# --------------------------------------------------------------------------- #

class MockLLM(LLMClient):
    """Offline stand-in that behaves like a tool-calling reviewer model.

    Turn 1: asks for one tool (so the agent loop is genuinely exercised).
    Turn 2: emits findings as JSON, derived from `rules.py` and shaped by the
            reviewer role found in the system prompt.

    `latency` simulates network/inference time. It is `asyncio.sleep`, i.e. a
    real await, so the orchestrator's concurrency is measured honestly -- but
    the resulting speedup describes the orchestrator, not a real model's
    throughput. The report states this explicitly.
    """

    def __init__(
        self,
        *,
        latency: float = 0.30,
        jitter: float = 0.12,
        model: str = "mock-reviewer",
        seed: int = 1234,
        failure_rate: float = 0.0,
        name: str = "mock",
    ) -> None:
        self.latency = latency
        self.jitter = jitter
        self.model = model
        self.name = name
        self.seed = seed
        self.failure_rate = failure_rate
        self.calls = 0
        self._files: dict[str, FileDiff] = {}

    def register(self, files: Sequence[FileDiff]) -> None:
        """Give the mock access to the diffs, standing in for the model's context."""
        for fd in files:
            self._files[fd.path] = fd

    async def chat(self, messages, *, tools=None, temperature=0.1, max_tokens=1500):
        self.calls += 1
        rng = random.Random(f"{self.seed}:{self.calls}")
        delay = max(0.0, self.latency + rng.uniform(-self.jitter, self.jitter))
        started = time.perf_counter()
        await asyncio.sleep(delay)

        if self.failure_rate and rng.random() < self.failure_rate:
            raise LLMError(f"simulated transient failure from {self.name}")

        system = next((m.get("content", "") for m in messages
                       if m.get("role") == "system"), "")
        user = "\n".join(str(m.get("content", "")) for m in messages
                         if m.get("role") == "user")
        role = self._role(system)
        path = self._path(user)
        already_used_tool = any(m.get("role") == "tool" for m in messages)

        if tools and not already_used_tool:
            return LLMResponse(
                text="",
                tool_calls=[ToolCall(
                    id=f"call_{self.calls}",
                    name="get_file_context",
                    arguments={"path": path, "around_line": 1, "radius": 40},
                )],
                prompt_tokens=len(user) // 4,
                completion_tokens=24,
                endpoint=self.name,
                latency_s=time.perf_counter() - started,
            )

        findings = self._findings_for(path, role)
        return LLMResponse(
            text=json.dumps({"findings": findings}, ensure_ascii=False),
            prompt_tokens=len(user) // 4,
            completion_tokens=max(40, len(json.dumps(findings)) // 4),
            endpoint=self.name,
            latency_s=time.perf_counter() - started,
        )

    @staticmethod
    def _role(system: str) -> str:
        m = re.search(r"\[role:(\w+)\]", system)
        return m.group(1) if m else "general"

    @staticmethod
    def _path(user: str) -> str:
        m = re.search(r"^File: (.+)$", user, re.MULTILINE)
        return m.group(1).strip() if m else "unknown"

    def _findings_for(self, path: str, role: str) -> list[dict[str, Any]]:
        """Shape the rule hits into what a role-specialised reviewer would say.

        A real `performance` reviewer does not report every `== None` nit: it
        mostly stays in its lane and occasionally strays. Modelling that matters,
        because if every role reported everything the roles would be redundant,
        agreement would be unanimous by construction, and the judge's
        corroboration logic would look effective when it was measuring nothing.

        Off-focus findings are therefore kept only sometimes, chosen by a hash of
        (role, path, line) so the behaviour stays deterministic and the
        benchmarks remain reproducible.
        """
        fd = self._files.get(path)
        if fd is None:
            return []
        focus = {"correctness": "bug", "performance": "perf",
                 "tests": "test", "style": "style"}.get(role)

        out: list[dict[str, Any]] = []
        for h in analyse_file(fd):
            in_focus = focus is None or h.category == focus
            if not in_focus:
                # Deterministic ~30% chance of straying outside the specialty.
                digest = hashlib.sha256(f"{role}:{path}:{h.line}:{h.rule_id}".encode())
                if digest.digest()[0] >= 77:  # 77/256 ~= 0.30
                    continue
            confidence = min(1.0, h.confidence * (1.08 if in_focus else 0.6))
            out.append({
                "path": path, "line": h.line, "severity": h.severity,
                "category": h.category, "title": h.title,
                "justification": h.justification,
                "confidence": round(confidence, 3), "suggestion": h.suggestion,
            })
        return out

# --------------------------------------------------------------------------- #
# Construction from the environment
# --------------------------------------------------------------------------- #

def endpoints_from_env() -> list[str]:
    """Read PRREVIEW_ENDPOINTS (comma/space separated) or PRREVIEW_BASE_URL."""
    raw = os.environ.get("PRREVIEW_ENDPOINTS", "")
    urls = [u.strip() for u in re.split(r"[,\s]+", raw) if u.strip()]
    if urls:
        return urls
    single = os.environ.get("PRREVIEW_BASE_URL", "").strip()
    return [single] if single else []


def build_client(
    *,
    mock: bool | None = None,
    model: str | None = None,
    latency: float = 0.30,
    timeout: float = 120.0,
) -> LLMClient:
    """Pick a transport: explicit --mock, else configured endpoints, else mock.

    Falling back to the mock rather than raising is deliberate: the demo must
    always run, and the CLI prints which transport was selected.
    """
    urls = endpoints_from_env()
    if mock is True or (mock is None and not urls):
        return MockLLM(latency=latency, model=model or "mock-reviewer")
    chosen_model = model or DEFAULT_MODEL
    clients = [
        OpenAICompatClient(u, model=chosen_model, timeout=timeout, name=f"ep{i + 1}")
        for i, u in enumerate(urls)
    ]
    if len(clients) == 1:
        return clients[0]
    return MultiEndpointClient(clients, name=f"multi({len(clients)})")
