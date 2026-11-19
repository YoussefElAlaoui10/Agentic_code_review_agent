# prreview — agentic pull-request review with parallel agents

Final project for **Advanced Programming and Parallel Computing** (MIASHS 2026–2027),
topic *"Agentic code review agent"*.

An agentic reviewer that fetches a pull request, reviews **every file in parallel**
with several role-specialised agents, and then runs a **judge agent** that
deduplicates, corroborates and filters the findings before publication.

```
            ┌─ reviewer agent (correctness) ─┐
 PR / diff ─┼─ reviewer agent (performance) ─┼─► judge ─► ranked findings
            └─ reviewer agent (tests) ───────┘   (dedupe, corroborate, filter, cap)
                 N agents, bounded by a semaphore
```

Each reviewer is a real **agent loop**: it decides which tools to call
(`get_diff_hunks`, `get_file_context`, `grep_repo`, `find_tests_for`,
`list_changed_files`), in what order, and when it has seen enough to answer —
rather than being handed a fixed prompt once.

---

## Objective

Three things have to be true at once, and the repository demonstrates each:

| Goal | Where it is demonstrated |
|---|---|
| Real parallelism, not sequential code in disguise | `scripts/prove_concurrency.py`, `prreview speedup` |
| Measured and justified gains | `results/speedup.{md,svg}`, `results/timeline.svg` |
| The review is actually useful | `prreview quality` → precision / recall / F1 vs labelled defects |

## Requirements

- **Python ≥ 3.10** (developed and tested on 3.14.4)
- **No third-party dependencies.** Everything uses the standard library, so there
  is nothing to pin and nothing to install. `requirements.txt` is intentionally
  empty and `pyproject.toml` declares `dependencies = []`.

```bash
git clone <this-repo> && cd pr-review-agent
python3 --version          # must print 3.10 or newer
export PYTHONPATH=src      # or: pip install -e .
```

## Quick start (offline, no API key, no network)

The `--mock` transport is a deterministic offline model, so the demo, the tests
and both benchmarks run with nothing configured:

```bash
PYTHONPATH=src python3 -m prreview review samples/demo_pr.diff --mock -c 8
```

```
[transport] offline mock model (no PRREVIEW_ENDPOINTS / PRREVIEW_BASE_URL set)
Review of samples/demo_pr.diff (local diff)
  model=mock-reviewer  mode=parallel  concurrency=8  files=4  tasks=12
  wall clock 0.63s   summed agent time 3.29s   peak overlap 8
  32 raw -> 18 deduped -> 18 published (10 merged, 0 below threshold, 9 corroborated)

app/fetcher.py
  !! L10    high/bug   conf 0.99 +2 agree  Mutable default argument is shared across calls
        Default arguments are evaluated once at function definition, so the same
        list/dict is reused by every call and accumulates state.
        -> Default to `None` and build the container inside the function.
  !! L14    high/perf  conf 0.95  Blocking `time.sleep` inside a coroutine stalls the event loop
        time.sleep blocks the thread running the loop, so every other task on
        that loop is frozen for the duration: the concurrency is fake.
        -> Use `await asyncio.sleep(...)`.
...
```

The process exits `1` when a high-severity finding survives the judge, so it can
be used directly as a CI gate.

## Reviewing a real pull request

```bash
# public PR, anonymous (60 requests/hour)
PYTHONPATH=src python3 -m prreview review https://github.com/dask/dask/pull/11000 --mock

# raise the rate limit and reach private repositories
export GITHUB_TOKEN=ghp_...
```

Accepted targets: a PR URL, `owner/repo#123`, `owner/repo/pull/123`, or a path
to a local `.diff` / `.patch` file (`git diff > change.patch`).

## Connecting a real model

Point the client at any OpenAI-compatible `/v1/chat/completions` server. Several
endpoints are round-robined, which is what turns reviewer concurrency into
wall-clock speedup instead of a queue on one server:

```bash
# the module's 7 provided endpoints
export PRREVIEW_ENDPOINTS="http://ep1:8000,http://ep2:8000,http://ep3:8000,http://ep4:8000,http://ep5:8000,http://ep6:8000,http://ep7:8000"
export PRREVIEW_MODEL="gemma-4-26b-a4b"

# a local model instead
export PRREVIEW_BASE_URL="http://localhost:11434/v1"   # Ollama
export PRREVIEW_MODEL="qwen2.5-coder:7b"

# a hosted API
export PRREVIEW_BASE_URL="https://api.openai.com/v1"
export PRREVIEW_API_KEY="sk-..."
```

With no endpoint configured the CLI falls back to the mock and says so on
stderr, so a demo never dies on a missing variable.

| Variable | Meaning |
|---|---|
| `PRREVIEW_ENDPOINTS` | comma/space separated base URLs, round-robined |
| `PRREVIEW_BASE_URL` | a single base URL (used when `PRREVIEW_ENDPOINTS` is unset) |
| `PRREVIEW_MODEL` | model name sent to the endpoint |
| `PRREVIEW_API_KEY` | bearer token, when the endpoint needs one |
| `GITHUB_TOKEN` | raises the GitHub rate limit; required for private repos |

## Commands

```bash
# review, in several output formats
python3 -m prreview review <target> --format markdown -o review.md
python3 -m prreview review <target> --format github -o review.json   # PR review payload, not posted
python3 -m prreview review <target> --sequential                     # the benchmark baseline
python3 -m prreview review <target> --roles correctness,performance,tests,style
python3 -m prreview review <target> --repo-root . --min-confidence 0.6

# measurements
python3 -m prreview speedup samples/demo_pr.diff --mock --concurrencies 1,2,4,8,12 --repeats 3
python3 -m prreview quality --mock --thresholds 0.0,0.45,0.6,0.75,0.9

# MCP server over stdio (editor integration)
python3 -m prreview serve-mcp --mock
```

`--format github` prints a ready `POST /repos/{owner}/{repo}/pulls/{n}/reviews`
body. It is **produced, never sent** — posting comments to someone's pull
request is a side effect that belongs to whoever runs the tool.

## Tests

```bash
python3 -m unittest discover -s tests -t .    # 49 tests, no dependencies
pytest tests/                                  # also works where pytest is installed
```

The suite covers diff line-number arithmetic, target parsing, LLM-output
coercion and JSON recovery, the rule engine's context sensitivity, tool
sandboxing (path traversal is refused), the judge's merge/filter/cap logic, the
agent loop (including dropping hallucinated line numbers), and the orchestrator
(semaphore bound, overlap, one failing agent not sinking the run).

Two of these tests found real bugs during development, both fixed and now
regression-covered:

- `Finding.dedupe_key` discarded every token of ≤3 characters, so findings whose
  titles differed only by a short identifier ("missing test for `foo`" vs "…
  for `bar`") were silently merged into one.
- the overlap sweep line counted a task ending at exactly *t* and one starting
  at exactly *t* as simultaneous, so a strictly sequential run reported a peak
  concurrency of 2.

## Measured results

`python3 scripts/prove_concurrency.py` — 4 files × 3 roles = 12 agent tasks,
simulated 150 ms per model call:

| Configuration | Wall clock | Summed agent time | Ratio | Peak overlap | Worst event-loop gap |
|---|---:|---:|---:|---:|---:|
| sequential | 3.73 s | 3.73 s | 1.0× | 1 | 1.8 ms |
| parallel c=4 | 0.96 s | 3.73 s | 3.9× | 4 | 2.0 ms |
| parallel c=12 | 0.35 s | 3.72 s | 10.7× | 12 | 1.4 ms |

All three publish the **same 18 findings**: concurrency changes the time, not the
result. The event-loop watchdog never observes a stall above 2 ms, which is the
direct evidence that no blocking call is running on the loop.

`prreview speedup` (median of 3 runs, 200 ms simulated latency):

| Mode | Concurrency | Wall clock | Speedup | Efficiency | Peak overlap |
|---|---:|---:|---:|---:|---:|
| sequential | 1 | 5.22 s | 1.00× | 100 % | 1 |
| parallel | 2 | 2.81 s | 1.86× | 93 % | 2 |
| parallel | 4 | 1.46 s | 3.59× | 90 % | 4 |
| parallel | 8 | 0.88 s | 5.95× | 74 % | 8 |
| parallel | 12 | 0.58 s | 9.02× | 75 % | 12 |

Efficiency falls off above 4 because each agent makes **two sequential model
calls** (one tool step, one answer step): the critical path is 2 × latency, so
12 agents cannot reach 12×. See the report for the full analysis.

`prreview quality` — 16 hand-labelled defects in `bench/demo_pr.labels.json`:

| Configuration | Threshold | Published | TP | FP | FN | Precision | Recall | F1 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| rule-baseline | — | 22 | 14 | 8 | 2 | 0.64 | 0.88 | **0.74** |
| agents | 0.00 | 18 | 11 | 7 | 5 | 0.61 | 0.69 | **0.65** |
| agents | 0.60 | 13 | 10 | 3 | 6 | 0.77 | 0.62 | **0.69** |
| agents | 0.75 | 11 | 9 | 2 | 7 | 0.82 | 0.56 | **0.67** |
| agents | 0.90 | 5 | 5 | 0 | 11 | 1.00 | 0.31 | **0.48** |

**Read this honestly:** the rule baseline *beats* the agent pipeline here, and it
has to. The offline mock derives its findings from that same rule engine, so it
cannot know anything the rules do not, while role filtering and judge
deduplication can only remove information. This benchmark therefore validates
the *harness* — that scoring, matching and the threshold sweep behave correctly
— and not the quality of any language model. Running the same command against
real endpoints is what measures a model, and the two deliberately-seeded
semantic defects (`fetcher.py:36`, `cache.py:27`) are the cases no pattern rule
can reach.

## Layout

```
src/prreview/
  models.py        typed Finding / FileDiff / ReviewResult, validation at the boundary
  diffparse.py     unified-diff parsing, line-number annotation
  ghclient.py      GitHub PR files API + offline .diff loading
  llm.py           LLMClient ABC, OpenAI-compatible, round-robin, offline mock
  rules.py         deterministic rule engine (backs the mock, and is the baseline)
  tools.py         the agent's read-only sandboxed tools
  reviewer.py      the agent loop (tool steps, JSON recovery, self-validation)
  judge.py         dedupe, corroborate, filter, cap  (+ optional LLM polish)
  orchestrator.py  asyncio fan-out, semaphore backpressure, overlap metrics
  bench.py         speedup sweep and precision/recall/F1 scoring
  render.py        terminal / markdown / GitHub payload / SVG charts
  mcp_server.py    JSON-RPC MCP server over stdio
  cli.py           review | speedup | quality | serve-mcp
scripts/prove_concurrency.py   three independent checks that the fan-out overlaps
samples/demo_pr.diff           synthetic PR with seeded defects
bench/demo_pr.labels.json      ground truth + labelling protocol
report/rapport.md              the written report
results/                       generated tables, JSON and SVG charts
```

## Known limits

- The judge merges same-category findings within ±3 lines, so **two distinct
  defects of the same category on adjacent lines are reported as one**. Measured:
  with `line_window=3` the blocking call at `fetcher.py:15` is absorbed into the
  one at `:14`; with `line_window=0` both appear. The window is a `JudgeConfig`
  field, not yet a CLI flag.
- `rules.py` infers `async def` context from indentation **within the diff**, so a
  hunk that starts mid-function cannot see its enclosing signature and a
  blocking call there is not flagged.
- The GitHub API omits `patch` for binary files and very large diffs; those files
  are skipped rather than fetched separately.
- Only `/v1/chat/completions` tool-calling is implemented; a model without tool
  support would fall back to a single answer step and stop being agentic.
- Speedup figures obtained with `--mock` measure the orchestrator against
  simulated latency. Because the mock's delay sequence is seeded, repeated runs
  are bit-identical and the reported standard deviation is 0.00 by construction;
  it only becomes meaningful against real endpoints.
