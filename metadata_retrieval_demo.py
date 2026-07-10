#!/usr/bin/env python3
"""Compare content-only, broad-tag, and discriminative SQLite retrieval."""

from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from statistics import mean
from time import perf_counter
from typing import Callable, Iterable, Mapping, Sequence


ROOT = Path(__file__).resolve().parent
DEFAULT_RECORDS = ROOT / "examples" / "context_items.jsonl"
DEFAULT_QUERIES = ROOT / "examples" / "eval_queries.jsonl"
DEFAULT_FAILURE_CASES = ROOT / "examples" / "failure_cases.jsonl"

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
EVALUATION_FAILURE_BUCKETS = {
    "scope_collision": (
        "Wrong scope or entity",
        "Similar wording points to the wrong workspace, project, or entity.",
    ),
    "record_kind_collision": (
        "Wrong record type",
        "A proposal, meeting, or task is mistaken for the requested decision or fact.",
    ),
    "stale_or_superseded": (
        "Wrong time or stale version",
        "An older record or a record outside the requested period outranks current truth.",
    ),
    "lifecycle_collision": (
        "Wrong lifecycle state",
        "Closed, draft, or completed work is returned when the user asked for open or approved work.",
    ),
    "provenance_conflict": (
        "Conflicting provenance",
        "A draft or discussion looks like an authoritative decision from the requested source.",
    ),
    "missing_or_misnormalized_metadata": (
        "Missing or inconsistent metadata",
        "The right record is removed because a useful field is empty or normalized differently.",
    ),
    "multi_result_retrieval": (
        "Multiple correct results",
        "The question needs a complete set of relevant records instead of one top answer.",
    ),
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
QUERY_FIELDS = {
    "id",
    "question",
    "search_terms",
    "filters",
    "expected_ids",
    "failure_bucket",
}
ALLOWED_FILTER_FIELDS = set(EXACT_FILTER_FIELDS) | {
    "occurred_from",
    "occurred_to",
    "current_only",
    "as_of",
}
DATE_FIELDS = {"occurred_at", "valid_from", "valid_until"}
FAILURE_BUCKETS = (
    "wrong_scope",
    "ambiguous_entity",
    "current_historical",
    "stale_superseded_duplicate",
    "record_kind_granularity",
    "eligibility_leakage",
    "authority_conflict",
    "underspecified_query",
    "malformed_metadata",
    "cross_source_conflict",
)
CRITICAL_FAILURE_BUCKETS = {
    "wrong_scope",
    "stale_superseded_duplicate",
    "eligibility_leakage",
    "authority_conflict",
}
FAILURE_CASE_FIELDS = {
    "id",
    "bucket",
    "query",
    "filters",
    "expected_ids",
    "expected_refusal",
    "expected_exclusions",
    "failure_class",
    "corrective_principle",
    "regression_status",
    "critical",
}
FAILURE_QUERY_FIELDS = {"question", "search_terms"}
REGRESSION_STATUSES = {"active", "known_limitation"}
EXCLUSION_REASONS = set(EXACT_FILTER_FIELDS) | {
    "occurred_from",
    "occurred_to",
    "validity",
    "supersession",
}
FAILURE_SUITE_MAX_SECONDS = 2.0
TOP_K = 3


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


@dataclass(frozen=True)
class ExpectedExclusion:
    record_id: str
    reason: str


@dataclass(frozen=True)
class FailureCase:
    case_id: str
    bucket: str
    question: str
    search_terms: tuple[str, ...]
    filters: Mapping[str, object]
    expected_ids: tuple[str, ...]
    expected_refusal: str | None
    expected_exclusions: tuple[ExpectedExclusion, ...]
    failure_class: str
    corrective_principle: str
    regression_status: str
    critical: bool

    def as_query(self) -> dict:
        return {
            "id": self.case_id,
            "question": self.question,
            "search_terms": list(self.search_terms),
            "filters": dict(self.filters),
            "expected_ids": list(self.expected_ids),
        }


@dataclass(frozen=True)
class PredicateTrace:
    reason: str
    predicate: str
    excluded_ids: tuple[str, ...]


@dataclass(frozen=True)
class RetrievalTrace:
    query_id: str
    mode: str
    applied_predicates: tuple[PredicateTrace, ...]
    initial_candidates: tuple[str, ...]
    eligible_candidates: tuple[str, ...]
    ranked_candidates: tuple[str, ...]
    selected_ids: tuple[str, ...]
    refusal_reason: str | None
    resolutions: tuple[str, ...]

    @property
    def exclusions(self) -> dict[str, str]:
        return {
            record_id: predicate.reason
            for predicate in self.applied_predicates
            for record_id in predicate.excluded_ids
        }


@dataclass(frozen=True)
class FailureCaseResult:
    case_id: str
    bucket: str
    mode: str
    expected_ids: tuple[str, ...]
    expected_refusal: str | None
    hit_at_1: float | None
    recall_at_3: float | None
    wrong_context: float
    refusal_correct: float | None
    expected_exclusion_accuracy: float
    case_accuracy: float
    passed: bool
    trace: RetrievalTrace


@dataclass(frozen=True)
class FailureMetricSummary:
    mode: str
    bucket: str
    case_count: int
    case_accuracy: float
    hit_at_1: float | None
    recall_at_3: float | None
    wrong_context_rate: float
    refusal_accuracy: float | None
    expected_exclusion_accuracy: float
    mean_candidate_set_size: float
    candidate_reduction: float


@dataclass(frozen=True)
class FailureSuiteReport:
    results: tuple[FailureCaseResult, ...]
    summaries: tuple[FailureMetricSummary, ...]
    elapsed_seconds: float = field(compare=False)
    within_time_bound: bool
    gate_failures: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.gate_failures


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


def load_failure_cases(path: Path) -> tuple[FailureCase, ...]:
    cases: list[FailureCase] = []
    for index, row in enumerate(load_jsonl(path), start=1):
        missing = FAILURE_CASE_FIELDS - row.keys()
        extra = row.keys() - FAILURE_CASE_FIELDS
        if missing or extra:
            raise ValueError(
                f"failure case {index} schema mismatch: "
                f"missing={sorted(missing)} extra={sorted(extra)}"
            )
        query = row["query"]
        if not isinstance(query, dict) or set(query) != FAILURE_QUERY_FIELDS:
            raise ValueError(
                f"failure case {index}.query must contain exactly "
                f"{sorted(FAILURE_QUERY_FIELDS)}"
            )
        exclusions = row["expected_exclusions"]
        if not isinstance(exclusions, list):
            raise ValueError(f"failure case {index}.expected_exclusions must be a list")
        typed_exclusions: list[ExpectedExclusion] = []
        for exclusion in exclusions:
            if not isinstance(exclusion, dict) or set(exclusion) != {"record_id", "reason"}:
                raise ValueError(
                    f"failure case {index} exclusions need record_id and reason"
                )
            typed_exclusions.append(
                ExpectedExclusion(exclusion["record_id"], exclusion["reason"])
            )
        cases.append(
            FailureCase(
                case_id=row["id"],
                bucket=row["bucket"],
                question=query["question"],
                search_terms=tuple(query["search_terms"]),
                filters=dict(row["filters"]),
                expected_ids=tuple(row["expected_ids"]),
                expected_refusal=row["expected_refusal"],
                expected_exclusions=tuple(typed_exclusions),
                failure_class=row["failure_class"],
                corrective_principle=row["corrective_principle"],
                regression_status=row["regression_status"],
                critical=row["critical"],
            )
        )
    return tuple(cases)


def _validate_iso_date(value: object, label: str) -> None:
    if value is None:
        return
    if not isinstance(value, str):
        raise ValueError(f"{label} must be an ISO date string or null")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} is not an ISO date: {value!r}") from exc


def validate_failure_cases(
    records: Sequence[dict], cases: Sequence[FailureCase]
) -> None:
    if not cases:
        raise ValueError("failure-case registry must not be empty")
    record_ids = {record["id"] for record in records}
    case_ids: set[str] = set()
    seen_buckets: set[str] = set()

    for case in cases:
        if not case.case_id or case.case_id in case_ids:
            raise ValueError(f"invalid or duplicate failure case id: {case.case_id!r}")
        case_ids.add(case.case_id)
        if case.bucket not in FAILURE_BUCKETS:
            raise ValueError(f"failure case {case.case_id} has unknown bucket {case.bucket!r}")
        seen_buckets.add(case.bucket)
        if case.critical != (case.bucket in CRITICAL_FAILURE_BUCKETS):
            raise ValueError(
                f"failure case {case.case_id}.critical disagrees with bucket policy"
            )
        if case.regression_status not in REGRESSION_STATUSES:
            raise ValueError(
                f"failure case {case.case_id} has invalid regression status"
            )
        if not case.question or not case.failure_class or not case.corrective_principle:
            raise ValueError(f"failure case {case.case_id} has empty explanatory fields")
        if not case.search_terms or not all(case.search_terms):
            raise ValueError(f"failure case {case.case_id} needs search terms")
        if set(case.filters) - ALLOWED_FILTER_FIELDS:
            raise ValueError(f"failure case {case.case_id} has unknown filters")
        if case.filters.get("current_only") and "as_of" not in case.filters:
            raise ValueError(
                f"failure case {case.case_id} current_only requires as_of"
            )
        for name in ("occurred_from", "occurred_to", "as_of"):
            if name in case.filters:
                _validate_iso_date(
                    case.filters[name], f"failure case {case.case_id}.{name}"
                )
        expects_selection = bool(case.expected_ids)
        expects_refusal = case.expected_refusal is not None
        if expects_selection == expects_refusal:
            raise ValueError(
                f"failure case {case.case_id} must expect selection or refusal, not both"
            )
        if case.expected_refusal is not None and not case.expected_refusal:
            raise ValueError(f"failure case {case.case_id} has empty refusal reason")
        unknown_expected = set(case.expected_ids) - record_ids
        if unknown_expected:
            raise ValueError(
                f"failure case {case.case_id} references unknown expected records: "
                f"{sorted(unknown_expected)}"
            )
        for exclusion in case.expected_exclusions:
            if exclusion.record_id not in record_ids:
                raise ValueError(
                    f"failure case {case.case_id} excludes unknown record "
                    f"{exclusion.record_id}"
                )
            if exclusion.reason not in EXCLUSION_REASONS:
                raise ValueError(
                    f"failure case {case.case_id} has unknown exclusion reason "
                    f"{exclusion.reason}"
                )

    missing_buckets = set(FAILURE_BUCKETS) - seen_buckets
    if missing_buckets:
        raise ValueError(f"failure-case registry lacks buckets: {sorted(missing_buckets)}")


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
        failure_bucket = query["failure_bucket"]
        if (
            not isinstance(failure_bucket, str)
            or failure_bucket not in EVALUATION_FAILURE_BUCKETS
        ):
            raise ValueError(
                f"query {query_id} has unknown failure_bucket: {failure_bucket!r}"
            )
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


def _predicate_specs(
    filters: Mapping[str, object],
) -> list[tuple[str, str, Callable[[dict], bool]]]:
    specs: list[tuple[str, str, Callable[[dict], bool]]] = []
    for field_name in EXACT_FILTER_FIELDS:
        if field_name in filters:
            expected = filters[field_name]
            specs.append(
                (
                    field_name,
                    f"{field_name} = {json.dumps(expected)}",
                    lambda record, name=field_name, value=expected: record[name] == value,
                )
            )
    if "occurred_from" in filters:
        occurred_from = filters["occurred_from"]
        specs.append(
            (
                "occurred_from",
                f"occurred_at >= {json.dumps(occurred_from)}",
                lambda record, value=occurred_from: record["occurred_at"] >= value,
            )
        )
    if "occurred_to" in filters:
        occurred_to = filters["occurred_to"]
        specs.append(
            (
                "occurred_to",
                f"occurred_at <= {json.dumps(occurred_to)}",
                lambda record, value=occurred_to: record["occurred_at"] <= value,
            )
        )
    if filters.get("current_only"):
        as_of = filters["as_of"]
        specs.extend(
            [
                (
                    "validity",
                    f"valid_from <= {json.dumps(as_of)} <= valid_until-or-open",
                    lambda record, value=as_of: record["valid_from"] <= value
                    and (
                        record["valid_until"] is None
                        or record["valid_until"] >= value
                    ),
                ),
                (
                    "supersession",
                    "superseded_by IS NULL",
                    lambda record: record["superseded_by"] is None,
                ),
            ]
        )
    return specs


def trace_retrieval(
    connection: sqlite3.Connection,
    records: Sequence[dict],
    query: dict,
    mode: str,
) -> RetrievalTrace:
    if mode not in {"content", "discriminative"}:
        raise ValueError(f"failure traces do not support mode: {mode}")
    record_by_id = {record["id"]: record for record in records}
    initial = run_query(connection, query, "content").ranked_ids
    eligible = list(initial)
    predicate_traces: list[PredicateTrace] = []

    if mode == "discriminative":
        for reason, label, predicate in _predicate_specs(query["filters"]):
            excluded = tuple(
                record_id
                for record_id in eligible
                if not predicate(record_by_id[record_id])
            )
            if excluded:
                excluded_set = set(excluded)
                eligible = [
                    record_id for record_id in eligible if record_id not in excluded_set
                ]
            predicate_traces.append(PredicateTrace(reason, label, excluded))

    ranked = tuple(eligible)
    refusal_reason: str | None = None
    if mode == "discriminative":
        if len(query["search_terms"]) < 2 and len(query["filters"]) < 2:
            refusal_reason = "insufficient_disambiguation"
        elif not ranked:
            refusal_reason = "no_eligible_candidates"
    selected = () if refusal_reason else ranked[:TOP_K]

    resolutions: list[str] = []
    if mode == "discriminative" and "entity" in query["filters"]:
        resolutions.append(
            f"canonical identity required entity={query['filters']['entity']}"
        )
    if mode == "discriminative" and "source_kind" in query["filters"]:
        resolutions.append(
            f"authority required source_kind={query['filters']['source_kind']}"
        )
    if mode == "discriminative":
        for record_id in initial:
            replacement = record_by_id[record_id]["superseded_by"]
            if replacement:
                resolutions.append(f"supersession {record_id} -> {replacement}")

    return RetrievalTrace(
        query_id=query["id"],
        mode=mode,
        applied_predicates=tuple(predicate_traces),
        initial_candidates=initial,
        eligible_candidates=ranked,
        ranked_candidates=ranked,
        selected_ids=selected,
        refusal_reason=refusal_reason,
        resolutions=tuple(dict.fromkeys(resolutions)),
    )


def evaluate_failure_case(
    connection: sqlite3.Connection,
    records: Sequence[dict],
    case: FailureCase,
    mode: str,
) -> FailureCaseResult:
    trace = trace_retrieval(connection, records, case.as_query(), mode)
    expected = set(case.expected_ids)
    if expected:
        hit_at_1 = float(bool(trace.selected_ids and trace.selected_ids[0] in expected))
        recall_at_3 = len(expected & set(trace.selected_ids[:TOP_K])) / len(expected)
        refusal_correct = None
        case_accuracy = float(hit_at_1 == 1.0 and recall_at_3 == 1.0)
        wrong_context = float(
            bool(trace.selected_ids and trace.selected_ids[0] not in expected)
        )
    else:
        hit_at_1 = None
        recall_at_3 = None
        refusal_correct = float(trace.refusal_reason == case.expected_refusal)
        case_accuracy = refusal_correct
        wrong_context = float(bool(trace.selected_ids))

    exclusions = trace.exclusions
    if case.expected_exclusions:
        matched = sum(
            exclusions.get(expected_exclusion.record_id) == expected_exclusion.reason
            for expected_exclusion in case.expected_exclusions
        )
        exclusion_accuracy = matched / len(case.expected_exclusions)
    else:
        exclusion_accuracy = 1.0
    passed = case_accuracy == 1.0 and exclusion_accuracy == 1.0
    return FailureCaseResult(
        case_id=case.case_id,
        bucket=case.bucket,
        mode=mode,
        expected_ids=case.expected_ids,
        expected_refusal=case.expected_refusal,
        hit_at_1=hit_at_1,
        recall_at_3=recall_at_3,
        wrong_context=wrong_context,
        refusal_correct=refusal_correct,
        expected_exclusion_accuracy=exclusion_accuracy,
        case_accuracy=case_accuracy,
        passed=passed,
        trace=trace,
    )


def _optional_mean(values: Sequence[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    return mean(present) if present else None


def summarize_failure_results(
    results: Sequence[FailureCaseResult], mode: str, bucket: str
) -> FailureMetricSummary:
    selected = [
        result
        for result in results
        if result.mode == mode and (bucket == "overall" or result.bucket == bucket)
    ]
    if not selected:
        raise ValueError(f"no failure results for {mode}/{bucket}")
    reductions = [
        (
            1.0
            - len(result.trace.eligible_candidates)
            / len(result.trace.initial_candidates)
            if result.trace.initial_candidates
            else 0.0
        )
        for result in selected
    ]
    return FailureMetricSummary(
        mode=mode,
        bucket=bucket,
        case_count=len(selected),
        case_accuracy=mean(result.case_accuracy for result in selected),
        hit_at_1=_optional_mean([result.hit_at_1 for result in selected]),
        recall_at_3=_optional_mean([result.recall_at_3 for result in selected]),
        wrong_context_rate=mean(result.wrong_context for result in selected),
        refusal_accuracy=_optional_mean(
            [result.refusal_correct for result in selected]
        ),
        expected_exclusion_accuracy=mean(
            result.expected_exclusion_accuracy for result in selected
        ),
        mean_candidate_set_size=mean(
            len(result.trace.eligible_candidates) for result in selected
        ),
        candidate_reduction=mean(reductions),
    )


def failure_gate_failures(
    summaries: Sequence[FailureMetricSummary], within_time_bound: bool
) -> tuple[str, ...]:
    by_key = {(summary.mode, summary.bucket): summary for summary in summaries}
    failures: list[str] = []
    overall = by_key[("discriminative", "overall")]
    if overall.case_accuracy < 1.0:
        failures.append("aggregate case accuracy is below 1.000")
    if overall.wrong_context_rate > 0.0:
        failures.append("aggregate wrong-context rate is above 0.000")
    if overall.expected_exclusion_accuracy < 1.0:
        failures.append("aggregate expected-exclusion accuracy is below 1.000")
    if not within_time_bound:
        failures.append(
            f"elapsed time exceeded {FAILURE_SUITE_MAX_SECONDS:.1f} seconds"
        )
    for bucket in sorted(CRITICAL_FAILURE_BUCKETS):
        summary = by_key[("discriminative", bucket)]
        if (
            summary.case_accuracy < 1.0
            or summary.wrong_context_rate > 0.0
            or summary.expected_exclusion_accuracy < 1.0
        ):
            failures.append(f"critical bucket failed independently: {bucket}")
    return tuple(failures)


def run_failure_suite(
    connection: sqlite3.Connection,
    records: Sequence[dict],
    cases: Sequence[FailureCase],
) -> FailureSuiteReport:
    started = perf_counter()
    results = tuple(
        evaluate_failure_case(connection, records, case, mode)
        for case in cases
        for mode in ("content", "discriminative")
    )
    summaries: list[FailureMetricSummary] = []
    for bucket in ("overall", *FAILURE_BUCKETS):
        for mode in ("content", "discriminative"):
            summaries.append(summarize_failure_results(results, mode, bucket))
    elapsed = perf_counter() - started
    within_time_bound = elapsed <= FAILURE_SUITE_MAX_SECONDS
    gate_failures = failure_gate_failures(summaries, within_time_bound)
    return FailureSuiteReport(
        results=results,
        summaries=tuple(summaries),
        elapsed_seconds=elapsed,
        within_time_bound=within_time_bound,
        gate_failures=gate_failures,
    )


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


def failure_bucket_counts(queries: Sequence[dict]) -> dict[str, int]:
    counts = {bucket: 0 for bucket in EVALUATION_FAILURE_BUCKETS}
    for query in queries:
        counts[query["failure_bucket"]] += 1
    return counts


def summarize_failure_buckets(
    connection: sqlite3.Connection, queries: Sequence[dict]
) -> dict[str, MetricSummary]:
    summaries: dict[str, MetricSummary] = {}
    for bucket in EVALUATION_FAILURE_BUCKETS:
        bucket_queries = [query for query in queries if query["failure_bucket"] == bucket]
        if bucket_queries:
            summaries[bucket] = evaluate(
                connection,
                bucket_queries,
                "discriminative",
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


def print_failure_buckets(
    connection: sqlite3.Connection, queries: Sequence[dict]
) -> None:
    summaries = summarize_failure_buckets(connection, queries)
    print("Discriminative retrieval results by failure bucket")
    print("Each row shows how the current metadata handles one recurring kind of mistake.\n")
    print(
        f"{'Failure bucket':<34} {'Cases':>5} {'Hit@1':>7} {'Recall@3':>9} "
        f"{'MRR':>7} {'Avg candidates':>15} {'Filter FN':>10}"
    )
    print("-" * 96)
    for bucket, summary in summaries.items():
        label = EVALUATION_FAILURE_BUCKETS[bucket][0]
        print(
            f"{label:<34} {summary.query_count:>5} {summary.hit_at_1:>7.3f} "
            f"{summary.recall_at_3:>9.3f} {summary.mrr:>7.3f} "
            f"{summary.mean_candidate_set_size:>15.2f} "
            f"{summary.filter_false_negatives:>10}"
        )

    print("\nWhat the buckets mean:")
    for bucket in summaries:
        label, description = EVALUATION_FAILURE_BUCKETS[bucket]
        print(f"- {label}: {description}")


def _format_optional(value: float | None) -> str:
    return "-" if value is None else f"{value:.3f}"


def _print_failure_metric_rows(rows: Sequence[FailureMetricSummary]) -> None:
    print(
        f"{'Bucket / mode':<43} {'Cases':>5} {'Case acc':>8} {'Hit@1':>7} "
        f"{'Recall@3':>8} {'Wrong':>7} {'Refusal':>8} {'Excl.':>7} "
        f"{'Avg cand.':>9} {'Reduction':>9}"
    )
    print("-" * 122)
    for summary in rows:
        label = f"{summary.bucket} / {summary.mode}"
        print(
            f"{label:<43} {summary.case_count:>5} {summary.case_accuracy:>8.3f} "
            f"{_format_optional(summary.hit_at_1):>7} "
            f"{_format_optional(summary.recall_at_3):>8} "
            f"{summary.wrong_context_rate:>7.3f} "
            f"{_format_optional(summary.refusal_accuracy):>8} "
            f"{summary.expected_exclusion_accuracy:>7.3f} "
            f"{summary.mean_candidate_set_size:>9.2f} "
            f"{summary.candidate_reduction:>9.3f}"
        )


def print_failure_suite(
    report: FailureSuiteReport, cases: Sequence[FailureCase]
) -> None:
    summaries = {(row.mode, row.bucket): row for row in report.summaries}
    print("Overall failure-suite comparison")
    _print_failure_metric_rows(
        [summaries[(mode, "overall")] for mode in ("content", "discriminative")]
    )
    print("\nPer-bucket comparison")
    _print_failure_metric_rows(
        [
            summaries[(mode, bucket)]
            for bucket in FAILURE_BUCKETS
            for mode in ("content", "discriminative")
        ]
    )

    result_by_key = {(row.case_id, row.mode): row for row in report.results}
    print("\nDeterministic case traces")
    for case in cases:
        baseline = result_by_key[(case.case_id, "content")]
        result = result_by_key[(case.case_id, "discriminative")]
        trace = result.trace
        expectation = (
            f"select={','.join(case.expected_ids)}"
            if case.expected_ids
            else f"refuse={case.expected_refusal}"
        )
        print(
            f"\n[{case.case_id}] bucket={case.bucket} critical={str(case.critical).lower()} "
            f"registry={case.regression_status} result={'PASS' if result.passed else 'FAIL'}"
        )
        print(f"  expectation: {expectation}")
        print(
            "  baseline selected: "
            + (", ".join(baseline.trace.selected_ids) or "none")
        )
        print("  initial candidates: " + (", ".join(trace.initial_candidates) or "none"))
        for predicate in trace.applied_predicates:
            excluded = ", ".join(predicate.excluded_ids) or "none"
            print(
                f"  predicate [{predicate.reason}] {predicate.predicate}; excluded: {excluded}"
            )
        print("  eligible candidates: " + (", ".join(trace.eligible_candidates) or "none"))
        print("  ranked candidates: " + (", ".join(trace.ranked_candidates) or "none"))
        if trace.refusal_reason:
            print(f"  refusal: {trace.refusal_reason}")
        else:
            print("  selected: " + (", ".join(trace.selected_ids) or "none"))
        for resolution in trace.resolutions:
            print(f"  resolution: {resolution}")

    print(
        f"\nElapsed: {report.elapsed_seconds:.6f}s "
        f"(bound <= {FAILURE_SUITE_MAX_SECONDS:.1f}s): "
        f"{'PASS' if report.within_time_bound else 'FAIL'}"
    )
    if report.gate_failures:
        for failure in report.gate_failures:
            print(f"GATE FAIL {failure}")
    else:
        print("Critical-bucket gate: PASS")


CORRECTION_BUCKET_RULES = (
    ("wrong project", "wrong_scope"),
    ("wrong workspace", "wrong_scope"),
    ("stale source", "stale_superseded_duplicate"),
    ("wrong period", "current_historical"),
    ("wrong record kind", "record_kind_granularity"),
    ("sensitive source", "eligibility_leakage"),
    ("should have refused", "underspecified_query"),
    ("should have asked for clarification", "ambiguous_entity"),
)


def preview_correction(feedback: str) -> dict:
    normalized = " ".join(feedback.lower().split())
    proposed_bucket = next(
        (bucket for phrase, bucket in CORRECTION_BUCKET_RULES if phrase in normalized),
        None,
    )
    return {
        "feedback": feedback,
        "proposed_bucket": proposed_bucket,
        "regression_case_draft": {
            "id": "INCOMPLETE",
            "bucket": proposed_bucket or "INCOMPLETE",
            "query": {"question": "INCOMPLETE", "search_terms": []},
            "filters": {},
            "expected_ids": [],
            "expected_refusal": None,
            "expected_exclusions": [],
            "failure_class": "INCOMPLETE",
            "corrective_principle": "INCOMPLETE",
            "regression_status": "proposed_not_admitted",
            "critical": proposed_bucket in CRITICAL_FAILURE_BUCKETS,
        },
        "admission": "not_admitted",
        "writes_performed": False,
        "source_metadata_changed": False,
        "required_before_admission": [
            "minimize the observed failure",
            "redact private or identifying details",
            "replace the observation with synthetic records and query text",
            "complete expected selection, refusal, and exclusion assertions",
            "obtain human review before adding a durable registry case",
        ],
    }


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
    failure_cases: Sequence[FailureCase],
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
    counts = failure_bucket_counts(queries)
    assert all(count > 0 for count in counts.values())
    first_bucket_summary = summarize_failure_buckets(connection, queries)
    second_bucket_summary = summarize_failure_buckets(connection, queries)
    assert first_bucket_summary == second_bucket_summary
    first = run_query(connection, queries[0], "discriminative")
    second = run_query(connection, queries[0], "discriminative")
    assert first == second
    failure_report = run_failure_suite(connection, records, failure_cases)
    assert failure_report.passed
    repeated_report = run_failure_suite(connection, records, failure_cases)
    assert failure_report == repeated_report

    print("PASS fts5_available")
    print(f"PASS fixtures_valid records={len(records)} queries={len(queries)}")
    print(f"PASS failure_registry_valid cases={len(failure_cases)}")
    print("PASS discriminative_retrieval_improves_hit_at_1")
    print("PASS discriminative_filters_shrink_candidate_sets")
    print("PASS filter_induced_false_negative_is_visible")
    print("PASS failure_bucket_coverage")
    print("PASS deterministic_bucket_reporting")
    print("PASS critical_failure_buckets")
    print("PASS deterministic_repeat")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", type=Path, default=DEFAULT_RECORDS)
    parser.add_argument("--queries", type=Path, default=DEFAULT_QUERIES)
    parser.add_argument("--failure-cases", type=Path, default=DEFAULT_FAILURE_CASES)
    parser.add_argument("--self-test", action="store_true")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("compare", help="Compare all three retrieval modes")
    subparsers.add_parser("ablate", help="Remove one structured filter group at a time")
    subparsers.add_parser(
        "buckets",
        help="Show discriminative retrieval results for each user-friendly failure category",
    )
    subparsers.add_parser(
        "failures", help="Replay the checked-in failure registry with traces"
    )
    query_parser = subparsers.add_parser("query", help="Inspect one evaluation query")
    query_parser.add_argument("query_id")
    query_parser.add_argument("--mode", choices=MODES, default="discriminative")
    correction_parser = subparsers.add_parser(
        "correction-preview",
        help="Preview a feedback-to-regression draft without writing or admitting it",
    )
    correction_parser.add_argument("feedback")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "correction-preview":
        print(json.dumps(preview_correction(args.feedback), indent=2, sort_keys=True))
        return 0

    records = load_jsonl(args.records)
    queries = load_jsonl(args.queries)
    validate_fixtures(records, queries)
    failure_cases = load_failure_cases(args.failure_cases)
    validate_failure_cases(records, failure_cases)
    connection = build_database(records)
    try:
        if args.self_test:
            run_self_test(connection, records, queries, failure_cases)
        elif args.command == "ablate":
            print_ablations(connection, queries)
        elif args.command == "buckets":
            print_failure_buckets(connection, queries)
        elif args.command == "failures":
            report = run_failure_suite(connection, records, failure_cases)
            print_failure_suite(report, failure_cases)
            return 0 if report.passed else 1
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
