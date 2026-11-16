import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("build_release", ROOT / "scripts" / "build_release.py")
build_release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build_release)


class ReleasePackagingTests(unittest.TestCase):
    def test_release_strips_private_browser_grant(self):
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp)
            (stage / "config").mkdir()
            raw = json.loads((ROOT / "config/config.json").read_text(encoding="utf-8"))
            raw.setdefault("security", {})["browser_auto_connect"] = {
                "enabled": True, "owner": "private-owner", "allow_temporary_focus": True,
                "user_data_dir": "private-profile", "profile_directory": "Default",
            }
            config = stage / "config/config.json"
            config.write_text(json.dumps(raw), encoding="utf-8")
            build_release.write_safe_release_config(stage, "1.3.0rc2")
            exported = json.loads(config.read_text(encoding="utf-8"))
            self.assertNotIn("browser_auto_connect", exported["security"])
            build_release.validate_release_tree(stage)

    def test_packages_notes_for_project_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("src", "config", "tests", "integrations", "docs", "scripts"):
                (root / name).mkdir()
            fixed_files = (
                "pyproject.toml", "README.md", "README.vi.md", "README-SCH3.md",
                "LICENSE", "CHANGELOG.md", "RELEASE_NOTES_1.3.0rc1.md",
            )
            for name in fixed_files:
                (root / name).write_text(name, encoding="utf-8")

            stage = root / "stage"
            with patch.object(build_release, "ROOT", root):
                build_release.copy_release_tree(stage, "1.3.0rc1")

            self.assertEqual(
                (stage / "RELEASE_NOTES_1.3.0rc1.md").read_text(encoding="utf-8"),
                "RELEASE_NOTES_1.3.0rc1.md",
            )
            self.assertFalse((stage / "RELEASE_NOTES_1.2.0.md").exists())


if __name__ == "__main__":
    unittest.main()
