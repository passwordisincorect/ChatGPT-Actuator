"""A stale IR revision must not win in a second Actuator process."""
import json
import multiprocessing
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from chatgpt_actuator.audit import AuditLogger
from chatgpt_actuator.config import EDAConfig
from chatgpt_actuator.security import PathGuard
from chatgpt_actuator.tools.eda_altium import AltiumAdapter


def adapter(base):
    project = Path(base) / "project"
    config = EDAConfig(True, True, True, False, (str(project),), "", "", .02, 1, 200000)
    return AltiumAdapter(config, AuditLogger(False, str(Path(base) / "audit.log")),
                         PathGuard((project,)), lambda: None, state_dir=Path(base) / "private-state")


def validation(plan, **_):
    return {"ok":True, "components":plan["components"], "nets":plan["nets"], "library_hashes":{}}


def revise(base, value, started, entered_validation, release, results):
    try:
        api = adapter(base)
        def paused_validation(plan, **kwargs):
            entered_validation.set()
            if not release.wait(10):
                raise TimeoutError("Owned revision fixture was not released")
            return validation(plan, **kwargs)
        started.set()
        with patch.object(api, "_validate", side_effect=paused_validation):
            result = api.plan_create(str(Path(base) / "project"), "revision_race",
                                     [{"reference":"R1", "value":value}], [{"name":"GND", "pins":[]}],
                                     expected_revision=1)
        results.put((value, "succeeded", result["revision"]))
    except BaseException as exc:
        results.put((value, type(exc).__name__, str(exc)))


class AltiumPlanConcurrencyTests(unittest.TestCase):
    def test_two_processes_cannot_both_replace_the_same_expected_revision(self):
        context = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory(prefix="altium-plan-race-") as directory:
            project = Path(directory) / "project"
            project.mkdir()
            api = adapter(directory)
            with patch.object(api, "_validate", side_effect=validation):
                initial = api.plan_create(str(project), "revision_race", [{"reference":"R1", "value":"initial"}],
                                          [{"name":"GND", "pins":[]}])
            release = context.Event()
            results = context.Queue()
            entered_a, entered_b = context.Event(), context.Event()
            started_a, started_b = context.Event(), context.Event()
            first = context.Process(target=revise, args=(directory,"first",started_a,entered_a,release,results))
            second = context.Process(target=revise, args=(directory,"second",started_b,entered_b,release,results))
            first.start()
            try:
                self.assertTrue(entered_a.wait(10), "First revision did not reach validation")
                second.start()
                self.assertTrue(started_b.wait(10), "Second process did not start")
                # The first writer holds its old-revision read open. Without a
                # cross-process lock the second writer reaches validation too.
                entered_b.wait(1)
                release.set()
                first.join(15)
                second.join(15)
                self.assertEqual(first.exitcode, 0)
                self.assertEqual(second.exitcode, 0)
                rows = sorted([results.get(timeout=2), results.get(timeout=2)])
                self.assertEqual(rows[0], ("first", "succeeded", 2))
                self.assertEqual(rows[1][0:2], ("second", "ValueError"), rows)
                final = json.loads(Path(initial["ir_path"]).read_text(encoding="utf-8"))
                self.assertEqual((final["revision"], final["components"][0]["value"]), (2, "first"))
                history = list((Path(initial["ir_path"]).parent / ".history").glob("*.json"))
                self.assertEqual(len(history), 1)
                self.assertEqual(json.loads(history[0].read_text(encoding="utf-8"))["revision"], 1)
            finally:
                release.set()
                for process in (first, second):
                    if process.pid is not None:
                        if process.is_alive():
                            process.terminate()
                        process.join(5)
                results.close()
                results.join_thread()


if __name__ == "__main__":
    unittest.main()
