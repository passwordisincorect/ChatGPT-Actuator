import tempfile
import unittest
from pathlib import Path

from chatgpt_actuator.audit import AuditLogger
from chatgpt_actuator.config import FileSystemConfig
from chatgpt_actuator.tools import FileSystemService


class FileSystemTests(unittest.TestCase):
    def make_service(self, root: Path) -> FileSystemService:
        config = FileSystemConfig(
            enabled=True,
            read_only=False,
            allow_create=True,
            allow_edit=True,
            allow_move=True,
            allow_delete=True,
            allowed_roots=(str(root),),
            max_list_items=100,
            max_read_bytes=1024 * 1024,
            max_search_results=100,
        )
        return FileSystemService(config, AuditLogger(False, "unused.log"))

    def test_list_and_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "hello.txt").write_text("Xin chao", encoding="utf-8")
            service = self.make_service(root)

            listing = service.list_directory(str(root))
            self.assertEqual(listing["items"][0]["name"], "hello.txt")

            content = service.read_text(str(root / "hello.txt"))
            self.assertEqual(content["content"], "Xin chao")

    def test_search_by_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "motor_notes.txt").write_text("x", encoding="utf-8")
            (root / "other.txt").write_text("x", encoding="utf-8")
            service = self.make_service(root)

            result = service.search(str(root), "motor")
            self.assertEqual(len(result["results"]), 1)
            self.assertEqual(result["results"][0]["name"], "motor_notes.txt")

    def test_create_edit_move_delete_cycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self.make_service(root)

            folder = root / "demo"
            service.create_directory(str(folder))

            file1 = folder / "a.txt"
            created = service.write_text(str(file1), "hello")
            self.assertEqual(created["action"], "create")
            self.assertTrue(file1.exists())

            edited = service.replace_in_file(str(file1), "hello", "xin chao")
            self.assertEqual(edited["replacements_made"], 1)
            self.assertEqual(file1.read_text(encoding="utf-8"), "xin chao")

            file2 = folder / "renamed.txt"
            service.move_path(str(file1), str(file2))
            self.assertFalse(file1.exists())
            self.assertTrue(file2.exists())

            service.delete_path(str(file2))
            self.assertFalse(file2.exists())

            service.delete_path(str(folder))
            self.assertFalse(folder.exists())

    def test_overwrite_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self.make_service(root)
            target = root / "x.txt"
            target.write_text("old", encoding="utf-8")

            with self.assertRaises(FileExistsError):
                service.write_text(str(target), "new", overwrite=False)

            service.write_text(str(target), "new", overwrite=True)
            self.assertEqual(target.read_text(encoding="utf-8"), "new")

    def test_allowed_root_cannot_be_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self.make_service(root)

            with self.assertRaises(PermissionError):
                service.delete_path(str(root), recursive=True)


if __name__ == "__main__":
    unittest.main()
