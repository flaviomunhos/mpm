import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mpm.core.appdata import PLUGINS, Plugin, _size, render_text, scan
from mpm.net.appdata_inventory import run_appdata_inventory
from mpm.net.rx import RxClient
from tests.helpers import make_profile
from tests.test_net import Pair, quiet


def put(path: Path, size: int = 10) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


def make_roots(tmp: str) -> dict[str, Path]:
    base = Path(tmp)
    roots = {"profile": base, "appdata": base / "AppData/Roaming", "localappdata": base / "AppData/Local",
             "documents": base / "Documents"}
    chrome = roots["localappdata"] / "Google/Chrome/User Data"
    put(chrome / "Default/Bookmarks", 100)
    put(chrome / "Default/Preferences", 50)
    put(chrome / "Default/Cache/data_0", 5000)               # cache: não conta
    put(chrome / "Default/Code Cache/js/a", 3000)            # cache: não conta
    put(chrome / "Default/Service Worker/CacheStorage/x", 9000)
    put(roots["appdata"] / "Mozilla/Firefox/profiles.ini", 20)
    put(roots["appdata"] / "Mozilla/Firefox/Profiles/abc.default/places.sqlite", 400)
    put(roots["appdata"] / "Notepad++/config.xml", 30)
    put(roots["appdata"] / "WinSCP.ini", 7)
    put(roots["profile"] / ".ssh/id_ed25519", 60)
    put(roots["documents"] / "Arquivos do Outlook/mail.pst", 1000)
    return roots


class ScanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.roots = make_roots(self.tmp.name)
        self.results = scan(PLUGINS, self.roots, lambda key: key == r"Software\SimonTatham")
        self.by_id = {r["id"]: r for r in self.results}

    def test_cache_directories_are_not_counted(self):
        chrome = self.by_id["chrome"]
        self.assertEqual((chrome["files"], chrome["bytes"]), (2, 150))

    def test_finds_files_registry_and_misses(self):
        self.assertEqual(self.by_id["firefox"]["files"], 2)
        self.assertEqual(self.by_id["firefox"]["bytes"], 420)
        self.assertEqual(self.by_id["winscp"]["bytes"], 7)           # arquivo único (WinSCP.ini)
        self.assertTrue(self.by_id["putty"]["found"])
        self.assertEqual(self.by_id["putty"]["registry"], [r"Software\SimonTatham"])
        self.assertEqual(self.by_id["outlook"]["bytes"], 1000)
        self.assertEqual(self.by_id["ssh"]["bytes"], 60)
        for missing in ("edge", "brave", "vlc", "filezilla", "git", "mapped-drives"):
            self.assertFalse(self.by_id[missing]["found"], missing)
        self.assertEqual(len(self.results), len(PLUGINS))

    def test_links_are_not_followed(self):
        outside = Path(self.tmp.name) / "fora"
        put(outside / "grande.bin", 99999)
        try:
            os.symlink(outside, self.roots["appdata"] / "Notepad++/link", target_is_directory=True)
            os.symlink(outside / "grande.bin", self.roots["appdata"] / "Notepad++/arquivo-link")
        except (OSError, NotImplementedError):
            self.skipTest("sem suporte a links simbólicos")
        notepad = next(r for r in scan(PLUGINS, self.roots) if r["id"] == "notepadpp")
        self.assertEqual((notepad["files"], notepad["bytes"]), (1, 30))

    def test_unreadable_registry_is_not_an_error(self):
        def boom(_key):
            raise OSError("acesso negado")
        self.assertFalse(next(r for r in scan(PLUGINS, self.roots, boom) if r["id"] == "putty")["found"])

    def test_missing_root_is_skipped(self):
        only_profile = {"profile": self.roots["profile"]}
        found = [r["id"] for r in scan(PLUGINS, only_profile) if r["found"]]
        self.assertEqual(found, ["ssh"])

    def test_catalog_is_well_formed(self):
        ids = [p.id for p in PLUGINS]
        self.assertEqual(len(ids), len(set(ids)))
        for plugin in PLUGINS:
            self.assertTrue(plugin.paths or plugin.registry, plugin.id)
            for root, rel in plugin.paths:
                self.assertIn(root, ("appdata", "localappdata", "documents", "profile"))
                self.assertNotIn("\\", rel)
                self.assertFalse(rel.startswith("/"))

    def test_render_text(self):
        text = render_text(self.results, "C:\\Users\\master")
        self.assertIn("C:\\Users\\master", text)
        self.assertIn("Google Chrome", text)
        self.assertIn("AVISOS", text)
        self.assertIn("DPAPI", text)
        self.assertIn("SEM DADOS NESTE PERFIL", text)
        self.assertLess(text.index("Outlook"), text.index("Notepad++"))      # maiores primeiro
        self.assertEqual(_size(1536), "1.5 KB")


class AppdataNetTest(unittest.TestCase):
    class Backend:
        def __init__(self, roots, fail=False):
            self._roots, self.fail = roots, fail

        def roots(self):
            if self.fail:
                raise RuntimeError("sem perfil")
            return self._roots

        def roots_for(self, profile):
            return self._roots

        def registry_key_exists(self, key, hive=None):
            return key == r"Software\SimonTatham"

    def run_it(self, tmp, backend):
        pair = Pair(make_profile(Path(tmp) / "src"))
        client = RxClient(pair.accept())
        logs: list[str] = []
        try:
            with mock.patch("mpm.platforms.appdata", return_value=backend):
                results = run_appdata_inventory(client, Path(tmp) / "out", log=logs.append)
        finally:
            client.close()
            pair.finish()
        return results, logs

    def test_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            results, _ = self.run_it(tmp, self.Backend(make_roots(str(Path(tmp) / "home"))))
            out = Path(tmp) / "out"
            self.assertEqual(next(r for r in results if r["id"] == "chrome")["bytes"], 150)
            self.assertIn("Mozilla Firefox", (out / "appdata-report.txt").read_text(encoding="utf-8"))
            saved = json.loads((out / "appdata-inventory.json").read_text(encoding="utf-8"))
            self.assertEqual(len(saved["results"]), len(PLUGINS))
            self.assertTrue(saved["profile"].endswith("home"))

    def test_tx_failure_is_a_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            results, logs = self.run_it(tmp, self.Backend({}, fail=True))
            self.assertEqual(results, [])
            self.assertTrue(any("Aviso do TX" in line and "sem perfil" in line for line in logs))


if __name__ == "__main__":
    unittest.main()
