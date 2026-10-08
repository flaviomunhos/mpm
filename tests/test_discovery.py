import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mpm.cli import main
from mpm.core.manifest import Manifest, build_manifest
from mpm.core.system import discover_system
from mpm.core.user import discover_user

from tests.helpers import make_profile


class SystemDiscoveryTest(unittest.TestCase):
    def test_fields_present(self):
        s = discover_system()
        for value in (s.hostname, s.os_family, s.os_name, s.architecture,
                      s.python_version, s.collected_at):
            self.assertTrue(value)


class UserDiscoveryTest(unittest.TestCase):
    def test_fields_present(self):
        u = discover_user()
        self.assertTrue(u.username)
        self.assertTrue(u.user_id)
        self.assertTrue(u.profile_path)


class ManifestTest(unittest.TestCase):
    def test_roundtrip_with_inventory(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = make_profile(Path(tmp))
            inv = Path(tmp) / "out" / "m.inventory.jsonl"
            m = build_manifest(profile=profile, include_appdata=False, inventory_path=inv)
            path = m.write(Path(tmp) / "out" / "m.json")
            loaded = Manifest.read(path)
            self.assertTrue(inv.is_file())
        self.assertEqual(loaded.to_dict(), m.to_dict())
        self.assertEqual(loaded.source_profile.username, "tester")
        self.assertEqual(loaded.inventory_file, "m.inventory.jsonl")
        self.assertEqual(loaded.folders["pictures"]["total_bytes"], 1000)
        self.assertEqual(set(loaded.folders), {"desktop", "documents", "pictures"})

    def test_migration_id_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = build_manifest(profile=make_profile(Path(tmp)), include_appdata=False)
        self.assertRegex(m.migration_id, r"^MPM-\d{8}-[0-9A-F]{6}$")

    def test_rejects_unknown_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = build_manifest(profile=make_profile(Path(tmp)), include_appdata=False).to_dict()
        data["schema_version"] = 999
        with self.assertRaises(ValueError):
            Manifest.from_dict(data)


class CliTest(unittest.TestCase):
    def test_discover_json(self):
        self.assertEqual(main(["discover", "--json"]), 0)

    def test_scan_manifest_validate_with_profile_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = make_profile(Path(tmp))
            out = Path(tmp) / "m.json"
            self.assertEqual(main(["scan", "--profile-path", profile.path]), 0)
            self.assertEqual(main(["manifest", "--profile-path", profile.path, "-o", str(out)]), 0)
            data = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(data["source"]["profile"]["username"], "tester")
            self.assertTrue((Path(tmp) / "m.inventory.jsonl").is_file())
            self.assertEqual(main(["validate", str(out)]), 0)

    def test_unknown_user_returns_2(self):
        self.assertEqual(main(["scan", "--user", "usuario-que-nao-existe"]), 2)

    def test_profiles_command(self):
        self.assertEqual(main(["profiles"]), 0)

    def test_validate_bad_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.json"
            bad.write_text("{}", encoding="utf-8")
            self.assertEqual(main(["validate", str(bad)]), 1)


if __name__ == "__main__":
    unittest.main()
