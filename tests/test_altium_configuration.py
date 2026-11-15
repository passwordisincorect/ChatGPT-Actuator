import tempfile
import unittest
import hashlib
from pathlib import Path

from chatgpt_actuator.tools.eda_altium_analysis import erc
from chatgpt_actuator.tools.eda_altium_configuration import annotate

RULE_NUMBERS = tuple(i for i in range(1, 166) if i not in {120, 121})


def project_text(suppressed="1", matrix=None):
    if matrix is None:
        matrix = {f"L{i}": "N" * 17 for i in range(1, 18)}
    types = "\n".join(f"Type{i}={i % 4}" for i in RULE_NUMBERS)
    connection = "\n".join(f"{name}={value}" for name, value in matrix.items())
    return ("[Design]\nReportSuppressedErrorsInMessages=" + suppressed +
            "\n[Document1]\nDocumentPath=Sheet.SchDoc\n" +
            "\n[Electrical Rules Check]\n" + types +
            "\n[ERC Connection Matrix]\n" + connection + "\n")


def native_rows():
    effective = {f"Type{i}": i % 4 for i in RULE_NUMBERS}
    settings = [{"kind": "erc_error_setting", "code": f"eError_{i}", "severity": "warning"}
                for i in RULE_NUMBERS]
    return settings + [{"kind": "erc_summary", "complete": True, "violation_count": 1,
             "report_suppressed": True, "error_reporting_complete": True,
             "error_reporting_setting_count": len(RULE_NUMBERS), "effective_error_reporting": effective},
            {"kind": "erc_violation", "message": "Suppressed warning", "severity": "warning",
             "source": "Sheet.SchDoc", "suppressed": True}]


def native_summary(rows):
    return next(row for row in rows if row["kind"] == "erc_summary")


class AltiumConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name) / "Fixture.PrjPcb"
        self.project.write_text(project_text(), encoding="utf-8")
        self.sheet = Path(self.temp.name) / "Sheet.SchDoc"
        self.sheet.write_bytes(b"sheet fixture")

    def tearDown(self):
        self.temp.cleanup()

    def test_valid_configuration_binds_saved_hash_and_preserves_violations(self):
        rows = self.attach_reload(native_rows())
        source_violation = dict(rows[-1])
        annotated = annotate(rows, self.project, reload_evidence=self.reload_evidence)
        summary = next(row for row in annotated if row["kind"] == "erc_summary")
        self.assertTrue(summary["configuration_complete"])
        self.assertEqual(163, len(summary["config"]["error_reporting"]))
        self.assertEqual(17, len(summary["config"]["connection_matrix"]))
        self.assertEqual(source_violation, annotated[-1])
        self.assertNotIn("config", rows[1])
        self.assertEqual("passed", erc(annotated)["status"])

    def test_utf8_bom_and_case_insensitive_sections_are_supported(self):
        self.project.write_bytes(project_text().replace("[Design]", "[dEsIgN]").encode("utf-8-sig"))
        rows = self.attach_reload(native_rows())
        summary = next(row for row in annotate(rows, self.project, reload_evidence=self.reload_evidence) if row["kind"] == "erc_summary")
        self.assertTrue(summary["configuration_complete"])

    def test_native_error_levels_or_suppression_mismatch_never_pass(self):
        rows = self.attach_reload(native_rows())
        native_summary(rows)["effective_error_reporting"]["Type119"] = 2
        summary = next(row for row in annotate(rows, self.project, reload_evidence=self.reload_evidence) if row["kind"] == "erc_summary")
        self.assertFalse(summary["configuration_complete"])
        self.assertEqual("incomplete", erc(annotate(rows, self.project, reload_evidence=self.reload_evidence))["status"])
        rows = self.attach_reload(native_rows())
        native_summary(rows)["report_suppressed"] = False
        summary = next(row for row in annotate(rows, self.project, reload_evidence=self.reload_evidence) if row["kind"] == "erc_summary")
        self.assertIn("suppressed-error", summary["configuration_note"])

    def test_case_variant_duplicate_native_rule_key_never_passes(self):
        rows = native_rows()
        effective = native_summary(rows)["effective_error_reporting"]
        # Insert a contradictory case alias first, then the canonical spelling;
        # normalization currently overwrites the conflict and appears to match.
        ambiguous = {"type1": 3, **effective}
        native_summary(rows)["effective_error_reporting"] = ambiguous
        summary = next(row for row in annotate(rows, self.project) if row["kind"] == "erc_summary")
        self.assertFalse(summary["configuration_complete"])

    def test_native_enums_without_project_type_mapping_stay_incomplete(self):
        rows = native_rows()
        native_summary(rows).pop("effective_error_reporting")
        summary = next(row for row in annotate(rows, self.project) if row["kind"] == "erc_summary")
        self.assertFalse(summary["configuration_complete"])
        self.assertIn("reload evidence", summary["configuration_note"])

    def reload_rows(self):
        rows = native_rows()
        native_summary(rows).pop("effective_error_reporting")
        return self.attach_reload(rows)

    def attach_reload(self, rows):
        native_summary(rows)["operation_id"] = "b" * 32
        rows += [{"kind": "compile_summary", "success": True, "complete": True},
                 {"kind": "project", "path": str(self.project), "documents": 1},
                 {"kind": "document", "path": str(self.sheet)}]
        self.reload_evidence = {"kind": "project_configuration_reload", "complete": True, "previously_open": False,
                    "project_path": str(self.project), "sheet_path": str(self.sheet),
                    "project_sha256": hashlib.sha256(self.project.read_bytes()).hexdigest(),
                    "sheet_sha256": hashlib.sha256(self.sheet.read_bytes()).hexdigest(),
                    "prepare_operation_id": "a" * 32, "inspection_operation_id": "b" * 32,
                    "adapter_sha256": "c" * 64}
        return rows

    def test_native_reload_certifies_saved_config_without_enum_mapping(self):
        rows = self.reload_rows(); evidence = self.reload_evidence
        summary = native_summary(annotate(rows, self.project, reload_evidence=evidence))
        self.assertTrue(summary["configuration_complete"])
        self.assertEqual("saved_project_natively_reloaded", summary["config"]["method"])
        self.assertIsNone(summary["config"]["native_error_reporting"])

    def test_reload_checks_suppression_and_accepts_single_native_configuration_section(self):
        self.project.write_text(project_text().replace("DocumentPath=Sheet.SchDoc", 'DocumentPath="Sheet.SchDoc"')
                                + "[Configuration1]\nName=Default\n", encoding="utf-8")
        rows = self.reload_rows(); evidence = self.reload_evidence
        evidence["project_sha256"] = hashlib.sha256(self.project.read_bytes()).hexdigest()
        self.assertTrue(native_summary(annotate(rows, self.project, reload_evidence=evidence))["configuration_complete"])
        native_summary(rows)["report_suppressed"] = False
        self.assertFalse(native_summary(annotate(rows, self.project, reload_evidence=evidence))["configuration_complete"])

    def test_reload_rejects_actual_ambiguous_sections_bad_compile_and_duplicate_operation_rows(self):
        for extra in ("[Configuration1]\nName=A\n[Configuration2]\nName=B\n", "[Variant1]\nName=A\n"):
            self.project.write_text(project_text() + extra, encoding="utf-8")
            rows = self.reload_rows(); evidence = self.reload_evidence
            evidence["project_sha256"] = hashlib.sha256(self.project.read_bytes()).hexdigest()
            self.assertFalse(native_summary(annotate(rows, self.project, reload_evidence=evidence))["configuration_complete"])
        self.project.write_text(project_text(), encoding="utf-8")
        for extra in ([{"kind": "compile_summary", "success": False, "complete": True}],
                      [{"kind": "other", "operation_id": "e" * 32}]):
            rows = self.reload_rows(); evidence = self.reload_evidence
            rows.extend(extra)
            self.assertFalse(native_summary(annotate(rows, self.project, reload_evidence=evidence))["configuration_complete"])

    def test_reload_rejects_malformed_paths_without_raising(self):
        for key in ("project_path", "sheet_path"):
            rows = self.reload_rows(); evidence = self.reload_evidence
            evidence[key] = None
            self.assertFalse(native_summary(annotate(rows, self.project, reload_evidence=evidence))["configuration_complete"])

    def test_reload_evidence_must_match_paths_hashes_and_operation_ids(self):
        for key, value in (("project_path", str(self.sheet)), ("project_sha256", "0" * 64),
                           ("sheet_sha256", "0" * 64), ("inspection_operation_id", "d" * 32)):
            rows = self.reload_rows(); evidence = self.reload_evidence
            evidence[key] = value
            self.assertFalse(native_summary(annotate(rows, self.project, reload_evidence=evidence))["configuration_complete"], key)

    def test_reload_rejects_variants_and_malformed_claimed_mapping(self):
        rows = self.reload_rows(); evidence = self.reload_evidence
        self.project.write_text(project_text() + "[Variant1]\nName=Production\n", encoding="utf-8")
        evidence["project_sha256"] = hashlib.sha256(self.project.read_bytes()).hexdigest()
        self.assertFalse(native_summary(annotate(rows, self.project, reload_evidence=evidence))["configuration_complete"])

    def test_matching_map_still_requires_fresh_reload_proof(self):
        rows = native_rows()
        self.assertFalse(native_summary(annotate(rows, self.project))["configuration_complete"])

    def test_contradictory_map_stays_incomplete_even_with_valid_reload_proof(self):
        rows = self.attach_reload(native_rows())
        native_summary(rows)["effective_error_reporting"]["Type119"] = 2
        result = native_summary(annotate(rows, self.project, reload_evidence=self.reload_evidence))
        self.assertFalse(result["configuration_complete"])
        self.assertIn("do not match", result["configuration_note"])
        self.project.write_text(project_text(), encoding="utf-8")
        rows = self.reload_rows(); evidence = self.reload_evidence
        native_summary(rows)["effective_error_reporting"] = {"Type1": 99}
        self.assertFalse(native_summary(annotate(rows, self.project, reload_evidence=evidence))["configuration_complete"])

    def test_bridge_project_labels_do_not_certify_unverified_native_mapping(self):
        settings = [{"kind": "erc_error_setting", "code": f"Type{i}", "project_type": f"Type{i}",
                     "project_level": i % 4, "kind_id": i} for i in RULE_NUMBERS]
        violation = {"kind": "erc_violation", "message": "Suppressed warning", "severity": "warning",
                     "source": "Sheet.SchDoc", "suppressed": True}
        summary = {"kind": "erc_summary", "complete": True, "violation_count": 1,
                   "report_suppressed": True, "error_reporting_complete": True,
                   "error_reporting_setting_count": len(settings),
                   "configuration_note": "TypeN to native enum assumption unverified"}
        rows = settings + [summary, violation]
        original = [dict(row) for row in rows]

        annotated = annotate(rows, self.project)
        result_summary = next(row for row in annotated if row["kind"] == "erc_summary")
        self.assertFalse(result_summary["configuration_complete"])
        self.assertIsNone(result_summary["config"]["native_error_reporting"])
        self.assertEqual("incomplete", erc(annotated)["status"])
        self.assertEqual(violation, annotated[-1])
        self.assertEqual(original, rows)

    def test_missing_or_malformed_matrix_and_levels_fail_closed(self):
        self.project.write_text(project_text(matrix={f"L{i}": "N" * 17 for i in range(1, 17)}), encoding="utf-8")
        summary = next(row for row in annotate(native_rows(), self.project) if row["kind"] == "erc_summary")
        self.assertFalse(summary["configuration_complete"])
        self.assertEqual({}, summary["config"])
        malformed = {f"L{i}": "N" * 17 for i in range(1, 18)}
        malformed["L4"] = "N" * 16 + "X"
        self.project.write_text(project_text(matrix=malformed), encoding="utf-8")
        summary = next(row for row in annotate(native_rows(), self.project) if row["kind"] == "erc_summary")
        self.assertFalse(summary["configuration_complete"])
        self.assertIn("matrix row", summary["configuration_note"])

    def test_sparse_or_unexpected_saved_type_coverage_fails_closed(self):
        projects = (project_text().replace("Type165=1\n", ""),
                    project_text().replace("[ERC Connection Matrix]", "Type166=1\n[ERC Connection Matrix]"))
        for project_text_value in projects:
            self.project.write_text(project_text_value, encoding="utf-8")
            rows = self.attach_reload(native_rows())
            source_violation = dict(rows[-1])
            annotated = annotate(rows, self.project, reload_evidence=self.reload_evidence)
            summary = native_summary(annotated)
            self.assertFalse(summary["configuration_complete"])
            self.assertEqual({}, summary["config"])
            self.assertIn("coverage", summary["configuration_note"])
            self.assertEqual(source_violation, annotated[-1])

    def test_malformed_prefixed_rule_keys_fail_closed_and_preserve_violations(self):
        malformed_projects = (
            project_text().replace("[ERC Connection Matrix]", "TypeFoo=1\n[ERC Connection Matrix]"),
            project_text().replace("[ERC Connection Matrix]", "Type01=1\n[ERC Connection Matrix]"),
            project_text() + "L00=" + "N" * 17 + "\n",
            project_text() + "LFoo=" + "N" * 17 + "\n",
        )
        for malformed in malformed_projects:
            self.project.write_text(malformed, encoding="utf-8")
            rows = self.attach_reload(native_rows())
            violation = dict(rows[-1])
            annotated = annotate(rows, self.project, reload_evidence=self.reload_evidence)
            summary = native_summary(annotated)
            self.assertFalse(summary["configuration_complete"])
            self.assertEqual({}, summary["config"])
            self.assertIn("malformed", summary["configuration_note"])
            self.assertEqual(violation, annotated[-1])

    def test_invalid_project_path_and_violation_count_do_not_certify_erc(self):
        summary = next(row for row in annotate(native_rows(), self.project.with_suffix(".txt"))
                       if row["kind"] == "erc_summary")
        self.assertFalse(summary["configuration_complete"])
        self.assertEqual({}, summary["config"])
        rows = native_rows()
        native_summary(rows)["violation_count"] = 0
        summary = next(row for row in annotate(rows, self.project) if row["kind"] == "erc_summary")
        self.assertFalse(summary["configuration_complete"])
        self.assertIn("violation count", summary["configuration_note"])


if __name__ == "__main__":
    unittest.main()
