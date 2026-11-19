# Automated review of `samples/demo_pr.diff (local diff)`

- Model: `mock-reviewer`
- Mode: parallel, concurrency 8
- 4 files, 12 reviewer tasks
- Wall clock **0.68s** (summed agent time 4.02s, peak overlap 8)
- 32 raw findings -> 18 published

| File | Line | Severity | Category | Conf. | Finding |
|---|---:|---|---|---:|---|
| `app/exporter.py` | 16 | high | bug | 0.99 | `shell=True` enables shell injection |
| `app/fetcher.py` | 10 | high | bug | 0.99 | Mutable default argument is shared across calls |
| `app/fetcher.py` | 24 | high | perf | 0.99 | Synchronous HTTP call inside a coroutine blocks the event loop |
| `app/fetcher.py` | 14 | high | perf | 0.95 | Blocking `time.sleep` inside a coroutine stalls the event loop |
| `app/fetcher.py` | 32 | high | bug | 0.89 | `eval`/`exec` on runtime data is a code-execution risk |
| `app/cache.py` | 14 | high | bug | 0.78 | Lock acquired without a guaranteed release |
| `app/fetcher.py` | 26 | medium | bug | 0.93 | Bare `except:` swallows every exception |
| `app/cache.py` | 13 | medium | test | 0.80 | New public function `put` has no test in this change |
| `app/exporter.py` | 7 | medium | test | 0.80 | New public function `export_csv` has no test in this change |
| `app/exporter.py` | 14 | medium | test | 0.80 | New public function `archive` has no test in this change |
| `app/exporter.py` | 8 | medium | bug | 0.77 | File opened without a context manager may leak the handle |
| `app/fetcher.py` | 10 | medium | test | 0.68 | New public function `fetch_feeds` has no test in this change |
| `app/fetcher.py` | 30 | medium | test | 0.68 | New public function `parse_rule` has no test in this change |
| `app/cache.py` | 25 | medium | bug | 0.59 | `assert` used for runtime validation is stripped under -O |
| `app/cache.py` | 19 | medium | test | 0.56 | New public function `get` has no test in this change |
| `app/cache.py` | 30 | medium | test | 0.56 | New public function `warm` has no test in this change |
| `app/fetcher.py` | 22 | medium | test | 0.56 | New public function `fetch_one` has no test in this change |
| `app/fetcher.py` | 35 | medium | test | 0.56 | New public function `summarise` has no test in this change |

## Details

### `app/exporter.py:16` -- `shell=True` enables shell injection
**high / bug**, confidence 0.99, found by `correctness` (corroborated by tests)

With shell=True the command string is interpreted by the shell, so any interpolated value can inject further commands.

*Suggestion:* Pass a list of arguments and leave shell=False.

### `app/fetcher.py:10` -- Mutable default argument is shared across calls
**high / bug**, confidence 0.99, found by `correctness` (corroborated by performance, tests)

Default arguments are evaluated once at function definition, so the same list/dict is reused by every call and accumulates state.

*Suggestion:* Default to `None` and build the container inside the function.

### `app/fetcher.py:24` -- Synchronous HTTP call inside a coroutine blocks the event loop
**high / perf**, confidence 0.99, found by `performance` (corroborated by tests)

`requests` is blocking; called from a coroutine it serialises all concurrent tasks on the same loop.

*Suggestion:* Use an async client, or wrap the call in `asyncio.to_thread`.

### `app/fetcher.py:14` -- Blocking `time.sleep` inside a coroutine stalls the event loop
**high / perf**, confidence 0.95, found by `performance`

time.sleep blocks the thread running the loop, so every other task on that loop is frozen for the duration: the concurrency is fake.

*Suggestion:* Use `await asyncio.sleep(...)`.

### `app/fetcher.py:32` -- `eval`/`exec` on runtime data is a code-execution risk
**high / bug**, confidence 0.89, found by `correctness`

If any part of the argument comes from user input, request data or a file, this executes arbitrary code in the process.

*Suggestion:* Use `ast.literal_eval`, `json.loads`, or an explicit dispatch dict.

### `app/cache.py:14` -- Lock acquired without a guaranteed release
**high / bug**, confidence 0.78, found by `correctness`

If the guarded block raises, `.release()` is never reached and every other thread blocks on that lock forever: a deadlock.

*Suggestion:* Use the lock as a context manager (`with lock:`) or try/finally.

### `app/fetcher.py:26` -- Bare `except:` swallows every exception
**medium / bug**, confidence 0.93, found by `correctness`

A bare except also catches KeyboardInterrupt and SystemExit, so the process stops being interruptible and real errors are hidden.

*Suggestion:* Catch the specific exception, or `except Exception:` at minimum.

### `app/cache.py:13` -- New public function `put` has no test in this change
**medium / test**, confidence 0.80, found by `tests` (corroborated by correctness, performance)

`put` is introduced by this PR and no test file in the same change references it, so the new behaviour is unverified.

*Suggestion:* Add a unit test covering `put`'s expected result and one edge case.

### `app/exporter.py:7` -- New public function `export_csv` has no test in this change
**medium / test**, confidence 0.80, found by `tests` (corroborated by correctness, performance)

`export_csv` is introduced by this PR and no test file in the same change references it, so the new behaviour is unverified.

*Suggestion:* Add a unit test covering `export_csv`'s expected result and one edge case.

### `app/exporter.py:14` -- New public function `archive` has no test in this change
**medium / test**, confidence 0.80, found by `tests` (corroborated by correctness, performance)

`archive` is introduced by this PR and no test file in the same change references it, so the new behaviour is unverified.

*Suggestion:* Add a unit test covering `archive`'s expected result and one edge case.

### `app/exporter.py:8` -- File opened without a context manager may leak the handle
**medium / bug**, confidence 0.77, found by `correctness` (corroborated by tests)

Without `with`, an exception before `.close()` leaves the descriptor open until the garbage collector runs.

*Suggestion:* Wrap it in `with open(...) as fh:`.

### `app/fetcher.py:10` -- New public function `fetch_feeds` has no test in this change
**medium / test**, confidence 0.68, found by `tests` (corroborated by correctness)

`fetch_feeds` is introduced by this PR and no test file in the same change references it, so the new behaviour is unverified.

*Suggestion:* Add a unit test covering `fetch_feeds`'s expected result and one edge case.

### `app/fetcher.py:30` -- New public function `parse_rule` has no test in this change
**medium / test**, confidence 0.68, found by `tests` (corroborated by performance)

`parse_rule` is introduced by this PR and no test file in the same change references it, so the new behaviour is unverified.

*Suggestion:* Add a unit test covering `parse_rule`'s expected result and one edge case.

### `app/cache.py:25` -- `assert` used for runtime validation is stripped under -O
**medium / bug**, confidence 0.59, found by `correctness`

Running Python with -O removes assert statements entirely, so this check silently disappears in optimised deployments.

*Suggestion:* Raise an explicit exception instead.

### `app/cache.py:19` -- New public function `get` has no test in this change
**medium / test**, confidence 0.56, found by `tests`

`get` is introduced by this PR and no test file in the same change references it, so the new behaviour is unverified.

*Suggestion:* Add a unit test covering `get`'s expected result and one edge case.

### `app/cache.py:30` -- New public function `warm` has no test in this change
**medium / test**, confidence 0.56, found by `tests`

`warm` is introduced by this PR and no test file in the same change references it, so the new behaviour is unverified.

*Suggestion:* Add a unit test covering `warm`'s expected result and one edge case.

### `app/fetcher.py:22` -- New public function `fetch_one` has no test in this change
**medium / test**, confidence 0.56, found by `tests`

`fetch_one` is introduced by this PR and no test file in the same change references it, so the new behaviour is unverified.

*Suggestion:* Add a unit test covering `fetch_one`'s expected result and one edge case.

### `app/fetcher.py:35` -- New public function `summarise` has no test in this change
**medium / test**, confidence 0.56, found by `tests`

`summarise` is introduced by this PR and no test file in the same change references it, so the new behaviour is unverified.

*Suggestion:* Add a unit test covering `summarise`'s expected result and one edge case.

