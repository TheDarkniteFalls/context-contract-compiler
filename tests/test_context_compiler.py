from __future__ import annotations

import copy
import contextlib
import hashlib
import io
import json
import re
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import context_compiler


class ContextCompilerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.records, cls.scenario = context_compiler.load_input_bundle(
            context_compiler.DEFAULT_RECORDS,
            context_compiler.DEFAULT_SCENARIO,
        )

    def compile(self, *, contract=None, controls=None):  # type: ignore[no-untyped-def]
        return context_compiler.compile_context(
            self.records,
            contract or self.scenario["contract"],
            controls or self.scenario["controls"],
        )

    def no_change(self):  # type: ignore[no-untyped-def]
        return {"kind": "none", "summary": "", "required_record_ids": []}

    def evaluate(self, prior, change, current_contract, *, records=None):  # type: ignore[no-untyped-def]
        return context_compiler.evaluate_context_staleness(
            records or self.records,
            context_compiler.receipt_proof(prior),
            change,
            current_contract,
            self.scenario["controls"],
        )

    def test_fixture_is_small_synthetic_and_schema_valid(self) -> None:
        self.assertEqual(len(self.records), 13)
        self.assertEqual(len(self.records), len({row["id"] for row in self.records}))
        fixture_text = context_compiler.DEFAULT_RECORDS.read_text(encoding="utf-8")
        for forbidden in (str(Path.home()), "private_source_path", "production.sqlite"):
            self.assertNotIn(forbidden, fixture_text)

    def test_default_compile_emits_expected_packet_and_accounting(self) -> None:
        result = self.compile()
        self.assertEqual(result["status"], "compiled")
        self.assertEqual(
            [row["id"] for row in result["packet"]],
            ["ADR-0234", "DEC-0421", "GUIDE-0112", "RUN-0880"],
        )
        self.assertEqual(result["summary"]["token_count"], 74)
        self.assertEqual(result["summary"]["remaining_tokens"], 22)
        self.assertEqual(result["summary"]["required_tokens"], 16)

    def test_naive_baseline_selects_future_context_and_misses_obligation(self) -> None:
        result = self.compile()
        selected = {row["id"]: row for row in result["naive"]["selected_records"]}
        self.assertIn("FR-1050", selected)
        self.assertEqual(selected["FR-1050"]["warning_code"], "EXCLUDE_FUTURE")
        self.assertIn("ADR-0234", result["naive"]["missing_required_ids"])
        self.assertGreaterEqual(result["naive"]["illegal_selected_count"], 4)

    def test_required_record_is_selected_before_its_low_relevance_rank(self) -> None:
        result = self.compile()
        required = result["packet"][0]
        self.assertTrue(required["required"])
        self.assertEqual(required["id"], "ADR-0234")
        self.assertGreater(required["relevance_rank"], 10)
        self.assertEqual(result["trace"][0]["reason_code"], "SELECTED_REQUIRED")

    def test_future_records_cannot_change_legal_optional_ranking(self) -> None:
        contract = copy.deepcopy(self.scenario["contract"])
        contract["token_budget"] = 48
        baseline = self.compile(contract=contract)

        records = copy.deepcopy(self.records)
        template = next(row for row in records if row["id"] == "DEC-0421")
        for index in range(10):
            future = copy.deepcopy(template)
            future_id = f"FUTURE-{index:02d}"
            future.update(
                {
                    "id": future_id,
                    "title": "Backoff",
                    "body": "Backoff",
                    "tags": "backoff",
                    "stable_identity": future_id.lower(),
                    "valid_from": "2025-06-01",
                    "provenance": [f"plan://{future_id.lower()}"],
                }
            )
            records.append(future)

        injected = context_compiler.compile_context(
            records,
            contract,
            self.scenario["controls"],
        )
        self.assertEqual(
            [row["id"] for row in baseline["packet"]],
            ["ADR-0234", "DEC-0421"],
        )
        self.assertEqual(
            [row["id"] for row in injected["packet"]],
            ["ADR-0234", "DEC-0421"],
        )
        injected_trace = {row["record_id"]: row for row in injected["trace"]}
        raw_ranked_ids = context_compiler.rank_records(records, contract["task"])
        self.assertEqual(
            injected_trace["DEC-0421"]["relevance_rank"],
            raw_ranked_ids.index("DEC-0421") + 1,
        )
        self.assertEqual(
            injected_trace["DEC-0421"]["boundary_fact"],
            "rank 1 after legality",
        )
        for index in range(10):
            self.assertEqual(
                injected_trace[f"FUTURE-{index:02d}"]["reason_code"],
                "EXCLUDE_FUTURE",
            )

    def test_every_active_candidate_has_exactly_one_trace_decision(self) -> None:
        result = self.compile()
        trace_ids = [row["record_id"] for row in result["trace"]]
        self.assertEqual(len(trace_ids), len(self.records))
        self.assertEqual(len(trace_ids), len(set(trace_ids)))
        self.assertEqual(
            result["summary"]["selected_count"] + result["summary"]["excluded_count"],
            len(self.records),
        )

    def test_poison_records_have_specific_reason_codes(self) -> None:
        trace = {row["record_id"]: row for row in self.compile()["trace"]}
        expected = {
            "FR-1050": "EXCLUDE_FUTURE",
            "DEC-0410": "EXCLUDE_SUPERSEDED",
            "DRAFT-0702": "EXCLUDE_SOURCE",
            "PLAN-0901": "EXCLUDE_LIFECYCLE",
            "AMB-0606": "EXCLUDE_IDENTITY",
            "SENS-9900": "EXCLUDE_SENSITIVITY",
            "OTHER-3000": "EXCLUDE_PROJECT",
        }
        for record_id, reason_code in expected.items():
            self.assertEqual(trace[record_id]["reason_code"], reason_code, record_id)
            self.assertTrue(trace[record_id]["boundary_fact"], record_id)

    def test_missing_required_provenance_fails_closed_without_packet(self) -> None:
        controls = dict(self.scenario["controls"])
        controls["remove_required_provenance"] = True
        result = self.compile(controls=controls)
        self.assertEqual(result["status"], "fail_closed")
        self.assertEqual(result["failure"]["reason_code"], "REQUIRED_MISSING_PROVENANCE")
        self.assertEqual(result["packet"], [])
        self.assertEqual(result["packet_text"], "")
        trace = {row["record_id"]: row for row in result["trace"]}
        self.assertEqual(trace["ADR-0234"]["reason_code"], "REQUIRED_MISSING_PROVENANCE")
        self.assertEqual(trace["DEC-0421"]["reason_code"], "EXCLUDE_FAIL_CLOSED")

    def test_required_set_that_cannot_fit_fails_closed(self) -> None:
        controls = dict(self.scenario["controls"])
        controls["tight_token_budget"] = True
        result = self.compile(controls=controls)
        self.assertEqual(result["contract"]["token_budget"], 12)
        self.assertEqual(result["status"], "fail_closed")
        self.assertEqual(result["failure"]["reason_code"], "REQUIRED_OVER_BUDGET")

    def test_absent_required_record_fails_closed(self) -> None:
        contract = copy.deepcopy(self.scenario["contract"])
        contract["required_record_ids"] = ["MISSING-404"]
        result = self.compile(contract=contract)
        self.assertEqual(result["status"], "fail_closed")
        self.assertEqual(result["failure"]["reason_code"], "REQUIRED_NOT_FOUND")
        self.assertEqual(result["trace"][0]["record_id"], "MISSING-404")
        self.assertEqual(result["summary"]["candidate_count"], len(self.records))
        self.assertEqual(result["summary"]["trace_count"], len(self.records) + 1)
        self.assertEqual(result["summary"]["excluded_count"], len(self.records))
        self.assertEqual(result["summary"]["missing_required_count"], 1)

    def test_illegal_required_source_fails_closed(self) -> None:
        contract = copy.deepcopy(self.scenario["contract"])
        contract["required_record_ids"] = ["DEC-0421"]
        contract["allowed_sources"] = [
            source for source in contract["allowed_sources"] if source != "decision_log"
        ]
        result = self.compile(contract=contract)
        self.assertEqual(result["status"], "fail_closed")
        self.assertEqual(result["failure"]["reason_code"], "REQUIRED_SOURCE_NOT_ALLOWED")

    def test_optional_records_are_pruned_only_after_required_fit(self) -> None:
        contract = copy.deepcopy(self.scenario["contract"])
        contract["token_budget"] = 54
        result = self.compile(contract=contract)
        self.assertEqual(result["status"], "compiled")
        self.assertEqual(
            [row["id"] for row in result["packet"]],
            ["ADR-0234", "DEC-0421", "GUIDE-0112"],
        )
        trace = {row["record_id"]: row for row in result["trace"]}
        self.assertEqual(trace["RUN-0880"]["reason_code"], "EXCLUDE_BUDGET")

    def test_turning_off_record_poisons_removes_only_those_candidates(self) -> None:
        controls = dict(self.scenario["controls"])
        controls.update({control: False for control in context_compiler.CONTROL_RECORDS})
        result = self.compile(controls=controls)
        self.assertEqual(result["summary"]["candidate_count"], 8)
        trace_ids = {row["record_id"] for row in result["trace"]}
        self.assertFalse(set(context_compiler.CONTROL_RECORDS.values()) & trace_ids)

    def test_compile_and_receipt_are_deterministic(self) -> None:
        first = self.compile()
        second = self.compile()
        self.assertEqual(first, second)
        changed_contract = copy.deepcopy(self.scenario["contract"])
        changed_contract["token_budget"] = 80
        changed = self.compile(contract=changed_contract)
        self.assertNotEqual(first["receipt_id"], changed["receipt_id"])

    def test_unchanged_receipt_may_continue(self) -> None:
        compiled = self.compile()
        decision = self.evaluate(compiled, self.no_change(), compiled["contract"])
        self.assertEqual(decision["outcome"], "continue")
        self.assertTrue(decision["can_continue"])
        self.assertEqual(decision["reason_code"], "RECEIPT_CURRENT")
        self.assertEqual(decision["mismatches"], [])

    def test_late_user_input_cannot_be_ignored(self) -> None:
        compiled = self.compile()
        decision = self.evaluate(
            compiled,
            self.scenario["fire_drill"]["change"],
            compiled["contract"],
        )
        self.assertEqual(decision["outcome"], "block")
        self.assertFalse(decision["can_continue"])
        self.assertEqual(decision["reason_code"], "LATE_USER_INPUT_NOT_APPLIED")
        self.assertIn("RUN-0880", decision["boundary_fact"])

    def test_stale_receipt_cannot_continue(self) -> None:
        compiled = self.compile()
        decision = self.evaluate(
            compiled,
            self.scenario["fire_drill"]["change"],
            self.scenario["fire_drill"]["current_contract"],
        )
        self.assertEqual(decision["outcome"], "recompile_required")
        self.assertFalse(decision["can_continue"])
        self.assertEqual(decision["reason_code"], "STALE_CONTRACT_FINGERPRINT")

    def test_contract_and_packet_fingerprint_mismatches_are_visible(self) -> None:
        compiled = self.compile()
        contract_changed = self.evaluate(
            compiled,
            self.scenario["fire_drill"]["change"],
            self.scenario["fire_drill"]["current_contract"],
        )
        self.assertIn("contract_fingerprint", contract_changed["mismatches"])

        changed_records = copy.deepcopy(self.records)
        runbook = next(row for row in changed_records if row["id"] == "GUIDE-0112")
        runbook["body"] += " Synthetic late clarification."
        packet_changed = self.evaluate(
            compiled,
            self.no_change(),
            compiled["contract"],
            records=changed_records,
        )
        self.assertEqual(packet_changed["reason_code"], "STALE_PACKET_FINGERPRINT")
        self.assertEqual(packet_changed["mismatches"], ["packet_fingerprint"])

    def test_staleness_decision_is_deterministic(self) -> None:
        compiled = self.compile()
        arguments = (
            compiled,
            self.scenario["fire_drill"]["change"],
            self.scenario["fire_drill"]["current_contract"],
        )
        self.assertEqual(self.evaluate(*arguments), self.evaluate(*arguments))

    def test_recompilation_produces_new_current_receipt(self) -> None:
        old = self.compile()
        recompiled = self.compile(
            contract=self.scenario["fire_drill"]["current_contract"]
        )
        self.assertNotEqual(old["receipt_id"], recompiled["receipt_id"])
        self.assertNotEqual(old["contract_fingerprint"], recompiled["contract_fingerprint"])
        self.assertNotEqual(old["packet_fingerprint"], recompiled["packet_fingerprint"])
        current = self.evaluate(
            recompiled,
            self.no_change(),
            self.scenario["fire_drill"]["current_contract"],
        )
        self.assertEqual(current["outcome"], "continue")
        self.assertTrue(current["can_continue"])

    def test_contract_rejects_unknown_fields_and_duplicate_requirements(self) -> None:
        contract = copy.deepcopy(self.scenario["contract"])
        contract["hidden_authority"] = True
        with self.assertRaisesRegex(ValueError, "schema mismatch"):
            self.compile(contract=contract)
        duplicate = copy.deepcopy(self.scenario["contract"])
        duplicate["required_record_ids"] = ["ADR-0234", "ADR-0234"]
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.compile(contract=duplicate)

    def test_static_debugger_contains_the_required_judge_controls(self) -> None:
        html = (context_compiler.WEB_ROOT / "index.html").read_text(encoding="utf-8")
        javascript = (context_compiler.WEB_ROOT / "app.js").read_text(encoding="utf-8")
        debugger_source = html + javascript
        for label in (
            "Task contract",
            "Naïve relevance",
            "Context Contract Compiler packet",
            "Decision trace",
            "Copy packet",
            "Adversarial controls",
            "Agent Fire Drill",
            "STALE — RECOMPILE REQUIRED",
        ):
            self.assertIn(label, debugger_source)
        self.assertIn('name = "adversarial_control"', javascript)
        html_ids = re.findall(r'\bid="([^"]+)"', html)
        self.assertEqual(len(html_ids), len(set(html_ids)))
        referenced_ids = set(re.findall(r'byId\("([^"]+)"\)', javascript))
        payload = context_compiler.scenario_payload(
            context_compiler.DEFAULT_RECORDS, context_compiler.DEFAULT_SCENARIO
        )
        dynamic_ids = {
            f"control-{item['id']}" for item in payload["options"]["controls"]
        }
        self.assertEqual(referenced_ids - set(html_ids) - dynamic_ids, set())
        self.assertEqual(
            {item["id"] for item in payload["options"]["controls"]},
            context_compiler.CONTROL_FIELDS,
        )


class ContextCompilerInputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.starter_records_path = context_compiler.ROOT / "examples" / "starter_records.jsonl"
        cls.starter_scenario_path = context_compiler.ROOT / "examples" / "starter_scenario.json"
        cls.starter_records, cls.starter_scenario = context_compiler.load_input_bundle(
            cls.starter_records_path,
            cls.starter_scenario_path,
        )

    def _write_bundle(
        self,
        root: Path,
        *,
        records: list[dict] | None = None,
        scenario: dict | None = None,
        records_text: str | None = None,
        scenario_text: str | None = None,
    ) -> tuple[Path, Path]:
        records_path = root / "records.jsonl"
        scenario_path = root / "scenario.json"
        if records_text is None:
            records_text = "\n".join(
                json.dumps(record, sort_keys=True)
                for record in (records if records is not None else self.starter_records)
            ) + "\n"
        if scenario_text is None:
            scenario_text = json.dumps(
                scenario if scenario is not None else self.starter_scenario,
                indent=2,
                sort_keys=True,
            ) + "\n"
        records_path.write_text(records_text, encoding="utf-8")
        scenario_path.write_text(scenario_text, encoding="utf-8")
        return records_path, scenario_path

    def _run_main(self, arguments: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = context_compiler.main(arguments)
        return status, stdout.getvalue(), stderr.getvalue()

    def test_validate_inputs_accepts_default_and_starter(self) -> None:
        default = self._run_main(["validate-inputs"])
        starter = self._run_main(
            [
                "--records",
                str(self.starter_records_path),
                "--scenario-file",
                str(self.starter_scenario_path),
                "validate-inputs",
            ]
        )
        self.assertEqual(default, (0, "VALID INPUTS: records=13 scenarios=1 base_required=1 late_required=1\n", ""))
        self.assertEqual(starter, (0, "VALID INPUTS: records=4 scenarios=1 base_required=1 late_required=1\n", ""))

    def test_invalid_cli_is_concise_and_starts_no_server(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            records_path, scenario_path = self._write_bundle(
                root,
                records_text="{not json}\n",
            )
            status, stdout, stderr = self._run_main(
                [
                    "--records",
                    str(records_path),
                    "--scenario-file",
                    str(scenario_path),
                    "validate-inputs",
                ]
            )
            self.assertEqual(status, 2)
            self.assertEqual(stdout, "")
            self.assertRegex(stderr, r"^INVALID INPUTS: .+\n$")
            self.assertNotIn("Traceback", stderr)

            status, stdout, stderr = self._run_main(
                [
                    "--records",
                    str(records_path),
                    "--scenario-file",
                    str(scenario_path),
                    "compile",
                    "--json",
                ]
            )
            self.assertEqual(status, 2)
            self.assertEqual(stdout, "")
            self.assertRegex(stderr, r"^INVALID INPUTS: .+\n$")
            self.assertNotIn("Traceback", stderr)

            with socket.socket() as probe:
                probe.bind(("localhost", 0))
                port = probe.getsockname()[1]
            status, stdout, stderr = self._run_main(
                [
                    "--records",
                    str(records_path),
                    "--scenario-file",
                    str(scenario_path),
                    "serve",
                    "--host",
                    "localhost",
                    "--port",
                    str(port),
                ]
            )
            self.assertEqual(status, 2)
            self.assertEqual(stdout, "")
            self.assertNotIn("Traceback", stderr)
            with socket.socket() as client:
                client.settimeout(0.1)
                self.assertNotEqual(client.connect_ex(("localhost", port)), 0)

    def test_strict_json_rejects_malformed_non_object_duplicate_and_non_finite(self) -> None:
        first = self.starter_records[0]
        valid_first = json.dumps(first, sort_keys=True)
        duplicate_first = valid_first.replace(
            '"id": "POLICY-100"',
            '"id": "POLICY-100", "id": "POLICY-101"',
            1,
        )
        non_finite_first = valid_first.replace('"token_count": 12', '"token_count": NaN')
        cases = {
            "malformed": "{not json}\n",
            "non_object": "[]\n",
            "duplicate": duplicate_first + "\n",
            "non_finite": non_finite_first + "\n",
        }
        for label, records_text in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                records_path, scenario_path = self._write_bundle(
                    Path(directory),
                    records_text=records_text,
                )
                with self.assertRaises(ValueError):
                    context_compiler.load_input_bundle(records_path, scenario_path)

        scenario_text = self.starter_scenario_path.read_text(encoding="utf-8")
        duplicate_scenario = scenario_text.replace(
            '"id": "starter-message-router"',
            '"id": "starter-message-router", "id": "duplicate"',
            1,
        )
        non_finite_scenario = scenario_text.replace('"token_budget": 48', '"token_budget": NaN', 1)
        for label, text in {
            "duplicate_scenario": duplicate_scenario,
            "non_finite_scenario": non_finite_scenario,
        }.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                records_path, scenario_path = self._write_bundle(
                    Path(directory),
                    scenario_text=text,
                )
                with self.assertRaises(ValueError):
                    context_compiler.load_input_bundle(records_path, scenario_path)

    def test_record_schema_types_and_references_fail_closed(self) -> None:
        cases: dict[str, list[dict]] = {}
        missing = copy.deepcopy(self.starter_records)
        missing[0].pop("title")
        cases["missing_field"] = missing
        extra = copy.deepcopy(self.starter_records)
        extra[0]["private_note"] = "not allowed"
        cases["extra_field"] = extra
        wrong_type = copy.deepcopy(self.starter_records)
        wrong_type[0]["token_count"] = "12"
        cases["wrong_type"] = wrong_type
        whitespace = copy.deepcopy(self.starter_records)
        whitespace[0]["title"] = "   "
        cases["whitespace"] = whitespace
        duplicate = copy.deepcopy(self.starter_records)
        duplicate[1]["id"] = duplicate[0]["id"]
        cases["duplicate_id"] = duplicate
        broken = copy.deepcopy(self.starter_records)
        broken[3]["superseded_by"] = "MISSING-999"
        cases["broken_supersession"] = broken
        self_reference = copy.deepcopy(self.starter_records)
        self_reference[3]["superseded_by"] = self_reference[3]["id"]
        cases["self_supersession"] = self_reference

        for label, records in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                records_path, scenario_path = self._write_bundle(
                    Path(directory),
                    records=records,
                )
                with self.assertRaises(ValueError):
                    context_compiler.load_input_bundle(records_path, scenario_path)

    def test_contract_fire_drill_and_control_references_fail_closed(self) -> None:
        cases: dict[str, dict] = {}
        missing_base = copy.deepcopy(self.starter_scenario)
        missing_base["contract"]["required_record_ids"] = ["MISSING-999"]
        cases["missing_base_required"] = missing_base

        dropped = copy.deepcopy(self.starter_scenario)
        dropped["fire_drill"]["current_contract"]["required_record_ids"] = ["RUN-220"]
        cases["dropped_existing_required"] = dropped

        mismatch = copy.deepcopy(self.starter_scenario)
        mismatch["fire_drill"]["current_contract"]["required_record_ids"].append("GUIDE-310")
        cases["undeclared_new_required"] = mismatch

        missing_fire = copy.deepcopy(self.starter_scenario)
        missing_fire["fire_drill"]["change"]["required_record_ids"] = ["MISSING-999"]
        missing_fire["fire_drill"]["current_contract"]["required_record_ids"] = [
            "POLICY-100",
            "MISSING-999",
        ]
        cases["missing_fire_required"] = missing_fire

        missing_control = copy.deepcopy(self.starter_scenario)
        missing_control["controls"]["future_reveal"] = True
        cases["missing_control_fixture"] = missing_control

        wrong_contract_type = copy.deepcopy(self.starter_scenario)
        wrong_contract_type["contract"] = []
        cases["wrong_contract_type"] = wrong_contract_type

        extra_scenario = copy.deepcopy(self.starter_scenario)
        extra_scenario["private_note"] = "not allowed"
        cases["extra_scenario_field"] = extra_scenario

        for label, scenario in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                records_path, scenario_path = self._write_bundle(
                    Path(directory),
                    scenario=scenario,
                )
                with self.assertRaises(ValueError):
                    context_compiler.load_input_bundle(records_path, scenario_path)

    def test_valid_bundle_can_still_fail_policy_compilation(self) -> None:
        scenario = copy.deepcopy(self.starter_scenario)
        scenario["contract"]["allowed_sources"].remove("policy_note")
        with tempfile.TemporaryDirectory() as directory:
            records_path, scenario_path = self._write_bundle(
                Path(directory),
                scenario=scenario,
            )
            records, loaded = context_compiler.load_input_bundle(records_path, scenario_path)
        result = context_compiler.compile_context(
            records,
            loaded["contract"],
            loaded["controls"],
        )
        self.assertEqual(result["status"], "fail_closed")
        self.assertEqual(result["failure"]["reason_code"], "REQUIRED_SOURCE_NOT_ALLOWED")
        self.assertEqual(result["packet"], [])

    def test_starter_compile_and_fire_drill_are_deterministic(self) -> None:
        compiled = context_compiler.compile_default(
            self.starter_records_path,
            self.starter_scenario_path,
        )
        self.assertEqual(
            compiled,
            context_compiler.compile_default(
                self.starter_records_path,
                self.starter_scenario_path,
            ),
        )
        self.assertEqual(compiled["receipt_id"], "cg-506603b39e23d288")
        self.assertEqual(
            [record["id"] for record in compiled["packet"]],
            ["POLICY-100", "GUIDE-310", "RUN-220"],
        )
        self.assertEqual(compiled["summary"]["token_count"], 37)
        self.assertEqual(compiled["summary"]["remaining_tokens"], 11)

        fire_drill = self.starter_scenario["fire_drill"]
        stale = context_compiler.evaluate_context_staleness(
            self.starter_records,
            context_compiler.receipt_proof(compiled),
            fire_drill["change"],
            fire_drill["current_contract"],
            self.starter_scenario["controls"],
        )
        self.assertEqual(stale["outcome"], "recompile_required")
        self.assertEqual(stale["reason_code"], "STALE_CONTRACT_FINGERPRINT")
        recompiled = context_compiler.compile_context(
            self.starter_records,
            fire_drill["current_contract"],
            self.starter_scenario["controls"],
        )
        self.assertEqual(
            [record["id"] for record in recompiled["packet"]],
            ["POLICY-100", "RUN-220", "GUIDE-310"],
        )
        self.assertEqual(recompiled["receipt_id"], "cg-12f857c62bba5fdc")

        controls = dict(self.starter_scenario["controls"])
        controls["remove_required_provenance"] = True
        unsupported = context_compiler.compile_context(
            self.starter_records,
            self.starter_scenario["contract"],
            controls,
        )
        self.assertEqual(unsupported["status"], "fail_closed")
        self.assertEqual(
            unsupported["failure"]["reason_code"],
            "REQUIRED_MISSING_PROVENANCE",
        )
        self.assertEqual(unsupported["failure"]["record_id"], "POLICY-100")

    def test_debugger_options_follow_loaded_legal_records(self) -> None:
        default = context_compiler.scenario_payload(
            context_compiler.DEFAULT_RECORDS,
            context_compiler.DEFAULT_SCENARIO,
        )
        starter = context_compiler.scenario_payload(
            self.starter_records_path,
            self.starter_scenario_path,
        )
        self.assertEqual(
            [item["id"] for item in default["options"]["required_records"]],
            ["ADR-0234", "DEC-0421", "RUN-0880", "GUIDE-0112"],
        )
        self.assertEqual(len(default["options"]["controls"]), 7)
        self.assertEqual(
            [item["id"] for item in starter["options"]["required_records"]],
            ["POLICY-100", "RUN-220", "GUIDE-310"],
        )
        self.assertEqual(
            [item["id"] for item in starter["options"]["controls"]],
            ["remove_required_provenance", "tight_token_budget"],
        )
        self.assertIn("draft", starter["options"]["lifecycle_states"])
        self.assertIn("restricted", starter["options"]["sensitivity_states"])
        for source in self.starter_scenario["contract"]["allowed_sources"]:
            self.assertIn(source, starter["options"]["sources"])
        for authority in self.starter_scenario["contract"]["allowed_authorities"]:
            self.assertIn(authority, starter["options"]["authorities"])

    def test_starter_fixture_is_public_safe_and_synthetic(self) -> None:
        text = self.starter_records_path.read_text(encoding="utf-8") + self.starter_scenario_path.read_text(encoding="utf-8")
        for forbidden in (
            "http://",
            "https://",
            "github.com",
            "@",
            "/Users/",
            "TheDarkniteFalls",
            "credential",
            "private note",
        ):
            self.assertNotIn(forbidden, text)
        for record in self.starter_records:
            self.assertTrue(all(item.startswith("synthetic:") for item in record["provenance"]))

    def test_default_cli_bytes_remain_exact(self) -> None:
        commands = {
            "json": (
                [sys.executable, "-B", "context_compiler.py", "compile", "--json"],
                "1f299f24fcc88fe83dbad69f6095bb76d2bf4a4b650182b6b98b3000109c21dd",
            ),
            "human": (
                [sys.executable, "-B", "context_compiler.py", "compile"],
                "18214747c29fe73f51b66c7653b7b10c1e4dd5130d5d7ac1102b088c565cb30a",
            ),
        }
        for label, (command, expected_hash) in commands.items():
            with self.subTest(label=label):
                completed = subprocess.run(
                    command,
                    cwd=context_compiler.ROOT,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(completed.returncode, 0)
                self.assertEqual(completed.stderr, b"")
                self.assertEqual(hashlib.sha256(completed.stdout).hexdigest(), expected_hash)


class QuietContextCompilerHandler(context_compiler.ContextCompilerHandler):
    def log_message(self, format: str, *args: object) -> None:
        return


class ContextCompilerHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        QuietContextCompilerHandler.records_path = context_compiler.DEFAULT_RECORDS
        QuietContextCompilerHandler.scenario_path = context_compiler.DEFAULT_SCENARIO
        try:
            cls.server = ThreadingHTTPServer(("localhost", 0), QuietContextCompilerHandler)
        except PermissionError as exc:
            raise unittest.SkipTest(
                "managed sandbox blocked localhost binding; rerun outside the managed sandbox"
            ) from exc
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://localhost:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def get_json(self, path: str):  # type: ignore[no-untyped-def]
        with urllib.request.urlopen(self.base_url + path, timeout=2) as response:
            return response, json.loads(response.read())

    def test_health_and_scenario_endpoints(self) -> None:
        response, health = self.get_json("/api/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(
            health, {"product": "Context Contract Compiler", "status": "ok"}
        )
        _, scenario = self.get_json("/api/scenario")
        self.assertEqual(scenario["scenario"]["id"], "retry-safety")
        self.assertEqual(len(scenario["options"]["controls"]), 7)

    def test_compile_endpoint_and_security_headers(self) -> None:
        scenario = context_compiler.load_scenario(context_compiler.DEFAULT_SCENARIO)
        payload = json.dumps(
            {"contract": scenario["contract"], "controls": scenario["controls"]}
        ).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + "/api/compile",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            result = json.loads(response.read())
            self.assertEqual(response.status, 200)
            self.assertEqual(result["status"], "compiled")
            self.assertIn("default-src 'self'", response.headers["Content-Security-Policy"])
            self.assertEqual(response.headers["Cache-Control"], "no-store")

        fire_drill = scenario["fire_drill"]
        payload = json.dumps(
            {
                "prior_receipt": context_compiler.receipt_proof(result),
                "change": fire_drill["change"],
                "current_contract": fire_drill["current_contract"],
                "controls": scenario["controls"],
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + "/api/fire-drill",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            decision = json.loads(response.read())
            self.assertEqual(response.status, 200)
            self.assertEqual(decision["outcome"], "recompile_required")
            self.assertFalse(decision["can_continue"])

    def test_compile_endpoint_rejects_undeclared_input(self) -> None:
        payload = json.dumps({"contract": {}, "controls": {}, "extra": True}).encode(
            "utf-8"
        )
        request = urllib.request.Request(
            self.base_url + "/api/compile",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=2)
        self.assertEqual(caught.exception.code, 400)

    def test_http_rejects_duplicate_keys_and_non_finite_numbers(self) -> None:
        scenario = context_compiler.load_scenario(context_compiler.DEFAULT_SCENARIO)
        contract = json.dumps(scenario["contract"], separators=(",", ":"))
        payloads = (
            (
                '{"contract":'
                + contract
                + ',"controls":{},"controls":{"tight_token_budget":false}}'
            ).encode("utf-8"),
            (
                '{"contract":'
                + contract
                + ',"controls":{"tight_token_budget":NaN}}'
            ).encode("utf-8"),
        )
        for payload in payloads:
            with self.subTest(payload=payload[-48:]):
                request = urllib.request.Request(
                    self.base_url + "/api/compile",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(request, timeout=2)
                self.assertEqual(caught.exception.code, 400)

    def test_post_body_size_boundary(self) -> None:
        scenario = context_compiler.load_scenario(context_compiler.DEFAULT_SCENARIO)
        payload = json.dumps(
            {"contract": scenario["contract"], "controls": scenario["controls"]}
        ).encode("utf-8")
        exact = payload + b" " * (
            context_compiler.REQUEST_BODY_MAX_BYTES - len(payload)
        )
        exact_request = urllib.request.Request(
            self.base_url + "/api/compile",
            data=exact,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(exact_request, timeout=2) as response:
            result = json.loads(response.read())
            self.assertEqual(response.status, 200)
            self.assertEqual(result["status"], "compiled")

        oversized_request = urllib.request.Request(
            self.base_url + "/api/compile",
            data=exact + b" ",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(oversized_request, timeout=2)
        self.assertEqual(caught.exception.code, 400)

    def test_static_app_loads(self) -> None:
        with urllib.request.urlopen(self.base_url + "/", timeout=2) as response:
            html = response.read().decode("utf-8")
            self.assertEqual(response.status, 200)
            self.assertIn("Context Contract Compiler · Context Debugger", html)
            self.assertIn("app.js", html)


class StarterContextCompilerHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        QuietContextCompilerHandler.records_path = (
            context_compiler.ROOT / "examples" / "starter_records.jsonl"
        )
        QuietContextCompilerHandler.scenario_path = (
            context_compiler.ROOT / "examples" / "starter_scenario.json"
        )
        cls.server = ThreadingHTTPServer(("localhost", 0), QuietContextCompilerHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://localhost:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def test_starter_get_and_post_compile(self) -> None:
        with urllib.request.urlopen(self.base_url + "/api/compile", timeout=2) as response:
            result = json.loads(response.read())
            self.assertEqual(response.status, 200)
            self.assertEqual(result["receipt_id"], "cg-506603b39e23d288")

        scenario = context_compiler.load_scenario(
            context_compiler.ROOT / "examples" / "starter_scenario.json"
        )
        payload = json.dumps(
            {"contract": scenario["contract"], "controls": scenario["controls"]}
        ).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + "/api/compile",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            result = json.loads(response.read())
            self.assertEqual(response.status, 200)
            self.assertEqual(result["receipt_id"], "cg-506603b39e23d288")

        invalid_controls = dict(scenario["controls"])
        invalid_controls["future_reveal"] = True
        invalid_payload = json.dumps(
            {"contract": scenario["contract"], "controls": invalid_controls}
        ).encode("utf-8")
        invalid_request = urllib.request.Request(
            self.base_url + "/api/compile",
            data=invalid_payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(invalid_request, timeout=2)
        self.assertEqual(caught.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
