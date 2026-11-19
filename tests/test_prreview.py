"""Unit tests. Runnable with the standard library alone:

    python -m unittest discover -s tests -t .

pytest also collects these (it understands unittest.TestCase), so
`pytest tests/` works where pytest is installed.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from prreview.bench import Label, rule_baseline_findings, score_findings
from prreview.diffparse import parse_hunks, split_unified_diff
from prreview.ghclient import TargetError, load_diff_file, parse_pr_target, should_skip
from prreview.judge import JudgeAgent, JudgeConfig
from prreview.llm import MockLLM
from prreview.models import Finding, InvalidFinding, ReviewResult, sort_findings
from prreview.orchestrator import (OrchestratorConfig, ReviewOrchestrator,
                                   TaskMetric, max_overlap)
from prreview.reviewer import ReviewerAgent, _parse_findings
from prreview.rules import analyse_file
from prreview.tools import ToolContext, execute_tool

SAMPLE = Path(__file__).resolve().parents[1] / "samples" / "demo_pr.diff"

ONE_FILE_DIFF = """diff --git a/app/svc.py b/app/svc.py
--- a/app/svc.py
+++ b/app/svc.py
@@ -5,3 +5,7 @@ import time
 def existing():
     pass
 
+async def handler(items, cache=[]):
+    time.sleep(1)
+    return items
"""


def run(coro):
    return asyncio.run(coro)


class TestDiffParsing(unittest.TestCase):
    def test_hunk_line_numbers_follow_the_new_file(self):
        """Added lines must carry their real line number in the new file."""
        fd = split_unified_diff(ONE_FILE_DIFF)[0]
        added = dict((ln, t.strip()) for h in fd.hunks for ln, t in h.added_lines())
        self.assertIn(8, added)
        self.assertTrue(added[8].startswith("async def handler"))
        self.assertEqual(added[9], "time.sleep(1)")

    def test_counts_and_language(self):
        fd = split_unified_diff(ONE_FILE_DIFF)[0]
        self.assertEqual(fd.path, "app/svc.py")
        self.assertEqual(fd.additions, 3)
        self.assertEqual(fd.deletions, 0)
        self.assertEqual(fd.language, "python")
        self.assertFalse(fd.is_test_file)

    def test_multi_file_diff(self):
        files = load_diff_file(str(SAMPLE))
        self.assertEqual(len(files), 4)
        self.assertEqual([f.path for f in files][0], "app/fetcher.py")
        self.assertTrue(any(f.is_test_file for f in files))
        self.assertEqual(files[0].status, "added")

    def test_empty_patch_yields_no_hunks(self):
        self.assertEqual(parse_hunks(""), ())

    def test_noise_lines_do_not_shift_numbering(self):
        patch = ("@@ -1,2 +1,3 @@\n"
                 " keep\n"
                 "+added\n"
                 "\\ No newline at end of file\n")
        hunks = parse_hunks(patch)
        self.assertEqual([ln for ln, _ in hunks[0].added_lines()], [2])


class TestTargetParsing(unittest.TestCase):
    def test_accepted_forms(self):
        for target in ("https://github.com/dask/dask/pull/11",
                       "dask/dask#11", "dask/dask/pull/11"):
            self.assertEqual(parse_pr_target(target).slug, "dask/dask#11")

    def test_rejects_garbage(self):
        with self.assertRaises(TargetError):
            parse_pr_target("not a pull request")

    def test_skips_generated_files(self):
        self.assertTrue(should_skip("poetry.lock"))
        self.assertTrue(should_skip("static/app.min.js"))
        self.assertFalse(should_skip("src/app.py"))


class TestFindingValidation(unittest.TestCase):
    def test_coerces_vendor_synonyms(self):
        f = Finding.from_raw(
            {"severity": "CRITICAL", "type": "performance", "line": "12",
             "summary": "blocking call", "confidence": 2.0},
            path="a.py", reviewer="perf")
        self.assertEqual((f.severity, f.category, f.line), ("high", "perf", 12))
        self.assertEqual(f.confidence, 1.0)

    def test_rejects_unknown_severity(self):
        with self.assertRaises(InvalidFinding):
            Finding(path="a.py", line=1, severity="catastrophic",  # type: ignore[arg-type]
                    category="bug", title="t", justification="j")

    def test_rejects_empty_title(self):
        with self.assertRaises(InvalidFinding):
            Finding(path="a.py", line=1, severity="low", category="bug",
                    title="   ", justification="j")

    def test_dedupe_key_is_wording_insensitive(self):
        a = Finding(path="a.py", line=1, severity="high", category="bug",
                    title="Mutable default argument is shared", justification="j")
        b = Finding(path="a.py", line=9, severity="low", category="bug",
                    title="shared mutable default argument", justification="j")
        self.assertEqual(a.dedupe_key(), b.dedupe_key())

    def test_sort_is_severity_then_confidence(self):
        low = Finding(path="a.py", line=1, severity="low", category="bug",
                      title="l", justification="j", confidence=0.9)
        high = Finding(path="a.py", line=2, severity="high", category="bug",
                       title="h", justification="j", confidence=0.1)
        self.assertEqual([f.title for f in sort_findings([low, high])], ["h", "l"])


class TestJSONRecovery(unittest.TestCase):
    def test_plain_object(self):
        self.assertEqual(_parse_findings('{"findings": []}'), [])

    def test_code_fence(self):
        out = _parse_findings('```json\n{"findings": [{"line": 3}]}\n```')
        self.assertEqual(out, [{"line": 3}])

    def test_prose_around_json(self):
        out = _parse_findings('Sure! {"findings": [{"line": 4}]} Hope that helps.')
        self.assertEqual(out, [{"line": 4}])

    def test_bare_list(self):
        self.assertEqual(_parse_findings('[{"line": 1}]'), [{"line": 1}])

    def test_unparseable_returns_none(self):
        """None means 'retry', which is different from an empty finding list."""
        self.assertIsNone(_parse_findings("I could not review this file."))

    def test_empty_text_is_no_findings(self):
        self.assertEqual(_parse_findings("   "), [])


class TestRules(unittest.TestCase):
    def test_blocking_sleep_only_flagged_inside_coroutine(self):
        fd = split_unified_diff(ONE_FILE_DIFF)[0]
        ids = {h.rule_id for h in analyse_file(fd)}
        self.assertIn("blocking-sleep-async", ids)
        self.assertIn("mutable-default", ids)

    def test_sync_function_sleep_is_not_a_perf_finding(self):
        sync_diff = ONE_FILE_DIFF.replace("async def handler", "def handler")
        fd = split_unified_diff(sync_diff)[0]
        ids = {h.rule_id for h in analyse_file(fd)}
        self.assertNotIn("blocking-sleep-async", ids)

    def test_rules_ignore_non_python(self):
        fd = split_unified_diff(ONE_FILE_DIFF.replace("svc.py", "svc.md"))[0]
        self.assertEqual(analyse_file(fd), [])


class TestTools(unittest.TestCase):
    def setUp(self):
        self.files = {f.path: f for f in load_diff_file(str(SAMPLE))}
        self.ctx = ToolContext(files=self.files, repo_root=Path.cwd())

    def test_unknown_tool_returns_recoverable_error(self):
        out = execute_tool(self.ctx, "no_such_tool", {})
        self.assertTrue(out.startswith("ERROR:"))
        self.assertIn("get_diff_hunks", out)

    def test_missing_required_argument_is_reported_with_schema(self):
        out = execute_tool(self.ctx, "find_tests_for", {})
        self.assertIn("requires ['symbol']", out)

    def test_path_traversal_is_refused(self):
        out = execute_tool(self.ctx, "get_file_context",
                           {"path": "../../../../etc/passwd"})
        self.assertIn("escapes the repository root", out)

    def test_find_tests_for_sees_tests_in_the_diff(self):
        out = execute_tool(self.ctx, "find_tests_for", {"symbol": "put"})
        self.assertIn("tests/test_cache.py", out)

    def test_list_changed_files_marks_tests(self):
        out = execute_tool(self.ctx, "list_changed_files", {})
        self.assertIn("[test]", out)


class TestJudge(unittest.TestCase):
    @staticmethod
    def _finding(reviewer, line, title, conf, severity="medium", category="bug"):
        return Finding(path="a.py", line=line, severity=severity, category=category,
                       title=title, justification="j", confidence=conf,
                       reviewer=reviewer)

    def test_merges_near_duplicates_and_boosts_confidence(self):
        results = [
            ReviewResult(path="a.py", reviewer="correctness",
                         findings=[self._finding("correctness", 10, "Lock never released", 0.5)]),
            ReviewResult(path="a.py", reviewer="performance",
                         findings=[self._finding("performance", 11, "lock not released", 0.4)]),
        ]
        findings, stats = JudgeAgent(JudgeConfig()).judge(results)
        self.assertEqual(len(findings), 1)
        self.assertEqual(stats.merged_groups, 1)
        self.assertEqual(findings[0].agreed_by, ("performance",))
        self.assertGreater(findings[0].confidence, 0.5)

    def test_does_not_merge_different_categories(self):
        results = [ReviewResult(path="a.py", reviewer="r", findings=[
            self._finding("r", 10, "same line bug", 0.9, category="bug"),
            self._finding("r", 10, "same line style", 0.9, category="style"),
        ])]
        findings, _ = JudgeAgent(JudgeConfig()).judge(results)
        self.assertEqual(len(findings), 2)

    def test_filters_below_threshold(self):
        results = [ReviewResult(path="a.py", reviewer="r", findings=[
            self._finding("r", 1, "weak", 0.2), self._finding("r", 50, "strong", 0.8)])]
        findings, stats = JudgeAgent(JudgeConfig(min_confidence=0.5)).judge(results)
        self.assertEqual([f.title for f in findings], ["strong"])
        self.assertEqual(stats.dropped_low_confidence, 1)

    def test_keeps_corroborated_high_severity_below_threshold(self):
        """Two roles independently finding a high-severity defect survives."""
        results = [
            ReviewResult(path="a.py", reviewer="correctness", findings=[
                self._finding("correctness", 5, "race condition", 0.2, severity="high")]),
            ReviewResult(path="a.py", reviewer="performance", findings=[
                self._finding("performance", 5, "race condition", 0.2, severity="high")]),
        ]
        findings, _ = JudgeAgent(JudgeConfig(min_confidence=0.9)).judge(results)
        self.assertEqual(len(findings), 1)

    def test_caps_output(self):
        many = [self._finding("r", i * 10, f"issue {i}", 0.9) for i in range(40)]
        results = [ReviewResult(path="a.py", reviewer="r", findings=many)]
        findings, stats = JudgeAgent(JudgeConfig(max_findings=5)).judge(results)
        self.assertEqual(len(findings), 5)
        self.assertEqual(stats.dropped_over_cap, 35)

    def test_records_failed_reviewers(self):
        results = [ReviewResult(path="a.py", reviewer="tests", error="timeout")]
        _, stats = JudgeAgent().judge(results)
        self.assertEqual(len(stats.failed_reviewers), 1)
        self.assertIn("timeout", stats.failed_reviewers[0])


class TestReviewerAgent(unittest.TestCase):
    def test_agent_loop_calls_a_tool_then_answers(self):
        files = load_diff_file(str(SAMPLE))
        client = MockLLM(latency=0.0, jitter=0.0)
        client.register(files)
        ctx = ToolContext(files={f.path: f for f in files})
        agent = ReviewerAgent(client, role="performance")
        result = run(agent.review(files[0], ctx))
        self.assertTrue(result.ok, msg=result.error)
        self.assertGreaterEqual(result.steps, 2)
        self.assertTrue(result.tool_calls)
        self.assertTrue(result.findings)
        self.assertTrue(all(f.reviewer == "performance" for f in result.findings))

    def test_hallucinated_line_numbers_are_dropped(self):
        """A finding on a line the diff never touched must not be published."""
        fd = split_unified_diff(ONE_FILE_DIFF)[0]
        agent = ReviewerAgent(MockLLM(latency=0.0), role="correctness")
        kept = agent._validate(
            [{"line": 9999, "severity": "high", "category": "bug",
              "title": "imagined defect", "justification": "j"},
             {"line": 9, "severity": "high", "category": "bug",
              "title": "real defect", "justification": "j"}],
            fd)
        self.assertEqual([f.title for f in kept], ["real defect"])

    def test_near_miss_line_is_snapped_to_a_touched_line(self):
        fd = split_unified_diff(ONE_FILE_DIFF)[0]
        agent = ReviewerAgent(MockLLM(latency=0.0), role="correctness")
        kept = agent._validate(
            [{"line": 10, "severity": "low", "category": "style",
              "title": "off by one", "justification": "j"}], fd)
        self.assertEqual(len(kept), 1)
        self.assertIn(kept[0].line, {8, 9, 10})

    def test_transport_failure_is_captured_not_raised(self):
        client = MockLLM(latency=0.0, failure_rate=1.0)
        files = load_diff_file(str(SAMPLE))
        client.register(files)
        ctx = ToolContext(files={f.path: f for f in files})
        result = run(ReviewerAgent(client, role="tests").review(files[0], ctx))
        self.assertFalse(result.ok)
        self.assertIn("simulated", result.error or "")


class TestOrchestrator(unittest.TestCase):
    def setUp(self):
        self.files = load_diff_file(str(SAMPLE))

    def test_parallel_is_faster_and_overlaps(self):
        """The core claim: same work, same findings, real overlap, less wall clock."""
        def orchestrate(sequential: bool, concurrency: int):
            client = MockLLM(latency=0.05, jitter=0.0)
            cfg = OrchestratorConfig(concurrency=concurrency, sequential=sequential)
            orch = ReviewOrchestrator(client, cfg)
            return run(orch.run(self.files, target="t")), orch

        seq, seq_orch = orchestrate(True, 1)
        par, par_orch = orchestrate(False, 8)

        self.assertEqual(max_overlap(seq_orch.metrics), 1)
        self.assertGreaterEqual(max_overlap(par_orch.metrics), 4)
        self.assertLess(par.wall_clock_s, seq.wall_clock_s)
        self.assertEqual(len(seq.findings), len(par.findings))

    def test_semaphore_caps_in_flight_tasks(self):
        client = MockLLM(latency=0.02, jitter=0.0)
        orch = ReviewOrchestrator(client, OrchestratorConfig(concurrency=2))
        run(orch.run(self.files, target="t"))
        self.assertLessEqual(max_overlap(orch.metrics), 2)

    def test_one_failing_task_does_not_sink_the_run(self):
        """return_exceptions=True: a single bad agent must not cancel the rest."""
        client = MockLLM(latency=0.0, jitter=0.0, failure_rate=0.5, seed=7)
        orch = ReviewOrchestrator(client, OrchestratorConfig(concurrency=4))
        report = run(orch.run(self.files, target="t"))
        self.assertEqual(report.judge_stats["tasks"], len(self.files) * 3)
        self.assertTrue(any(not r.ok for r in report.results))
        self.assertTrue(any(r.ok for r in report.results))

    def test_task_count_is_files_times_roles(self):
        client = MockLLM(latency=0.0, jitter=0.0)
        orch = ReviewOrchestrator(
            client, OrchestratorConfig(concurrency=4, roles=("correctness", "style")))
        report = run(orch.run(self.files, target="t"))
        self.assertEqual(report.judge_stats["tasks"], len(self.files) * 2)

    def test_empty_target_is_handled(self):
        orch = ReviewOrchestrator(MockLLM(latency=0.0))
        report = run(orch.run([], target="empty"))
        self.assertEqual(report.files_reviewed, 0)
        self.assertEqual(report.findings, [])


class TestOverlapMetric(unittest.TestCase):
    def test_sequential_windows_give_overlap_one(self):
        metrics = [TaskMetric("a", "r", 0.0, 1.0, True),
                   TaskMetric("b", "r", 1.0, 2.0, True)]
        self.assertEqual(max_overlap(metrics), 1)

    def test_nested_windows_are_counted(self):
        metrics = [TaskMetric("a", "r", 0.0, 3.0, True),
                   TaskMetric("b", "r", 0.5, 1.0, True),
                   TaskMetric("c", "r", 0.6, 2.0, True)]
        self.assertEqual(max_overlap(metrics), 3)

    def test_no_metrics(self):
        self.assertEqual(max_overlap([]), 0)


class TestScoring(unittest.TestCase):
    def test_one_to_one_matching_prevents_double_credit(self):
        labels = [Label(path="a.py", line=10, category="bug")]
        findings = [
            Finding(path="a.py", line=10, severity="high", category="bug",
                    title="first", justification="j"),
            Finding(path="a.py", line=11, severity="high", category="bug",
                    title="second", justification="j"),
        ]
        score = score_findings(findings, labels)
        self.assertEqual((score.tp, score.fp, score.fn), (1, 1, 0))

    def test_category_mismatch_is_a_false_positive(self):
        labels = [Label(path="a.py", line=10, category="bug")]
        findings = [Finding(path="a.py", line=10, severity="low", category="style",
                            title="t", justification="j")]
        score = score_findings(findings, labels)
        self.assertEqual((score.tp, score.fp, score.fn), (0, 1, 1))

    def test_perfect_and_empty_scores(self):
        self.assertEqual(score_findings([], []).f1, 0.0)
        labels = [Label(path="a.py", line=1, category="bug")]
        findings = [Finding(path="a.py", line=1, severity="high", category="bug",
                            title="t", justification="j")]
        self.assertEqual(score_findings(findings, labels).f1, 1.0)

    def test_rule_baseline_finds_the_seeded_defects(self):
        files = load_diff_file(str(SAMPLE))
        findings = rule_baseline_findings(files)
        titles = " ".join(f.title for f in findings)
        self.assertIn("Mutable default", titles)
        self.assertIn("shell=True", titles)


if __name__ == "__main__":
    unittest.main(verbosity=2)
