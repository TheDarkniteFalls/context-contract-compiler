from __future__ import annotations

import unittest

import metadata_retrieval_demo as demo


class MetadataRetrievalDemoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.records = demo.load_jsonl(demo.DEFAULT_RECORDS)
        cls.queries = demo.load_jsonl(demo.DEFAULT_QUERIES)
        demo.validate_fixtures(cls.records, cls.queries)

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


if __name__ == "__main__":
    unittest.main()
