#!/usr/bin/env python3
"""Compile a task-specific context packet after enforcing declared boundaries."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import webbrowser
from datetime import date
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Mapping, Sequence
from urllib.parse import urlparse

from metadata_retrieval_demo import fts_expression, load_jsonl


ROOT = Path(__file__).resolve().parent
DEFAULT_RECORDS = ROOT / "examples" / "contextgate_records.jsonl"
DEFAULT_SCENARIO = ROOT / "examples" / "contextgate_scenario.json"
WEB_ROOT = ROOT / "web"

CONTRACT_FIELDS = {
    "task",
    "as_of",
    "project",
    "scope",
    "allowed_sources",
    "allowed_authorities",
    "required_record_ids",
    "forbidden_lifecycle_states",
    "forbidden_sensitivity_states",
    "token_budget",
}
RECORD_FIELDS = {
    "id",
    "title",
    "body",
    "tags",
    "project",
    "scope",
    "stable_identity",
    "identity_status",
    "valid_from",
    "valid_until",
    "superseded_by",
    "source_class",
    "authority",
    "review_state",
    "lifecycle",
    "sensitivity",
    "provenance",
    "token_count",
}
CONTROL_RECORDS = {
    "future_reveal": "FR-1050",
    "superseded_decision": "DEC-0410",
    "generated_draft": "DRAFT-0702",
    "rejected_plan": "PLAN-0901",
    "ambiguous_identity": "AMB-0606",
}
CONTROL_FIELDS = set(CONTROL_RECORDS) | {
    "remove_required_provenance",
    "tight_token_budget",
}
CONTROL_LABELS = {
    "future_reveal": "Future reveal",
    "superseded_decision": "Superseded decision",
    "generated_draft": "Generated draft",
    "rejected_plan": "Rejected plan",
    "ambiguous_identity": "Ambiguous identity",
    "remove_required_provenance": "Remove required provenance",
    "tight_token_budget": "Tight token budget",
}
RECEIPT_PROOF_FIELDS = {
    "receipt_id",
    "contract_fingerprint",
    "packet_fingerprint",
}
CHANGE_FIELDS = {"kind", "summary", "required_record_ids"}
FIRE_DRILL_FIELDS = {"id", "name", "change", "current_contract"}
STOP_WORDS = {
    "a",
    "an",
    "and",
    "for",
    "from",
    "in",
    "of",
    "on",
    "the",
    "this",
    "to",
    "with",
}
REQUIRED_REASON_CODES = {
    "EXCLUDE_PROJECT": "REQUIRED_PROJECT_MISMATCH",
    "EXCLUDE_SCOPE": "REQUIRED_SCOPE_MISMATCH",
    "EXCLUDE_IDENTITY": "REQUIRED_AMBIGUOUS_IDENTITY",
    "EXCLUDE_FUTURE": "REQUIRED_FUTURE",
    "EXCLUDE_EXPIRED": "REQUIRED_STALE",
    "EXCLUDE_SUPERSEDED": "REQUIRED_SUPERSEDED",
    "EXCLUDE_SOURCE": "REQUIRED_SOURCE_NOT_ALLOWED",
    "EXCLUDE_LIFECYCLE": "REQUIRED_FORBIDDEN_LIFECYCLE",
    "EXCLUDE_SENSITIVITY": "REQUIRED_FORBIDDEN_SENSITIVITY",
    "EXCLUDE_AUTHORITY": "REQUIRED_AUTHORITY_NOT_ALLOWED",
    "EXCLUDE_REVIEW": "REQUIRED_UNREVIEWED",
    "EXCLUDE_PROVENANCE": "REQUIRED_MISSING_PROVENANCE",
}


def _validate_iso_date(value: object, label: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be an ISO date string")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO date string") from exc


def validate_records(records: Sequence[dict]) -> None:
    if not records:
        raise ValueError("ContextGate requires at least one record")
    seen: set[str] = set()
    for index, record in enumerate(records, start=1):
        missing = RECORD_FIELDS - record.keys()
        extra = record.keys() - RECORD_FIELDS
        if missing or extra:
            raise ValueError(
                f"record {index} schema mismatch: "
                f"missing={sorted(missing)} extra={sorted(extra)}"
            )
        record_id = record["id"]
        if not isinstance(record_id, str) or not record_id:
            raise ValueError(f"record {index}.id must be a non-empty string")
        if record_id in seen:
            raise ValueError(f"duplicate record id: {record_id}")
        seen.add(record_id)
        for field in (
            "title",
            "body",
            "tags",
            "project",
            "scope",
            "stable_identity",
            "identity_status",
            "source_class",
            "authority",
            "review_state",
            "lifecycle",
            "sensitivity",
        ):
            if not isinstance(record[field], str) or not record[field]:
                raise ValueError(f"record {record_id}.{field} must be a non-empty string")
        _validate_iso_date(record["valid_from"], f"record {record_id}.valid_from")
        if record["valid_until"] is not None:
            _validate_iso_date(record["valid_until"], f"record {record_id}.valid_until")
            if record["valid_until"] < record["valid_from"]:
                raise ValueError(f"record {record_id} ends before it starts")
        if record["superseded_by"] is not None and not isinstance(
            record["superseded_by"], str
        ):
            raise ValueError(f"record {record_id}.superseded_by must be a string or null")
        if not isinstance(record["provenance"], list) or not all(
            isinstance(item, str) and item for item in record["provenance"]
        ):
            raise ValueError(f"record {record_id}.provenance must be a list of strings")
        token_count = record["token_count"]
        if isinstance(token_count, bool) or not isinstance(token_count, int) or token_count <= 0:
            raise ValueError(f"record {record_id}.token_count must be a positive integer")


def validate_contract(contract: Mapping[str, object]) -> dict:
    missing = CONTRACT_FIELDS - contract.keys()
    extra = contract.keys() - CONTRACT_FIELDS
    if missing or extra:
        raise ValueError(
            f"contract schema mismatch: missing={sorted(missing)} extra={sorted(extra)}"
        )
    normalized = copy.deepcopy(dict(contract))
    for field in ("task", "project", "scope"):
        if not isinstance(normalized[field], str) or not normalized[field].strip():
            raise ValueError(f"contract.{field} must be a non-empty string")
        normalized[field] = normalized[field].strip()
    _validate_iso_date(normalized["as_of"], "contract.as_of")
    for field in (
        "allowed_sources",
        "allowed_authorities",
        "required_record_ids",
        "forbidden_lifecycle_states",
        "forbidden_sensitivity_states",
    ):
        values = normalized[field]
        if not isinstance(values, list) or not all(
            isinstance(item, str) and item for item in values
        ):
            raise ValueError(f"contract.{field} must be a list of strings")
        if len(values) != len(set(values)):
            raise ValueError(f"contract.{field} contains duplicate values")
    budget = normalized["token_budget"]
    if isinstance(budget, bool) or not isinstance(budget, int) or budget <= 0:
        raise ValueError("contract.token_budget must be a positive integer")
    return normalized


def validate_controls(controls: Mapping[str, object]) -> dict[str, bool]:
    extra = controls.keys() - CONTROL_FIELDS
    if extra:
        raise ValueError(f"unknown controls: {sorted(extra)}")
    normalized = {field: False for field in CONTROL_FIELDS}
    for field, value in controls.items():
        if not isinstance(value, bool):
            raise ValueError(f"control {field} must be boolean")
        normalized[field] = value
    return normalized


def validate_change(change: Mapping[str, object]) -> dict:
    if not isinstance(change, Mapping):
        raise ValueError("change must be an object")
    missing = CHANGE_FIELDS - change.keys()
    extra = change.keys() - CHANGE_FIELDS
    if missing or extra:
        raise ValueError(
            f"change schema mismatch: missing={sorted(missing)} extra={sorted(extra)}"
        )
    normalized = copy.deepcopy(dict(change))
    if normalized["kind"] not in {"none", "late_user_correction"}:
        raise ValueError("change.kind must be none or late_user_correction")
    if not isinstance(normalized["summary"], str):
        raise ValueError("change.summary must be a string")
    normalized["summary"] = normalized["summary"].strip()
    required_ids = normalized["required_record_ids"]
    if not isinstance(required_ids, list) or not all(
        isinstance(item, str) and item for item in required_ids
    ):
        raise ValueError("change.required_record_ids must be a list of strings")
    if len(required_ids) != len(set(required_ids)):
        raise ValueError("change.required_record_ids contains duplicate values")
    if normalized["kind"] == "none" and (normalized["summary"] or required_ids):
        raise ValueError("a none change must have an empty summary and required_record_ids")
    if normalized["kind"] == "late_user_correction" and (
        not normalized["summary"] or not required_ids
    ):
        raise ValueError(
            "a late_user_correction needs a summary and required_record_ids"
        )
    return normalized


def validate_receipt_proof(proof: Mapping[str, object]) -> dict[str, str]:
    if not isinstance(proof, Mapping):
        raise ValueError("prior_receipt must be an object")
    missing = RECEIPT_PROOF_FIELDS - proof.keys()
    extra = proof.keys() - RECEIPT_PROOF_FIELDS
    if missing or extra:
        raise ValueError(
            "prior_receipt schema mismatch: "
            f"missing={sorted(missing)} extra={sorted(extra)}"
        )
    normalized: dict[str, str] = {}
    for field in sorted(RECEIPT_PROOF_FIELDS):
        value = proof[field]
        if not isinstance(value, str) or not value:
            raise ValueError(f"prior_receipt.{field} must be a non-empty string")
        normalized[field] = value
    return normalized


def validate_fire_drill(drill: Mapping[str, object], base_contract: Mapping[str, object]) -> dict:
    if not isinstance(drill, Mapping) or set(drill) != FIRE_DRILL_FIELDS:
        raise ValueError(f"fire_drill must contain exactly {sorted(FIRE_DRILL_FIELDS)}")
    normalized = copy.deepcopy(dict(drill))
    for field in ("id", "name"):
        if not isinstance(normalized[field], str) or not normalized[field].strip():
            raise ValueError(f"fire_drill.{field} must be a non-empty string")
        normalized[field] = normalized[field].strip()
    normalized["change"] = validate_change(normalized["change"])
    normalized["current_contract"] = validate_contract(normalized["current_contract"])
    missing_requirements = set(normalized["change"]["required_record_ids"]) - set(
        normalized["current_contract"]["required_record_ids"]
    )
    if missing_requirements:
        raise ValueError(
            "fire_drill current_contract does not apply required records: "
            f"{sorted(missing_requirements)}"
        )
    newly_required = set(normalized["change"]["required_record_ids"]) - set(
        base_contract["required_record_ids"]
    )
    if not newly_required:
        raise ValueError("fire_drill must add at least one required record")
    return normalized


def load_scenario(path: Path = DEFAULT_SCENARIO) -> dict:
    with path.open(encoding="utf-8") as handle:
        scenario = json.load(handle)
    expected = {"id", "name", "contract", "controls", "fire_drill"}
    if not isinstance(scenario, dict) or set(scenario) != expected:
        raise ValueError(f"scenario must contain exactly {sorted(expected)}")
    if not isinstance(scenario["id"], str) or not isinstance(scenario["name"], str):
        raise ValueError("scenario id and name must be strings")
    scenario["contract"] = validate_contract(scenario["contract"])
    scenario["controls"] = validate_controls(scenario["controls"])
    scenario["fire_drill"] = validate_fire_drill(
        scenario["fire_drill"], scenario["contract"]
    )
    return scenario


def extract_search_terms(task: str) -> tuple[str, ...]:
    terms: list[str] = []
    for term in re.findall(r"[a-z0-9]+", task.lower()):
        if len(term) < 3 or term in STOP_WORDS or term in terms:
            continue
        terms.append(term)
    if not terms:
        raise ValueError("contract.task must contain at least one searchable term")
    return tuple(terms[:16])


def rank_records(records: Sequence[dict], task: str) -> tuple[str, ...]:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        try:
            connection.execute(
                """
                CREATE VIRTUAL TABLE contextgate_fts USING fts5(
                    id UNINDEXED,
                    title,
                    body,
                    tags
                )
                """
            )
        except sqlite3.OperationalError as exc:
            raise RuntimeError("This Python SQLite build does not provide FTS5") from exc
        connection.executemany(
            "INSERT INTO contextgate_fts (id, title, body, tags) VALUES (?, ?, ?, ?)",
            (
                (record["id"], record["title"], record["body"], record["tags"])
                for record in records
            ),
        )
        expression = fts_expression(extract_search_terms(task))
        rows = connection.execute(
            """
            SELECT id
            FROM contextgate_fts
            WHERE contextgate_fts MATCH ?
            ORDER BY bm25(contextgate_fts, 0.0, 5.0, 2.0, 1.5), id
            """,
            (expression,),
        )
        return tuple(row["id"] for row in rows)
    finally:
        connection.close()


def apply_controls(records: Sequence[dict], controls: Mapping[str, bool]) -> list[dict]:
    hidden_ids = {
        record_id
        for control, record_id in CONTROL_RECORDS.items()
        if not controls.get(control, False)
    }
    active = [copy.deepcopy(record) for record in records if record["id"] not in hidden_ids]
    if controls.get("remove_required_provenance", False):
        for record in active:
            if record["id"] == "ADR-0234":
                record["provenance"] = []
    return active


def first_violation(record: Mapping[str, object], contract: Mapping[str, object]) -> tuple[str, str] | None:
    as_of = str(contract["as_of"])
    if record["project"] != contract["project"]:
        return (
            "EXCLUDE_PROJECT",
            f'project {record["project"]} != {contract["project"]}',
        )
    if record["scope"] != contract["scope"]:
        return (
            "EXCLUDE_SCOPE",
            f'scope {record["scope"]} != {contract["scope"]}',
        )
    if record["identity_status"] != "resolved" or not record["stable_identity"]:
        return (
            "EXCLUDE_IDENTITY",
            f'stable identity is {record["identity_status"]}',
        )
    if record["valid_from"] > as_of:
        return (
            "EXCLUDE_FUTURE",
            f'valid_from {record["valid_from"]} > as_of {as_of}',
        )
    if record["superseded_by"] is not None:
        return (
            "EXCLUDE_SUPERSEDED",
            f'superseded_by {record["superseded_by"]}',
        )
    if record["valid_until"] is not None and record["valid_until"] < as_of:
        return (
            "EXCLUDE_EXPIRED",
            f'valid_until {record["valid_until"]} < as_of {as_of}',
        )
    if record["source_class"] not in contract["allowed_sources"]:
        return (
            "EXCLUDE_SOURCE",
            f'source {record["source_class"]} is not allowed',
        )
    if record["lifecycle"] in contract["forbidden_lifecycle_states"]:
        return (
            "EXCLUDE_LIFECYCLE",
            f'lifecycle {record["lifecycle"]} is forbidden',
        )
    if record["sensitivity"] in contract["forbidden_sensitivity_states"]:
        return (
            "EXCLUDE_SENSITIVITY",
            f'sensitivity {record["sensitivity"]} is forbidden',
        )
    if record["authority"] not in contract["allowed_authorities"]:
        return (
            "EXCLUDE_AUTHORITY",
            f'authority {record["authority"]} is not allowed',
        )
    if record["review_state"] != "approved":
        return (
            "EXCLUDE_REVIEW",
            f'review_state {record["review_state"]} is not approved',
        )
    if not record["provenance"]:
        return (
            "EXCLUDE_PROVENANCE",
            "provenance is missing",
        )
    return None


def public_record(record: Mapping[str, object]) -> dict:
    return {
        "id": record["id"],
        "title": record["title"],
        "body": record["body"],
        "project": record["project"],
        "scope": record["scope"],
        "stable_identity": record["stable_identity"],
        "identity_status": record["identity_status"],
        "valid_from": record["valid_from"],
        "valid_until": record["valid_until"],
        "superseded_by": record["superseded_by"],
        "source_class": record["source_class"],
        "authority": record["authority"],
        "review_state": record["review_state"],
        "lifecycle": record["lifecycle"],
        "sensitivity": record["sensitivity"],
        "provenance": list(record["provenance"]),
        "token_count": record["token_count"],
    }


def _selected_record(
    record: Mapping[str, object],
    *,
    required: bool,
    rank_position: int | None,
    warning: tuple[str, str] | None = None,
) -> dict:
    item = public_record(record)
    item.update(
        {
            "required": required,
            "relevance_rank": rank_position,
            "warning_code": warning[0] if warning else None,
            "warning_fact": warning[1] if warning else None,
        }
    )
    return item


def compile_naive(
    records: Sequence[dict],
    contract: Mapping[str, object],
    ranked_ids: Sequence[str],
) -> dict:
    by_id = {record["id"]: record for record in records}
    remaining = int(contract["token_budget"])
    selected: list[dict] = []
    for rank_position, record_id in enumerate(ranked_ids, start=1):
        record = by_id[record_id]
        if record["token_count"] > remaining:
            continue
        warning = first_violation(record, contract)
        selected.append(
            _selected_record(
                record,
                required=record_id in contract["required_record_ids"],
                rank_position=rank_position,
                warning=warning,
            )
        )
        remaining -= record["token_count"]
    selected_ids = {record["id"] for record in selected}
    missing_required = [
        record_id
        for record_id in contract["required_record_ids"]
        if record_id not in selected_ids
    ]
    illegal_count = sum(record["warning_code"] is not None for record in selected)
    return {
        "status": "unsafe_or_incomplete" if illegal_count or missing_required else "complete",
        "selected_records": selected,
        "selected_count": len(selected),
        "token_count": int(contract["token_budget"]) - remaining,
        "remaining_tokens": remaining,
        "illegal_selected_count": illegal_count,
        "missing_required_ids": missing_required,
    }


def _failure_message(reason_code: str, record_id: str | None) -> tuple[str, str]:
    if reason_code == "REQUIRED_NOT_FOUND":
        return (
            "Required record is absent",
            f"{record_id} was not supplied. No packet was emitted.",
        )
    if reason_code == "REQUIRED_MISSING_PROVENANCE":
        return (
            "Required record is unsupported",
            f"{record_id} has no declared provenance. No packet was emitted.",
        )
    if reason_code == "REQUIRED_OVER_BUDGET":
        return (
            "Required records exceed the budget",
            f"The required set cannot fit. No packet was emitted.",
        )
    return (
        "Required record violates the contract",
        f"{record_id} failed {reason_code}. No packet was emitted.",
    )


def _fingerprint(prefix: str, value: object) -> str:
    material = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return prefix + "-" + hashlib.sha256(material).hexdigest()[:16]


def _receipt_id(contract: Mapping[str, object], trace: Sequence[dict]) -> str:
    material = json.dumps(
        {
            "contract": contract,
            "decisions": [
                (row["record_id"], row["reason_code"], row["selected"])
                for row in trace
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "cg-" + hashlib.sha256(material).hexdigest()[:16]


def _contract_fingerprint(contract: Mapping[str, object]) -> str:
    return _fingerprint("cgc", contract)


def _packet_fingerprint(packet: Sequence[dict]) -> str:
    canonical_packet = [
        {
            key: value
            for key, value in record.items()
            if key not in {"relevance_rank", "warning_code", "warning_fact"}
        }
        for record in packet
    ]
    return _fingerprint("cgp", canonical_packet)


def _packet_text(contract: Mapping[str, object], packet: Sequence[dict]) -> str:
    lines = [
        "ContextGate packet",
        f'Task: {contract["task"]}',
        f'As of: {contract["as_of"]}',
        f'Project / scope: {contract["project"]} / {contract["scope"]}',
        f'Token budget: {contract["token_budget"]}',
        "",
    ]
    for record in packet:
        lines.extend(
            [
                f'[{record["id"]}] {record["title"]}',
                str(record["body"]),
                (
                    f'Source: {record["source_class"]} · '
                    f'Authority: {record["authority"]} · '
                    f'Tokens: {record["token_count"]}'
                ),
                f'Provenance: {", ".join(record["provenance"])}',
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def compile_context(
    records: Sequence[dict],
    contract: Mapping[str, object],
    controls: Mapping[str, object] | None = None,
) -> dict:
    validate_records(records)
    normalized_contract = validate_contract(contract)
    normalized_controls = validate_controls(controls or {})
    active_records = apply_controls(records, normalized_controls)
    requested_budget = normalized_contract["token_budget"]
    if normalized_controls["tight_token_budget"]:
        normalized_contract["token_budget"] = min(
            normalized_contract["token_budget"], 12
        )

    ranked_ids = rank_records(active_records, normalized_contract["task"])
    rank_positions = {record_id: index for index, record_id in enumerate(ranked_ids, 1)}
    by_id = {record["id"]: record for record in active_records}
    violations = {
        record["id"]: first_violation(record, normalized_contract)
        for record in active_records
    }
    naive = compile_naive(active_records, normalized_contract, ranked_ids)

    required_ids = list(normalized_contract["required_record_ids"])
    failure_code: str | None = None
    failure_record_id: str | None = None
    missing_required = [record_id for record_id in required_ids if record_id not in by_id]
    if missing_required:
        failure_code = "REQUIRED_NOT_FOUND"
        failure_record_id = missing_required[0]
    else:
        for record_id in required_ids:
            violation = violations[record_id]
            if violation is not None:
                failure_code = REQUIRED_REASON_CODES[violation[0]]
                failure_record_id = record_id
                break
    required_tokens = sum(
        by_id[record_id]["token_count"]
        for record_id in required_ids
        if record_id in by_id
    )
    if failure_code is None and required_tokens > normalized_contract["token_budget"]:
        failure_code = "REQUIRED_OVER_BUDGET"
        failure_record_id = required_ids[0] if required_ids else None

    packet: list[dict] = []
    trace_by_id: dict[str, dict] = {}
    budget_remaining = int(normalized_contract["token_budget"])
    if failure_code is None:
        for record_id in required_ids:
            record = by_id[record_id]
            packet.append(
                _selected_record(
                    record,
                    required=True,
                    rank_position=rank_positions.get(record_id),
                )
            )
            budget_remaining -= record["token_count"]
            trace_by_id[record_id] = {
                "record_id": record_id,
                "title": record["title"],
                "selected": True,
                "required": True,
                "reason_code": "SELECTED_REQUIRED",
                "boundary_fact": (
                    f'required · provenance {len(record["provenance"])} source(s)'
                ),
                "token_count": record["token_count"],
                "relevance_rank": rank_positions.get(record_id),
                "record": public_record(record),
            }

        for record_id in ranked_ids:
            if record_id in trace_by_id:
                continue
            record = by_id[record_id]
            violation = violations[record_id]
            if violation is not None:
                continue
            if record["token_count"] <= budget_remaining:
                packet.append(
                    _selected_record(
                        record,
                        required=False,
                        rank_position=rank_positions[record_id],
                    )
                )
                budget_remaining -= record["token_count"]
                trace_by_id[record_id] = {
                    "record_id": record_id,
                    "title": record["title"],
                    "selected": True,
                    "required": False,
                    "reason_code": "SELECTED_RANKED",
                    "boundary_fact": (
                        f'rank {rank_positions[record_id]} after legality'
                    ),
                    "token_count": record["token_count"],
                    "relevance_rank": rank_positions[record_id],
                    "record": public_record(record),
                }

        for record in active_records:
            record_id = record["id"]
            if record_id in trace_by_id:
                continue
            violation = violations[record_id]
            if violation is not None:
                reason_code, fact = violation
            elif record_id not in rank_positions:
                reason_code, fact = (
                    "EXCLUDE_NOT_RELEVANT",
                    "no task-term match after legality",
                )
            else:
                reason_code, fact = (
                    "EXCLUDE_BUDGET",
                    f'{record["token_count"]} tokens do not fit {budget_remaining} remaining',
                )
            trace_by_id[record_id] = {
                "record_id": record_id,
                "title": record["title"],
                "selected": False,
                "required": record_id in required_ids,
                "reason_code": reason_code,
                "boundary_fact": fact,
                "token_count": record["token_count"],
                "relevance_rank": rank_positions.get(record_id),
                "record": public_record(record),
            }
    else:
        for record in active_records:
            record_id = record["id"]
            violation = violations[record_id]
            if record_id == failure_record_id:
                reason_code = failure_code
                if failure_code == "REQUIRED_OVER_BUDGET":
                    fact = (
                        f'{required_tokens} required tokens > '
                        f'{normalized_contract["token_budget"]} budget'
                    )
                elif violation is not None:
                    fact = violation[1]
                else:
                    fact = "required record blocks compilation"
            elif violation is not None:
                reason_code, fact = violation
            else:
                reason_code, fact = (
                    "EXCLUDE_FAIL_CLOSED",
                    "no packet emitted after required-record failure",
                )
            trace_by_id[record_id] = {
                "record_id": record_id,
                "title": record["title"],
                "selected": False,
                "required": record_id in required_ids,
                "reason_code": reason_code,
                "boundary_fact": fact,
                "token_count": record["token_count"],
                "relevance_rank": rank_positions.get(record_id),
                "record": public_record(record),
            }
        for record_id in missing_required:
            trace_by_id[record_id] = {
                "record_id": record_id,
                "title": "Missing required record",
                "selected": False,
                "required": True,
                "reason_code": "REQUIRED_NOT_FOUND",
                "boundary_fact": "record was not supplied",
                "token_count": 0,
                "relevance_rank": None,
                "record": None,
            }
        budget_remaining = int(normalized_contract["token_budget"])

    selected_ids = [record["id"] for record in packet]
    trace = sorted(
        trace_by_id.values(),
        key=lambda row: (
            0
            if row["record_id"] in selected_ids
            or (row["required"] and row["reason_code"].startswith("REQUIRED_"))
            else 1,
            selected_ids.index(row["record_id"])
            if row["record_id"] in selected_ids
            else -1
            if row["required"] and row["reason_code"].startswith("REQUIRED_")
            else row["relevance_rank"] or 10_000,
            row["record_id"],
        ),
    )
    status = "compiled" if failure_code is None else "fail_closed"
    failure = None
    if failure_code is not None:
        title, message = _failure_message(failure_code, failure_record_id)
        failure = {
            "reason_code": failure_code,
            "record_id": failure_record_id,
            "title": title,
            "message": message,
        }
    token_count = sum(record["token_count"] for record in packet)
    return {
        "product": "ContextGate",
        "status": status,
        "contract": normalized_contract,
        "requested_token_budget": requested_budget,
        "controls": normalized_controls,
        "naive": naive,
        "packet": packet,
        "packet_text": _packet_text(normalized_contract, packet) if packet else "",
        "trace": trace,
        "failure": failure,
        "summary": {
            "candidate_count": len(active_records),
            "trace_count": len(trace),
            "selected_count": len(packet),
            "excluded_count": sum(
                not row["selected"] and row["record"] is not None for row in trace
            ),
            "missing_required_count": sum(row["record"] is None for row in trace),
            "token_count": token_count,
            "remaining_tokens": budget_remaining,
            "required_tokens": required_tokens,
            "optional_tokens": max(0, token_count - required_tokens),
        },
        "receipt_id": _receipt_id(normalized_contract, trace),
        "contract_fingerprint": _contract_fingerprint(normalized_contract),
        "packet_fingerprint": _packet_fingerprint(packet),
        "proof_boundary": (
            "This receipt proves the declared selection contract over the supplied "
            "structured records; it does not prove record truth or downstream answer correctness."
        ),
    }


def receipt_proof(result: Mapping[str, object]) -> dict[str, str]:
    return {
        field: str(result[field])
        for field in ("receipt_id", "contract_fingerprint", "packet_fingerprint")
    }


def evaluate_context_staleness(
    records: Sequence[dict],
    prior_receipt: Mapping[str, object],
    change: Mapping[str, object],
    current_contract: Mapping[str, object],
    controls: Mapping[str, object] | None = None,
) -> dict:
    """Compare a supplied receipt with a freshly compiled current context.

    This is a request-stateless comparison over declared inputs. It does not observe
    an agent process or discover changes on its own.
    """

    prior = validate_receipt_proof(prior_receipt)
    normalized_change = validate_change(change)
    current = compile_context(records, current_contract, controls)
    current_proof = receipt_proof(current)
    mismatch_order = (
        "contract_fingerprint",
        "packet_fingerprint",
        "receipt_id",
    )
    mismatches = [
        field for field in mismatch_order if prior[field] != current_proof[field]
    ]
    missing_late_requirements = [
        record_id
        for record_id in normalized_change["required_record_ids"]
        if record_id not in current["contract"]["required_record_ids"]
    ]

    if missing_late_requirements:
        outcome = "block"
        reason_code = "LATE_USER_INPUT_NOT_APPLIED"
        boundary_fact = (
            "Late correction requires "
            f"{', '.join(missing_late_requirements)}, but current required_record_ids "
            "does not include the declared requirement."
        )
    elif current["status"] != "compiled":
        outcome = "block"
        reason_code = "CURRENT_CONTEXT_FAIL_CLOSED"
        boundary_fact = (
            f"Current compilation failed {current['failure']['reason_code']}; "
            "no current packet is available."
        )
    elif mismatches:
        outcome = "recompile_required"
        if "contract_fingerprint" in mismatches:
            reason_code = "STALE_CONTRACT_FINGERPRINT"
            if normalized_change["kind"] == "late_user_correction":
                boundary_fact = (
                    "Late correction requires "
                    f"{', '.join(normalized_change['required_record_ids'])}; prior and "
                    "current contract fingerprints differ."
                )
            else:
                boundary_fact = (
                    "Prior and current contract fingerprints differ; the old receipt "
                    "cannot continue."
                )
        elif "packet_fingerprint" in mismatches:
            reason_code = "STALE_PACKET_FINGERPRINT"
            boundary_fact = (
                "Prior and current packet fingerprints differ; the old receipt cannot continue."
            )
        else:
            reason_code = "STALE_RECEIPT"
            boundary_fact = (
                "Prior and current receipt identifiers differ; recompilation is required."
            )
    elif normalized_change["kind"] == "late_user_correction":
        outcome = "recompile_required"
        reason_code = "LATE_USER_INPUT_REQUIRES_RECOMPILE"
        boundary_fact = (
            "A late user correction was declared after the supplied receipt; "
            "continuation requires a new receipt."
        )
    else:
        outcome = "continue"
        reason_code = "RECEIPT_CURRENT"
        boundary_fact = (
            "Receipt, contract, and packet fingerprints match and no intervening "
            "change was declared."
        )

    return {
        "product": "ContextGate",
        "mode": "context_staleness",
        "outcome": outcome,
        "can_continue": outcome == "continue",
        "reason_code": reason_code,
        "boundary_fact": boundary_fact,
        "change": normalized_change,
        "prior_receipt": prior,
        "current_proof": current_proof,
        "mismatches": mismatches,
        "proof_boundary": (
            "This decision compares the supplied receipt, declared change, current "
            "contract, and supplied records. It does not observe an agent runtime or "
            "prove that every real-world change was captured."
        ),
    }


def scenario_payload(records_path: Path, scenario_path: Path) -> dict:
    records = load_jsonl(records_path)
    validate_records(records)
    scenario = load_scenario(scenario_path)
    return {
        "product": "ContextGate",
        "scenario": scenario,
        "options": {
            "sources": sorted({record["source_class"] for record in records}),
            "authorities": sorted({record["authority"] for record in records}),
            "required_records": [
                {"id": record["id"], "title": record["title"]}
                for record in records
                if record["id"] in {"ADR-0234", "DEC-0421", "GUIDE-0112", "RUN-0880"}
            ],
            "lifecycle_states": sorted({record["lifecycle"] for record in records}),
            "sensitivity_states": sorted({record["sensitivity"] for record in records}),
            "controls": [
                {"id": control, "label": CONTROL_LABELS[control]}
                for control in (
                    "future_reveal",
                    "superseded_decision",
                    "generated_draft",
                    "rejected_plan",
                    "ambiguous_identity",
                    "remove_required_provenance",
                    "tight_token_budget",
                )
            ],
        },
    }


def compile_default(
    records_path: Path = DEFAULT_RECORDS,
    scenario_path: Path = DEFAULT_SCENARIO,
    *,
    contract_override: Mapping[str, object] | None = None,
    controls_override: Mapping[str, object] | None = None,
) -> dict:
    records = load_jsonl(records_path)
    scenario = load_scenario(scenario_path)
    contract = copy.deepcopy(scenario["contract"])
    if contract_override is not None:
        contract.update(copy.deepcopy(dict(contract_override)))
    controls = copy.deepcopy(scenario["controls"])
    if controls_override is not None:
        controls.update(copy.deepcopy(dict(controls_override)))
    return compile_context(records, contract, controls)


def run_self_test(records_path: Path, scenario_path: Path) -> None:
    compiled = compile_default(records_path, scenario_path)
    assert compiled["status"] == "compiled"
    print("PASS default_contract_compiles")
    assert compiled["naive"]["illegal_selected_count"] >= 1
    assert "ADR-0234" in compiled["naive"]["missing_required_ids"]
    print("PASS naive_baseline_is_plausible_but_illegal_and_incomplete")
    packet_ids = [record["id"] for record in compiled["packet"]]
    assert packet_ids[0] == "ADR-0234"
    assert "DEC-0421" in packet_ids
    print("PASS required_record_is_protected_before_ranking")
    trace_reasons = {row["reason_code"] for row in compiled["trace"]}
    assert {
        "EXCLUDE_FUTURE",
        "EXCLUDE_SUPERSEDED",
        "EXCLUDE_SOURCE",
        "EXCLUDE_LIFECYCLE",
        "EXCLUDE_IDENTITY",
    } <= trace_reasons
    print("PASS adversarial_reason_codes_are_visible")
    provenance_failure = compile_default(
        records_path,
        scenario_path,
        controls_override={"remove_required_provenance": True},
    )
    assert provenance_failure["status"] == "fail_closed"
    assert provenance_failure["failure"]["reason_code"] == "REQUIRED_MISSING_PROVENANCE"
    assert not provenance_failure["packet"]
    print("PASS missing_required_provenance_fails_closed")
    budget_failure = compile_default(
        records_path,
        scenario_path,
        controls_override={"tight_token_budget": True},
    )
    assert budget_failure["status"] == "fail_closed"
    assert budget_failure["failure"]["reason_code"] == "REQUIRED_OVER_BUDGET"
    print("PASS required_record_over_budget_fails_closed")
    repeated = compile_default(records_path, scenario_path)
    assert repeated == compiled
    print("PASS deterministic_repeat")
    records = load_jsonl(records_path)
    scenario = load_scenario(scenario_path)
    stale = evaluate_context_staleness(
        records,
        receipt_proof(compiled),
        scenario["fire_drill"]["change"],
        scenario["fire_drill"]["current_contract"],
        scenario["controls"],
    )
    assert stale["outcome"] == "recompile_required"
    assert stale["reason_code"] == "STALE_CONTRACT_FINGERPRINT"
    assert not stale["can_continue"]
    print("PASS late_correction_marks_old_receipt_stale")
    recompiled = compile_context(
        records,
        scenario["fire_drill"]["current_contract"],
        scenario["controls"],
    )
    assert recompiled["receipt_id"] != compiled["receipt_id"]
    current = evaluate_context_staleness(
        records,
        receipt_proof(recompiled),
        {"kind": "none", "summary": "", "required_record_ids": []},
        scenario["fire_drill"]["current_contract"],
        scenario["controls"],
    )
    assert current["outcome"] == "continue"
    print("PASS recompiled_receipt_is_current")


def run_full_check(records_path: Path, scenario_path: Path) -> int:
    run_self_test(records_path, scenario_path)
    commands = [
        (
            "legacy self-test",
            [sys.executable, "-B", "metadata_retrieval_demo.py", "--self-test"],
        ),
        (
            "legacy failure registry",
            [sys.executable, "-B", "metadata_retrieval_demo.py", "failures"],
        ),
    ]
    for label, command in commands:
        print(f"\n== {label} ==")
        completed = subprocess.run(command, cwd=ROOT, check=False)
        if completed.returncode:
            return completed.returncode
    print("\n== unit tests ==")
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
    test_result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not test_result.wasSuccessful():
        return 1
    with tempfile.TemporaryDirectory(prefix="contextgate-pycache-") as cache_dir:
        env = dict(os.environ)
        env["PYTHONPYCACHEPREFIX"] = cache_dir
        compile_command = [
            sys.executable,
            "-B",
            "-m",
            "py_compile",
            "contextgate.py",
            "metadata_retrieval_demo.py",
            "tests/test_contextgate.py",
            "tests/test_metadata_retrieval_demo.py",
        ]
        print("\n== Python compilation ==")
        completed = subprocess.run(compile_command, cwd=ROOT, env=env, check=False)
        if completed.returncode:
            return completed.returncode
    print("\nPASS ContextGate full check")
    return 0


class ContextGateHandler(SimpleHTTPRequestHandler):
    records_path = DEFAULT_RECORDS
    scenario_path = DEFAULT_SCENARIO

    def __init__(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        super().__init__(*args, directory=str(WEB_ROOT), **kwargs)

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'self'",
        )
        super().end_headers()

    def _send_json(self, status: int, payload: object) -> None:
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/health":
            self._send_json(200, {"status": "ok", "product": "ContextGate"})
            return
        if path == "/api/scenario":
            try:
                payload = scenario_payload(self.records_path, self.scenario_path)
            except (OSError, ValueError, RuntimeError) as exc:
                self._send_json(500, {"error": str(exc)})
                return
            self._send_json(200, payload)
            return
        if path == "/api/compile":
            try:
                result = compile_default(self.records_path, self.scenario_path)
            except (OSError, ValueError, RuntimeError) as exc:
                self._send_json(500, {"error": str(exc)})
                return
            self._send_json(200, result)
            return
        super().do_GET()

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path not in {"/api/compile", "/api/fire-drill"}:
            self._send_json(404, {"error": "not found"})
            return
        try:
            raw_length = self.headers.get("Content-Length", "")
            length = int(raw_length)
            if length <= 0 or length > 262_144:
                raise ValueError("request body must be between 1 and 262144 bytes")
            raw = self.rfile.read(length)
            payload = json.loads(raw.decode("utf-8"))
            records = load_jsonl(self.records_path)
            if path == "/api/compile":
                expected = {"contract", "controls"}
                if not isinstance(payload, dict) or set(payload) != expected:
                    raise ValueError("request must contain exactly contract and controls")
                result = compile_context(records, payload["contract"], payload["controls"])
            else:
                expected = {
                    "prior_receipt",
                    "change",
                    "current_contract",
                    "controls",
                }
                if not isinstance(payload, dict) or set(payload) != expected:
                    raise ValueError(
                        "request must contain exactly prior_receipt, change, "
                        "current_contract, and controls"
                    )
                result = evaluate_context_staleness(
                    records,
                    payload["prior_receipt"],
                    payload["change"],
                    payload["current_contract"],
                    payload["controls"],
                )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            self._send_json(400, {"error": str(exc)})
            return
        except (OSError, RuntimeError) as exc:
            self._send_json(500, {"error": str(exc)})
            return
        self._send_json(200, result)

    def log_message(self, format: str, *args: object) -> None:
        sys.stderr.write("ContextGate: " + format % args + "\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", type=Path, default=DEFAULT_RECORDS)
    parser.add_argument("--scenario-file", type=Path, default=DEFAULT_SCENARIO)
    parser.add_argument("--self-test", action="store_true")
    subparsers = parser.add_subparsers(dest="command")

    compile_parser = subparsers.add_parser("compile", help="Compile the checked-in scenario")
    compile_parser.add_argument("--json", action="store_true")
    compile_parser.add_argument("--remove-required-provenance", action="store_true")
    compile_parser.add_argument("--tight-token-budget", action="store_true")
    compile_parser.add_argument("--no-poisons", action="store_true")

    serve_parser = subparsers.add_parser("serve", help="Run the local Context Debugger")
    serve_parser.add_argument("--host", default="localhost")
    serve_parser.add_argument("--port", type=int, default=8765)
    serve_parser.add_argument("--open", action="store_true")

    subparsers.add_parser("check", help="Run the complete deterministic check stack")
    return parser


def _print_compile_summary(result: Mapping[str, object]) -> None:
    print(f'ContextGate: {str(result["status"]).upper()}')
    print(f'Receipt: {result["receipt_id"]}')
    naive = result["naive"]
    assert isinstance(naive, dict)
    print(
        "Naive: "
        f'{naive["selected_count"]} selected, '
        f'{naive["illegal_selected_count"]} illegal, '
        f'missing required={naive["missing_required_ids"]}'
    )
    if result["failure"]:
        failure = result["failure"]
        assert isinstance(failure, dict)
        print(f'Failure: {failure["reason_code"]} — {failure["message"]}')
        return
    packet = result["packet"]
    assert isinstance(packet, list)
    for record in packet:
        print(
            f'  {record["id"]}: {record["title"]} '
            f'({record["token_count"]} tokens)'
        )


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.self_test:
        run_self_test(args.records, args.scenario_file)
        return 0
    if args.command == "check":
        return run_full_check(args.records, args.scenario_file)
    if args.command == "compile" or args.command is None:
        controls: dict[str, bool] = {}
        if args.command == "compile":
            controls["remove_required_provenance"] = args.remove_required_provenance
            controls["tight_token_budget"] = args.tight_token_budget
            if args.no_poisons:
                controls.update({control: False for control in CONTROL_RECORDS})
        result = compile_default(
            args.records,
            args.scenario_file,
            controls_override=controls,
        )
        if args.command == "compile" and args.json:
            print(json.dumps(result, indent=2, sort_keys=True))
        else:
            _print_compile_summary(result)
        return 0 if result["status"] == "compiled" else 2
    if args.command == "serve":
        if not 0 < args.port < 65_536:
            raise SystemExit("--port must be between 1 and 65535")
        ContextGateHandler.records_path = args.records
        ContextGateHandler.scenario_path = args.scenario_file
        server = ThreadingHTTPServer((args.host, args.port), ContextGateHandler)
        url = f"http://{args.host}:{server.server_port}/"
        print(f"ContextGate debugger: {url}")
        print("Press Ctrl-C to stop.")
        if args.open:
            webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nStopping ContextGate.")
        finally:
            server.server_close()
        return 0
    raise SystemExit(f"unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
