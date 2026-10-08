import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from mpm.core.filesystem import (
    InventoryWriter,
    discover_folders,
    scan_tree,
    summarize_children,
)
from mpm.core.models import KNOWN_FOLDER_KEYS, FolderScan

from tests.helpers import make_profile


def _blank(path: Path) -> FolderScan:
    return FolderScan(key="t", path=str(path), exists=True, resolved_by="test", redirected=False)


class ScanTreeTest(unittest.TestCase):
    def test_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a" / "b").mkdir(parents=True)
            (root / "f1").write_bytes(b"1234")
            (root / "a" / "f2").write_bytes(b"12")
            (root / "a" / "b" / "f3").write_bytes(b"")
            scan = scan_tree(root, _blank(root))
        self.assertEqual(scan.file_count, 3)
        self.assertEqual(scan.dir_count, 2)
        self.assertEqual(scan.total_bytes, 6)
        self.assertEqual(scan.error_count, 0)

    def test_symlink_is_not_followed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            outside = Path(tmp) / "outside"
            root.mkdir()
            outside.mkdir()
            (outside / "big").write_bytes(b"x" * 1000)
            try:
                os.symlink(outside, root / "link", target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("sem permissão para criar symlink")
            scan = scan_tree(root, _blank(root))
        self.assertEqual(scan.file_count, 0)
        self.assertEqual(scan.skipped_count, 1)

    def test_record_uses_posix_relative_paths(self):
        seen = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a").mkdir()
            (root / "a" / "f").write_bytes(b"12")
            scan_tree(root, _blank(root), lambda rel, size, m: seen.append((rel, size)))
        self.assertEqual(seen, [("a/f", 2)])

    def test_missing_root_counts_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            ghost = Path(tmp) / "nao-existe"
            scan = scan_tree(ghost, _blank(ghost))
        self.assertEqual(scan.error_count, 1)


class DiscoverFoldersTest(unittest.TestCase):
    def test_only_the_three_folders(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = discover_folders(make_profile(Path(tmp)))
        self.assertEqual(tuple(result), KNOWN_FOLDER_KEYS)
        self.assertEqual(result["desktop"].file_count, 1)
        self.assertEqual(result["documents"].file_count, 2)
        self.assertEqual(result["documents"].total_bytes, 101)
        self.assertEqual(result["pictures"].total_bytes, 1000)
        self.assertNotIn("downloads", result)
        self.assertTrue(all(not s.redirected for s in result.values()))
        total = sum(s.total_bytes for s in result.values())
        self.assertEqual(total, 10 + 101 + 1000)   # Music (5000) e Downloads (9000) ficaram de fora

    def test_missing_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = make_profile(Path(tmp))
            shutil.rmtree(Path(profile.path) / "Pictures")
            result = discover_folders(profile)
        self.assertFalse(result["pictures"].exists)
        self.assertEqual(result["pictures"].file_count, 0)

    def test_inventory_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = make_profile(Path(tmp))
            inv = Path(tmp) / "out" / "inv.jsonl"
            with InventoryWriter(inv) as writer:
                discover_folders(profile, writer)
            lines = [json.loads(l) for l in inv.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(lines), 4)
        by_path = {(l["folder"], l["path"]): l["size"] for l in lines}
        self.assertEqual(by_path[("documents", "sub/b.bin")], 100)
        self.assertEqual(by_path[("desktop", "a.txt")], 10)


class SummarizeChildrenTest(unittest.TestCase):
    def test_sorted_by_size_and_loose_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "small").mkdir()
            (root / "big").mkdir()
            (root / "small" / "f").write_bytes(b"1")
            (root / "big" / "f").write_bytes(b"1" * 50)
            (root / "loose.txt").write_bytes(b"123")
            summary = summarize_children(root)
        self.assertEqual([e["name"] for e in summary["entries"]], ["big", "small"])
        self.assertEqual(summary["loose_files"], {"file_count": 1, "total_bytes": 3})

    def test_missing_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            summary = summarize_children(Path(tmp) / "nada")
        self.assertFalse(summary["exists"])
        self.assertEqual(summary["entries"], [])


if __name__ == "__main__":
    unittest.main()
