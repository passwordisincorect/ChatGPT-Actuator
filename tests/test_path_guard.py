import tempfile
import unittest
from pathlib import Path

from chatgpt_actuator.errors import AccessDeniedError
from chatgpt_actuator.security import PathGuard


class PathGuardTests(unittest.TestCase):
    def test_allows_file_below_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "allowed"
            root.mkdir()
            target = root / "hello.txt"
            target.write_text("hello", encoding="utf-8")

            guard = PathGuard([root])
            self.assertEqual(guard.resolve_existing(target), target.resolve())

    def test_allows_new_target_below_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "allowed"
            root.mkdir()
            target = root / "new-folder" / "hello.txt"

            guard = PathGuard([root])
            self.assertEqual(guard.resolve_target(target), target.resolve(strict=False))

    def test_denies_file_outside_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "allowed"
            root.mkdir()
            outside = base / "secret.txt"
            outside.write_text("secret", encoding="utf-8")

            guard = PathGuard([root])
            with self.assertRaises(AccessDeniedError):
                guard.resolve_existing(outside)

    def test_denies_new_target_outside_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "allowed"
            root.mkdir()
            outside = base / "new.txt"

            guard = PathGuard([root])
            with self.assertRaises(AccessDeniedError):
                guard.resolve_target(outside)


if __name__ == "__main__":
    unittest.main()
