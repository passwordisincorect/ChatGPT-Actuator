from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from chatgpt_actuator.orchestrator_control import OrchestratorControl


class OrchestratorControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.actuator = root / "ChatGPT-Actuator"
        self.orchestrator = root / "ChatGPT-Orchestrator"
        (self.actuator / "scripts").mkdir(parents=True)
        (self.orchestrator / "data").mkdir(parents=True)
        (self.orchestrator / "config").mkdir(parents=True)
        self.control = OrchestratorControl(self.actuator)
        self.control.root = self.orchestrator

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _make_db(self) -> None:
        con = sqlite3.connect(self.orchestrator / "data" / "orchestrator.db")
        try:
            con.executescript(
                """
                CREATE TABLE tasks(id TEXT, status TEXT, updated_at TEXT);
                CREATE TABLE workers(
                    id TEXT, task_id TEXT, role TEXT, status TEXT, updated_at TEXT
                );
                CREATE TABLE jobs(
                    id TEXT, worker_id TEXT, task_id TEXT, kind TEXT,
                    status TEXT, updated_at TEXT
                );
                INSERT INTO tasks VALUES('T1','RUNNING','2026-10-04');
                INSERT INTO tasks VALUES('T2','COMPLETED','2026-10-04');
                INSERT INTO workers VALUES('W1','T1','solver','RUNNING','2026-10-04');
                INSERT INTO workers VALUES('W2','T2','critic','CLOSED','2026-10-04');
                INSERT INTO jobs VALUES('J1','W1','T1','worker','RUNNING','2026-10-04');
                INSERT INTO jobs VALUES('J2','W2','T2','worker','COMPLETED','2026-10-04');
                """
            )
            con.commit()
        finally:
            con.close()

    def test_runtime_db_uses_orchestrator_status_case(self) -> None:
        self._make_db()
        status = self.control._runtime_db()
        self.assertEqual(status["active_worker_count"], 1)
        self.assertEqual(status["active_job_count"], 1)
        self.assertEqual(status["active_task_count"], 1)
        self.assertEqual(status["workers"][0]["id"], "W1")
        self.assertEqual(status["jobs"][0]["id"], "J1")

    def test_static_info_reads_version_and_backend(self) -> None:
        (self.orchestrator / "pyproject.toml").write_text(
            '[project]\nversion = "0.6.0"\n', encoding="utf-8"
        )
        (self.orchestrator / "config" / "config.json").write_text(
            json.dumps(
                {
                    "backend": "edge_hybrid",
                    "hybrid_cdp_enabled": True,
                    "max_active_workers": 3,
                }
            ),
            encoding="utf-8",
        )
        info = self.control._static_info()
        self.assertEqual(info["version"], "0.6.0")
        self.assertEqual(info["backend"], "edge_hybrid")
        self.assertTrue(info["hybrid_cdp_enabled"])
        self.assertEqual(info["max_active_workers"], 3)

    def test_hybrid_runtime_status_reports_selected_transport(self) -> None:
        status_path = self.orchestrator / "data" / "runtime-status.json"
        status_path.write_text(
            json.dumps(
                {
                    "selected_backend": "uia",
                    "active_cdp_sessions": 0,
                    "active_uia_sessions": 1,
                }
            ),
            encoding="utf-8",
        )
        result = self.control._runtime_backend(
            {"backend": "edge_hybrid"},
            {"active_worker_count": 1},
        )
        self.assertEqual(result["selected_backend"], "uia")
        self.assertEqual(result["source"], "runtime")


if __name__ == "__main__":
    unittest.main()
