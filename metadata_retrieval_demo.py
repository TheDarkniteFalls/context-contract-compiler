#!/usr/bin/env python3
"""Compare content-only, broad-tag, and discriminative SQLite retrieval."""

from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from statistics import mean
from typing import Iterable, Sequence


ROOT = Path(__file__).resolve().parent
DEFAULT_RECORDS = ROOT / "examples" / "context_items.jsonl"
DEFAULT_QUERIES = ROOT / "examples" / "eval_queries.jsonl"

MODES = ("content", "broad", "discriminative")
EXACT_FILTER_FIELDS = (
    "workspace",
    "project",
    "entity",
    "record_kind",
    "status",
    "source_kind",
)
FILTER_GROUPS = {
    "workspace": {"workspace"},
    "project": {"project"},
    "entity": {"entity"},
    "record_kind": {"record_kind"},
    "status": {"status"},
    "occurred_at": {"occurred_from", "occurred_to"},
    "validity": {"current_only", "as_of"},
    "source_kind": {"source_kind"},
}
RECORD_FIELDS = {
    "id",
    "title",
    "body",
    "workspace",
    "project",
    "entity",
    "record_kind",
    "status",
    "occurred_at",
    "valid_from",
    "valid_until",
    "superseded_by",
    "source_kind",
    "source_id",
    "broad_tags",
}
QUERY_FIELDS = {"id", "question", "search_terms", "filters", "expected_ids"}
ALLOWED_FILTER_FIELDS = set(EXACT_FILTER_FIELDS) | {
    "occurred_from",
    "occurred_to",
    "current_only",
    "as_of",
}
DATE_FIELDS = {"occurred_at", "valid_from", "valid_until"}


@dataclass(frozen=True)
class RetrievalResult:
    query_id: str
    mode: str
    ranked_ids: tuple[str, ...]

    @property
    def candidate_count(self) -> int:
        return len(self.ranked_ids)


@dataclass(frozen=True)
class MetricSummary:
    mode: str
    hit_at_1: float
    recall_at_3: float
    mrr: float
    mean_candidate_set_size: float
    filter_false_negatives: int
    filter_false_negative_queries: tuple[str, ...]
    query_count: int


def load_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
            if not isinstance(row, dict):
                raise ValueError(f"{path}:{line_number}: expected a JSON object")
            rows.append(row)
    return rows


def _validate_iso_date(value: object, label: str) -> None:
    if value is None:
        return
    if not isinstance(value, str):
        raise ValueError(f"{label} must be an ISO date string or null")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} is not an ISO date: {value!r}") from exc


def validate_fixtures(records: Sequence[dict], queries: Sequence[dict]) -> None:
    if not records or not queries:
        raise ValueError("record and query fixtures must not be empty")

    record_ids: set[str] = set()
    for index, record in enumerate(records, start=1):
        missing = RECORD_FIELDS - record.keys()
        extra = record.keys() - RECORD_FIELDS
        if missing or extra:
            raise ValueError(
                f"record {index} schema mismatch: missing={sorted(missing)} extra={sorted(extra)}"
            )
        record_id = record["id"]
        if not isinstance(record_id, str) or not record_id:
            raise ValueError(f"record {index} has an invalid id")
        if record_id in record_ids:
            raise ValueError(f"duplicate record id: {record_id}")
        record_ids.add(record_id)
        for field in RECORD_FIELDS - DATE_FIELDS - {"valid_until", "superseded_by"}:
            if not isinstance(record[field], str):
                raise ValueError(f"record {record_id} field {field} must be a string")
        for field in DATE_FIELDS:
            _validate_iso_date(record[field], f"record {record_id}.{field}")
        if record["superseded_by"] is not None and not isinstance(record["superseded_by"], str):
            raise ValueError(f"record {record_id}.superseded_by must be a string or null")

    query_ids: set[str] = set()
    for index, query in enumerate(queries, start=1):
        missing = QUERY_FIELDS - query.keys()
        extra = query.keys() - QUERY_FIELDS
        if missing or extra:
            raise ValueError(
                f"query {index} schema mismatch: missing={sorted(missing)} extra={sorted(extra)}"
            )
        query_id = query["id"]
        if not isinstance(query_id, str) or not query_id or query_id in query_ids:
            raise ValueError(f"query {index} has an invalid or duplicate id")
        query_ids.add(query_id)
        if not isinstance(query["question"], str) or not query["question"]:
            raise ValueError(f"query {query_id} needs a question")
        terms = query["search_terms"]
        if not isinstance(terms, list) or not terms or not all(isinstance(term, str) and term for term in terms):
            raise ValueError(f"query {query_id} needs non-empty string search_terms")
        expected_ids = query["expected_ids"]
        if not isinstance(expected_ids, list) or not expected_ids:
            raise ValueError(f"query {query_id} needs at least one expected id")
        unknown_expected = set(expected_ids) - record_ids
        if unknown_expected:
            raise ValueError(f"query {query_id} references unknown expected ids: {sorted(unknown_expected)}")
        filters = query["filters"]
        if not isinstance(filters, dict):
            raise ValueError(f"query {query_id}.filters must be an object")
        unknown_filters = filters.keys() - ALLOWED_FILTER_FIELDS
        if unknown_filters:
            raise ValueError(f"query {query_id} has unknown filters: {sorted(unknown_filters)}")
        if filters.get("current_only") and "as_of" not in filters:
            raise ValueError(f"query {query_id} current_only requires an explicit as_of date")
        for field in ("occurred_from", "occurred_to", "as_of"):
            if field in filters:
                _validate_iso_date(filters[field], f"query {query_id}.filters.{field}")


def build_database(records: Sequence[dict]) -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE context_items (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            workspace TEXT NOT NULL,
            project TEXT NOT NULL,
            entity TEXT NOT NULL,
            record_kind TEXT NOT NULL,
            status TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            valid_from TEXT NOT NULL,
            valid_until TEXT,
            superseded_by TEXT,
            source_kind TEXT NOT NULL,
            source_id TEXT NOT NULL,
            broad_tags TEXT NOT NULL
        );

        CREATE INDEX context_scope_idx
            ON context_items(workspace, project, record_kind, status);
        CREATE INDEX context_entity_idx
            ON context_items(entity, project);
        CREATE INDEX context_time_idx
            ON context_items(occurred_at, valid_from, valid_until);
        """
    )
    try:
        connection.executescript(
            """
            CREATE VIRTUAL TABLE content_fts USING fts5(
                id UNINDEXED,
                title,
                body
            );
            CREATE VIRTUAL TABLE broad_fts USING fts5(
                id UNINDEXED,
                title,
                body,
                broad_tags
            );
            """
        )
    except sqlite3.OperationalError as exc:
        connection.close()
        raise RuntimeError("This Python SQLite build does not provide FTS5") from exc

    insert_item = """
        INSERT INTO context_items (
            id, title, body, workspace, project, entity, record_kind, status,
            occurred_at, valid_from, valid_until, superseded_by, source_kind,
            source_id, broad_tags
        ) VALUES (
            :id, :title, :body, :workspace, :project, :entity, :record_kind, :status,
            :occurred_at, :valid_from, :valid_until, :superseded_by, :source_kind,
            :source_id, :broad_tags
        )
    """
    with connection:
        connection.executemany(insert_item, records)
        connection.executemany(
            "INSERT INTO content_fts (id, title, body) VALUES (?, ?, ?)",
            ((row["id"], row["title"], row["body"]) for row in records),
        )
        connection.executemany(
            "INSERT INTO broad_fts (id, title, body, broad_tags) VALUES (?, ?, ?, ?)",
            (
                (row["id"], row["title"], row["body"], row["broad_tags"])
                for row in records
            ),
        )
    return connection


def _fts_expression(search_terms: Sequence[str]) -> str:
    quoted = ['"' + term.replace('"', '""') + '"' for term in search_terms]
    return " OR ".join(quoted)


def _excluded_filter_keys(ablated_groups: Iterable[str]) -> set[str]:
    excluded: set[str] = set()
    for group in ablated_groups:
        try:
            excluded.update(FILTER_GROUPS[group])
        except KeyError as exc:
            raise ValueError(f"unknown ablation group: {group}") from exc
    return excluded


def run_query(
    connection: sqlite3.Connection,
    query: dict,
    mode: str,
    *,
    ablated_groups: Iterable[str] = (),
) -> RetrievalResult:
    if mode not in MODES:
        raise ValueError(f"unknown retrieval mode: {mode}")

    table = "broad_fts" if mode == "broad" else "content_fts"
    weights = "0.0, 5.0, 1.0, 1.5" if mode == "broad" else "0.0, 5.0, 1.0"
    clauses = [f"{table} MATCH ?"]
    params: list[object] = [_fts_expression(query["search_terms"])]

    if mode == "discriminative":
        excluded = _excluded_filter_keys(ablated_groups)
        filters = query["filters"]
        for field in EXACT_FILTER_FIELDS:
            if field in filters and field not in excluded:
                clauses.append(f"i.{field} = ?")
                params.append(filters[field])
        if "occurred_from" in filters and "occurred_from" not in excluded:
            clauses.append("i.occurred_at >= ?")
            params.append(filters["occurred_from"])
        if "occurred_to" in filters and "occurred_to" not in excluded:
            clauses.append("i.occurred_at <= ?")
            params.append(filters["occurred_to"])
        if filters.get("current_only") and "current_only" not in excluded:
            as_of = filters["as_of"]
            clauses.extend(
                [
                    "i.valid_from <= ?",
                    "(i.valid_until IS NULL OR i.valid_until >= ?)",
                    "i.superseded_by IS NULL",
                ]
            )
            params.extend([as_of, as_of])

    sql = f"""
        SELECT i.id
        FROM {table}
        JOIN context_items AS i ON i.id = {table}.id
        WHERE {' AND '.join(clauses)}
        ORDER BY bm25({table}, {weights}), i.id
    """
    ranked_ids = tuple(row["id"] for row in connection.execute(sql, params))
    return RetrievalResult(query_id=query["id"], mode=mode, ranked_ids=ranked_ids)


def evaluate(
    connection: sqlite3.Connection,
    queries: Sequence[dict],
    mode: str,
    *,
    ablated_groups: Iterable[str] = (),
) -> tuple[MetricSummary, tuple[RetrievalResult, ...]]:
    hit_scores: list[float] = []
    recall_scores: list[float] = []
    reciprocal_ranks: list[float] = []
    candidate_counts: list[int] = []
    false_negative_count = 0
    false_negative_queries: list[str] = []
    results: list[RetrievalResult] = []

    for query in queries:
        result = run_query(connection, query, mode, ablated_groups=ablated_groups)
        results.append(result)
        expected = set(query["expected_ids"])
        top_three = set(result.ranked_ids[:3])
        hit_scores.append(float(bool(result.ranked_ids and result.ranked_ids[0] in expected)))
        recall_scores.append(len(expected & top_three) / len(expected))
        reciprocal_rank = 0.0
        for rank, record_id in enumerate(result.ranked_ids, start=1):
            if record_id in expected:
                reciprocal_rank = 1.0 / rank
                break
        reciprocal_ranks.append(reciprocal_rank)
        candidate_counts.append(result.candidate_count)

        if mode == "discriminative":
            content_result = run_query(connection, query, "content")
            removed_expected = expected & set(content_result.ranked_ids) - set(result.ranked_ids)
            if removed_expected:
                false_negative_count += len(removed_expected)
                false_negative_queries.append(query["id"])

    summary = MetricSummary(
        mode=mode,
        hit_at_1=mean(hit_scores),
        recall_at_3=mean(recall_scores),
        mrr=mean(reciprocal_ranks),
        mean_candidate_set_size=mean(candidate_counts),
        filter_false_negatives=false_negative_count,
        filter_false_negative_queries=tuple(false_negative_queries),
        query_count=len(queries),
    )
    return summary, tuple(results)


def compare_modes(
    connection: sqlite3.Connection, queries: Sequence[dict]
) -> dict[str, MetricSummary]:
    return {mode: evaluate(connection, queries, mode)[0] for mode in MODES}


def run_ablations(
    connection: sqlite3.Connection, queries: Sequence[dict]
) -> dict[str, MetricSummary]:
    summaries = {"none": evaluate(connection, queries, "discriminative")[0]}
    for group in FILTER_GROUPS:
        summaries[group] = evaluate(
            connection,
            queries,
            "discriminative",
            ablated_groups=(group,),
        )[0]
    return summaries


def _print_metric_table(rows: Iterable[tuple[str, MetricSummary]]) -> None:
    print(
        f"{'Mode':<18} {'Hit@1':>7} {'Recall@3':>9} {'MRR':>7} "
        f"{'Avg candidates':>15} {'Filter FN':>10}"
    )
    print("-" * 73)
    for label, summary in rows:
        print(
            f"{label:<18} {summary.hit_at_1:>7.3f} {summary.recall_at_3:>9.3f} "
            f"{summary.mrr:>7.3f} {summary.mean_candidate_set_size:>15.2f} "
            f"{summary.filter_false_negatives:>10}"
        )


def print_comparison(connection: sqlite3.Connection, queries: Sequence[dict]) -> None:
    summaries = compare_modes(connection, queries)
    _print_metric_table((mode, summaries[mode]) for mode in MODES)
    discriminative = summaries["discriminative"]
    if discriminative.filter_false_negative_queries:
        query_list = ", ".join(discriminative.filter_false_negative_queries)
        print(f"\nFilter-induced false-negative queries: {query_list}")


def print_ablations(connection: sqlite3.Connection, queries: Sequence[dict]) -> None:
    summaries = run_ablations(connection, queries)
    rows = [("none removed", summaries["none"])]
    rows.extend((f"without {group}", summaries[group]) for group in FILTER_GROUPS)
    _print_metric_table(rows)
    print("\nA field can shrink the candidate set yet still hurt recall when its values are incomplete.")


def print_query(
    connection: sqlite3.Connection,
    records: Sequence[dict],
    query: dict,
    mode: str,
) -> None:
    record_by_id = {record["id"]: record for record in records}
    result = run_query(connection, query, mode)
    print(query["question"])
    print(f"Mode: {mode} | candidates: {result.candidate_count} | expected: {', '.join(query['expected_ids'])}")
    for rank, record_id in enumerate(result.ranked_ids[:3], start=1):
        record = record_by_id[record_id]
        print(f"{rank}. {record_id} — {record['title']}")
    if not result.ranked_ids:
        print("No candidates.")


def run_self_test(
    connection: sqlite3.Connection,
    records: Sequence[dict],
    queries: Sequence[dict],
) -> None:
    summaries = compare_modes(connection, queries)
    discriminative = summaries["discriminative"]
    content = summaries["content"]
    broad = summaries["broad"]

    assert discriminative.hit_at_1 > content.hit_at_1
    assert discriminative.hit_at_1 > broad.hit_at_1
    assert discriminative.recall_at_3 >= content.recall_at_3
    assert discriminative.mean_candidate_set_size < content.mean_candidate_set_size
    assert "q12_missing_entity_metadata" in discriminative.filter_false_negative_queries
    first = run_query(connection, queries[0], "discriminative")
    second = run_query(connection, queries[0], "discriminative")
    assert first == second

    print("PASS fts5_available")
    print(f"PASS fixtures_valid records={len(records)} queries={len(queries)}")
    print("PASS discriminative_retrieval_improves_hit_at_1")
    print("PASS discriminative_filters_shrink_candidate_sets")
    print("PASS filter_induced_false_negative_is_visible")
    print("PASS deterministic_repeat")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", type=Path, default=DEFAULT_RECORDS)
    parser.add_argument("--queries", type=Path, default=DEFAULT_QUERIES)
    parser.add_argument("--self-test", action="store_true")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("compare", help="Compare all three retrieval modes")
    subparsers.add_parser("ablate", help="Remove one structured filter group at a time")
    query_parser = subparsers.add_parser("query", help="Inspect one evaluation query")
    query_parser.add_argument("query_id")
    query_parser.add_argument("--mode", choices=MODES, default="discriminative")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    records = load_jsonl(args.records)
    queries = load_jsonl(args.queries)
    validate_fixtures(records, queries)
    connection = build_database(records)
    try:
        if args.self_test:
            run_self_test(connection, records, queries)
        elif args.command == "ablate":
            print_ablations(connection, queries)
        elif args.command == "query":
            query = next((item for item in queries if item["id"] == args.query_id), None)
            if query is None:
                raise SystemExit(f"Unknown query id: {args.query_id}")
            print_query(connection, records, query, args.mode)
        else:
            print_comparison(connection, queries)
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
