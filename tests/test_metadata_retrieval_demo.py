from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from dataclasses import replace

import metadata_retrieval_demo as demo


class MetadataRetrievalDemoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.records = demo.load_jsonl(demo.DEFAULT_RECORDS)
        cls.queries = demo.load_jsonl(demo.DEFAULT_QUERIES)
        demo.validate_fixtures(cls.records, cls.queries)
        cls.failure_cases = demo.load_failure_cases(demo.DEFAULT_FAILURE_CASES)
        demo.validate_failure_cases(cls.records, cls.failure_cases)

    def setUp(self) -> None:
        self.connection = demo.build_database(self.records)

    def tearDown(self) -> None:
        self.connection.close()

    def test_fixture_schema_and_ids_are_valid(self) -> None:
        self.assertEqual(len(self.records), len({row["id"] for row in self.records}))
        self.assertEqual(len(self.queries), len({row["id"] for row in self.queries}))
        self.assertGreaterEqual(len(self.records), 30)
        self.assertGreaterEqual(len(self.queries), 10)

    def test_discriminative_mode_improves_retrieval_and_reduces_candidates(self) -> None:
        summaries = demo.compare_modes(self.connection, self.queries)
        content = summaries["content"]
        broad = summaries["broad"]
        discriminative = summaries["discriminative"]

        self.assertGreater(discriminative.hit_at_1, content.hit_at_1)
        self.assertGreater(discriminative.hit_at_1, broad.hit_at_1)
        self.assertGreaterEqual(discriminative.recall_at_3, content.recall_at_3)
        self.assertLess(
            discriminative.mean_candidate_set_size,
            content.mean_candidate_set_size,
        )

    def test_missing_metadata_is_reported_as_filter_false_negative(self) -> None:
        summary, results = demo.evaluate(self.connection, self.queries, "discriminative")
        by_query = {result.query_id: result for result in results}

        self.assertIn("q12_missing_entity_metadata", summary.filter_false_negative_queries)
        self.assertEqual(
            by_query["q12_missing_entity_metadata"].ranked_ids,
            (),
        )
        self.assertGreaterEqual(summary.filter_false_negatives, 1)

    def test_field_ablation_report_covers_every_filter_group(self) -> None:
        summaries = demo.run_ablations(self.connection, self.queries)
        self.assertEqual(set(summaries), {"none", *demo.FILTER_GROUPS})

        baseline = summaries["none"]
        changed_surfaces = 0
        for group in demo.FILTER_GROUPS:
            ablated = summaries[group]
            if (
                ablated.mean_candidate_set_size != baseline.mean_candidate_set_size
                or ablated.hit_at_1 != baseline.hit_at_1
                or ablated.filter_false_negatives != baseline.filter_false_negatives
            ):
                changed_surfaces += 1
        self.assertGreaterEqual(changed_surfaces, 6)

    def test_removing_incomplete_entity_filter_restores_expected_record(self) -> None:
        query = next(
            row for row in self.queries if row["id"] == "q12_missing_entity_metadata"
        )
        baseline = demo.run_query(self.connection, query, "discriminative")
        without_entity = demo.run_query(
            self.connection,
            query,
            "discriminative",
            ablated_groups=("entity",),
        )

        self.assertEqual(baseline.ranked_ids, ())
        self.assertIn("cedar-security-risk-missing-entity", without_entity.ranked_ids)

    def test_results_are_deterministic(self) -> None:
        first = demo.compare_modes(self.connection, self.queries)
        second = demo.compare_modes(self.connection, self.queries)
        self.assertEqual(first, second)

    def test_every_failure_bucket_has_at_least_one_case(self) -> None:
        counts = demo.failure_bucket_counts(self.queries)

        self.assertEqual(set(counts), set(demo.EVALUATION_FAILURE_BUCKETS))
        self.assertTrue(all(count > 0 for count in counts.values()))
        self.assertEqual(sum(counts.values()), len(self.queries))

    def test_failure_bucket_report_is_deterministic_and_user_friendly(self) -> None:
        first_summary = demo.summarize_failure_buckets(self.connection, self.queries)
        second_summary = demo.summarize_failure_buckets(self.connection, self.queries)
        self.assertEqual(first_summary, second_summary)

        first_output = io.StringIO()
        second_output = io.StringIO()
        with redirect_stdout(first_output):
            demo.print_failure_buckets(self.connection, self.queries)
        with redirect_stdout(second_output):
            demo.print_failure_buckets(self.connection, self.queries)

        self.assertEqual(first_output.getvalue(), second_output.getvalue())
        self.assertIn("Wrong time or stale version", first_output.getvalue())
        self.assertIn("Missing or inconsistent metadata", first_output.getvalue())
        self.assertIn("Filter FN", first_output.getvalue())

    def test_failure_registry_covers_every_bucket_with_valid_fixture_ids(self) -> None:
        buckets = {case.bucket for case in self.failure_cases}
        self.assertEqual(buckets, set(demo.FAILURE_BUCKETS))
        record_ids = {record["id"] for record in self.records}
        for case in self.failure_cases:
            referenced = set(case.expected_ids) | {
                exclusion.record_id for exclusion in case.expected_exclusions
            }
            self.assertTrue(referenced <= record_ids, case.case_id)

    def test_critical_failure_buckets_pass_independently(self) -> None:
        report = demo.run_failure_suite(
            self.connection, self.records, self.failure_cases
        )
        summaries = {(row.mode, row.bucket): row for row in report.summaries}
        for bucket in demo.CRITICAL_FAILURE_BUCKETS:
            summary = summaries[("discriminative", bucket)]
            self.assertEqual(summary.case_accuracy, 1.0, bucket)
            self.assertEqual(summary.wrong_context_rate, 0.0, bucket)
            self.assertEqual(summary.expected_exclusion_accuracy, 1.0, bucket)

    def test_expected_exclusions_have_the_declared_reason(self) -> None:
        results = {
            (result.case_id, result.mode): result
            for result in demo.run_failure_suite(
                self.connection, self.records, self.failure_cases
            ).results
        }
        for case in self.failure_cases:
            exclusions = results[(case.case_id, "discriminative")].trace.exclusions
            for expected in case.expected_exclusions:
                self.assertEqual(
                    exclusions.get(expected.record_id),
                    expected.reason,
                    f"{case.case_id}/{expected.record_id}",
                )

    def test_aggregate_success_cannot_override_a_critical_bucket_failure(self) -> None:
        report = demo.run_failure_suite(
            self.connection, self.records, self.failure_cases
        )
        summaries = list(report.summaries)
        index = next(
            index
            for index, summary in enumerate(summaries)
            if summary.mode == "discriminative" and summary.bucket == "wrong_scope"
        )
        summaries[index] = replace(summaries[index], case_accuracy=0.0)
        failures = demo.failure_gate_failures(summaries, within_time_bound=True)
        self.assertIn(
            "critical bucket failed independently: wrong_scope",
            failures,
        )

    def test_correction_preview_writes_nothing_and_admits_nothing(self) -> None:
        before = demo.DEFAULT_FAILURE_CASES.read_bytes()
        before_mtime = demo.DEFAULT_FAILURE_CASES.stat().st_mtime_ns
        expected_buckets = {
            "wrong project": "wrong_scope",
            "stale source": "stale_superseded_duplicate",
            "wrong period": "current_historical",
            "wrong record kind": "record_kind_granularity",
            "sensitive source": "eligibility_leakage",
            "should have refused": "underspecified_query",
            "should have asked for clarification": "ambiguous_entity",
        }
        for feedback, bucket in expected_buckets.items():
            preview = demo.preview_correction(feedback)
            self.assertEqual(preview["proposed_bucket"], bucket)
            self.assertEqual(preview["admission"], "not_admitted")
            self.assertFalse(preview["writes_performed"])
            self.assertFalse(preview["source_metadata_changed"])
            self.assertNotIn(
                preview["regression_case_draft"]["regression_status"],
                demo.REGRESSION_STATUSES,
            )
        self.assertEqual(demo.DEFAULT_FAILURE_CASES.read_bytes(), before)
        self.assertEqual(demo.DEFAULT_FAILURE_CASES.stat().st_mtime_ns, before_mtime)

    def test_failure_suite_repeated_runs_are_deterministic(self) -> None:
        first = demo.run_failure_suite(
            self.connection, self.records, self.failure_cases
        )
        second = demo.run_failure_suite(
            self.connection, self.records, self.failure_cases
        )
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
