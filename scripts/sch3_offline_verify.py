"""Run the public checkout's offline regressions with a source-bound receipt."""
from pathlib import Path
import datetime
import hashlib
import json
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def source_hashes():
    return {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for folder in ("src/chatgpt_actuator", "tests", "integrations")
            for p in sorted((ROOT / folder).rglob("*")) if p.is_file() and "__pycache__" not in p.parts}


def main():
    from chatgpt_actuator.tools.eda_altium import AltiumAdapter
    (ROOT / "reports").mkdir(exist_ok=True)
    source = source_hashes()
    started = time.monotonic()
    with (ROOT / "reports/regression-final.log").open("w", encoding="utf-8") as log:
        result = unittest.TextTestRunner(stream=log, verbosity=2).run(
            unittest.defaultTestLoader.discover(str(ROOT / "tests"), pattern="test_*.py"))
    assert source == source_hashes(), "Source changed during regression"
    report = {"status": "passed" if result.wasSuccessful() else "failed", "tests": result.testsRun,
              "failures": len(result.failures), "errors": len(result.errors), "skipped": len(result.skipped),
              "seconds": round(time.monotonic() - started, 3), "adapter_sha256": AltiumAdapter._adapter_fingerprint(),
              "source_hashes": source, "checked_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "log": "reports/regression-final.log"}
    (ROOT / "reports/offline-verification.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "source_hashes"}))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
