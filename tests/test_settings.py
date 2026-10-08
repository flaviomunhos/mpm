import contextlib
import io
import itertools
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mpm.core.appdata import (PLUGINS, dest_base, inventory, parse_key, plugin_by_id, registry_allowlist,
                              retarget_reg)
from mpm.net.appdata_inventory import ProfileError, pick_source_profile
from mpm.net.rx import RemoteError, RxClient
from mpm.net.settings import Backends, SettingsError, resolve_destination, run_settings, settings_entries
from tests.helpers import make_profile
from tests.test_appdata import make_roots, put
from tests.test_net import Pair, needs_crypto

PUTTY_REG = ("Windows Registry Editor Version 5.00\r\n\r\n[HKEY_CURRENT_USER\\Software\\SimonTatham\\PuTTY]\r\n"
             "\r\n[HKEY_CURRENT_USER\\Software\\SimonTatham\\PuTTY\\Sessions\\srv]\r\n\"HostName\"=\"10.0.0.1\"\r\n"
             ).encode("utf-16")


class FakeTxAppdata:
    def __init__(self, roots):
        self._roots = roots
        self.profiles_asked: list[str] = []

    def roots(self):
        return self._roots

    def roots_for(self, profile):
        self.profiles_asked.append(str(profile))
        return self._roots

    def registry_key_exists(self, key, hive=None):
        return key == r"Software\SimonTatham"


class FakeTxReg:
    def __init__(self, hive_error=None):
        self.hive_error, self.hives_used = hive_error, []

    def export_key(self, key, hive=None):
        if hive is not None:
            self.hives_used.append(hive)
        return PUTTY_REG if key == r"Software\SimonTatham" else None

    @contextlib.contextmanager
    def user_hive(self, sid, ntuser):
        if self.hive_error:
            raise RuntimeError(self.hive_error)
        yield "HIVE_FAKE"


class FakeAccounts:
    def __init__(self, profile):
        self.profile, self.owned = profile, []

    def existing_profile_path(self, name):
        return self.profile if name == "maria" else None

    def set_owner(self, path, name):
        self.owned.append((Path(path), name))
        return None

    def is_elevated(self):
        return True


class FakeRxReg:
    def __init__(self, load_error=None):
        self.load_error, self.loaded, self.imported, self.unloaded = load_error, [], [], []

    def load_hive(self, name, ntuser):
        self.loaded.append((name, ntuser))
        return self.load_error

    def import_reg(self, data, hive_name=None):
        self.imported.append((data, hive_name))
        return None

    def unload_hive(self, name):
        self.unloaded.append(name)
        return None


class CoreTest(unittest.TestCase):
    def test_inventory_skips_cache_and_handles_single_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            roots = make_roots(tmp)
            entries, folder_roots, info = inventory(["chrome", "winscp", "chrome"], roots)
            paths = sorted((e["folder"], e["path"]) for e in entries)
            self.assertEqual(paths, [("chrome:0", "Default/Bookmarks"), ("chrome:0", "Default/Preferences"),
                                     ("winscp:0", "WinSCP.ini")])
            self.assertEqual(info["winscp:0"], {"file": True})
            self.assertEqual(folder_roots["winscp:0"], roots["appdata"])          # arquivo único: pasta pai
            self.assertEqual(folder_roots["chrome:0"], roots["localappdata"] / "Google/Chrome/User Data")

    def test_only_catalog_names_are_accepted(self):
        with self.assertRaises(ValueError):
            inventory(["../etc"], {})
        for bad in ("chrome", "chrome:9", "nada:0", "chrome:x", "../x:0"):
            with self.assertRaises(ValueError, msg=bad):
                parse_key(bad)
        self.assertEqual(parse_key("arduino:1")[0].id, "arduino")

    def test_dest_base(self):
        roots = {"appdata": Path("/p/AppData/Roaming"), "profile": Path("/p"), "localappdata": Path("/p/L"),
                 "documents": Path("/p/D")}
        self.assertEqual(dest_base("winscp:0", roots, is_file=True), Path("/p/AppData/Roaming"))
        self.assertEqual(dest_base("firefox:0", roots, is_file=False), Path("/p/AppData/Roaming/Mozilla/Firefox"))
        self.assertEqual(dest_base("git:0", roots, is_file=True), Path("/p"))

    def test_registry_allowlist_and_retarget(self):
        self.assertEqual(registry_allowlist(["putty"]), {r"Software\SimonTatham"})
        self.assertIn("Network", registry_allowlist())
        text = retarget_reg("[HKEY_CURRENT_USER\\Software\\A]\r\n[-HKEY_CURRENT_USER\\Network]\r\n\"v\"=\"HKEY_CURRENT_USER\\\\x\"\r\n", "MPM_1")
        self.assertIn("[HKEY_USERS\\MPM_1\\Software\\A]", text)
        self.assertIn("[-HKEY_USERS\\MPM_1\\Network]", text)
        self.assertIn('"v"="HKEY_CURRENT_USER\\\\x"', text)                 # só cabeçalhos são trocados

    def test_every_plugin_path_is_inside_a_known_root(self):
        for plugin in PLUGINS:
            self.assertIs(plugin_by_id(plugin.id), plugin)


@needs_crypto
class SettingsNetTest(unittest.TestCase):
    counter = itertools.count()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.tx_roots = make_roots(str(base / "tx"))
        self.profile = base / "rx" / "maria"
        self.profile.mkdir(parents=True)
        self.outdir = base / "out"
        self.accounts = FakeAccounts(self.profile)
        self.rxreg = FakeRxReg()
        self.logs: list[str] = []

    def run_it(self, *, regtools=None, answers=(), tx_reg=None, **kw):
        backends = Backends(self.accounts, SimpleNamespace(roots=lambda: {}), regtools or self.rxreg)
        queue = list(answers)
        pair = Pair(make_profile(Path(self.tmp.name) / f"src{next(self.counter)}"))
        client = RxClient(pair.accept())
        self.tx_app, self.tx_reg = FakeTxAppdata(self.tx_roots), tx_reg or FakeTxReg()
        try:
            with mock.patch("mpm.platforms.appdata", return_value=self.tx_app), \
                    mock.patch("mpm.platforms.regtools", return_value=self.tx_reg):
                return run_settings(client, [], backends, outdir=self.outdir, retry_wait=0, log=self.logs.append,
                                    ask=lambda _q: queue.pop(0) if queue else "",
                                    progress_stream=io.StringIO(), **kw)
        finally:
            client.close()
            pair.finish()

    def test_express_copies_files_registry_and_hands_over(self):
        summary = self.run_it(mode="express", to_user="maria")
        self.assertTrue(summary["ok"], self.logs)
        home = self.profile
        self.assertEqual((home / "AppData/Local/Google/Chrome/User Data/Default/Bookmarks").read_bytes(), b"x" * 100)
        self.assertFalse((home / "AppData/Local/Google/Chrome/User Data/Default/Cache").exists())
        self.assertEqual((home / "AppData/Roaming/WinSCP.ini").read_bytes(), b"x" * 7)
        self.assertTrue((home / "AppData/Roaming/Mozilla/Firefox/Profiles/abc.default/places.sqlite").is_file())
        self.assertTrue((home / ".ssh/id_ed25519").is_file())
        self.assertTrue((home / "Documents/Arquivos do Outlook/mail.pst").is_file())
        # registro: hive do usuário carregado, importado apontando para ele e descarregado
        (name, ntuser), = self.rxreg.loaded
        self.assertEqual(ntuser, home / "NTUSER.DAT")
        self.assertEqual([h for _d, h in self.rxreg.imported], [name])
        self.assertEqual(self.rxreg.unloaded, [name])
        self.assertTrue(name.startswith("MPM_"))
        # propriedade passada ao usuário
        self.assertIn((home / "AppData/Roaming/WinSCP.ini", "maria"), self.accounts.owned)
        self.assertTrue(all(user == "maria" for _p, user in self.accounts.owned))
        report = json.loads((self.outdir / "settings/settings-report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["registry"][0]["status"], "importado")
        verified = (self.outdir / "settings/verified.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertGreaterEqual(len(verified), 8)

    def test_second_run_copies_nothing_new(self):
        self.run_it(mode="express", to_user="maria")
        self.logs.clear()
        summary = self.run_it(mode="express", to_user="maria")
        self.assertTrue(summary["ok"])
        self.assertTrue(any("A copiar:" in line and " 0 (" in line for line in self.logs), self.logs[-30:])

    def test_only_limits_programs(self):
        summary = self.run_it(mode="express", only="notepad", to_user="maria")
        self.assertEqual(summary["programs"], ["notepadpp"])
        self.assertTrue((self.profile / "AppData/Roaming/Notepad++/config.xml").is_file())
        self.assertFalse((self.profile / "AppData/Local").exists())
        self.assertEqual(self.rxreg.loaded, [])                         # sem registro escolhido

    def test_custom_menu_with_scripted_answers(self):
        # "n" desmarca tudo, o número do Chrome (lista ordenada por nome) o marca, ENTER segue; ENTER confirma.
        from mpm.core.appdata import scan
        names = [e["label"] for e in settings_entries(scan(PLUGINS, self.tx_roots, lambda k: k == r"Software\SimonTatham"))]
        summary = self.run_it(mode="custom", answers=["n", str(names.index("chrome") + 1), "", ""], to_user="maria")
        self.assertEqual(summary["programs"], ["chrome"])
        self.assertTrue((self.profile / "AppData/Local/Google/Chrome/User Data/Default/Bookmarks").is_file())
        self.assertFalse((self.profile / ".ssh").exists())

    def test_declining_the_confirmation_copies_nothing(self):
        with self.assertRaises(SettingsError):
            self.run_it(mode="custom", only="notepad", answers=["", "n"], to_user="maria")
        self.assertFalse((self.profile / "AppData/Roaming/Notepad++").exists())

    def test_menu_with_yes_is_express(self):
        summary = self.run_it(mode="menu", assume_yes=True, only="winscp|putty", to_user="maria")
        self.assertEqual(sorted(summary["programs"]), ["putty", "winscp"])
        self.assertTrue((self.profile / "AppData/Roaming/WinSCP.ini").is_file())
        self.assertEqual(len(self.rxreg.imported), 1)

    def test_dry_run_writes_and_imports_nothing(self):
        summary = self.run_it(mode="express", dry_run=True, to_user="maria")
        self.assertTrue(summary["ok"])
        self.assertEqual(list(self.profile.iterdir()), [])
        self.assertEqual((self.rxreg.loaded, self.rxreg.imported), ([], []))
        self.assertEqual(self.accounts.owned, [])
        self.assertTrue(any("seriam importadas" in line for line in self.logs))

    def test_registry_failure_to_load_hive_is_reported(self):
        self.rxreg = FakeRxReg(load_error="o arquivo está sendo usado")
        summary = self.run_it(mode="express", only="putty", to_user="maria")
        self.assertFalse(summary["ok"])
        self.assertEqual(summary["registry"][0]["status"], "falhou")
        self.assertIn("sessão aberta", summary["registry"][0]["detail"])
        self.assertEqual(self.rxreg.unloaded, [])                       # nada foi carregado: nada a descarregar

    def test_reads_the_chosen_profile_not_the_tx_user(self):
        summary = self.run_it(mode="express", only="putty|notepad", to_user="maria", source_user="TESTER")
        self.assertTrue(summary["ok"], self.logs)
        self.assertEqual(summary["source_user"], "tester")
        self.assertTrue(any("Perfil de origem no TX: tester" in line for line in self.logs))
        self.assertTrue(self.tx_app.profiles_asked and all(p.endswith("tester") for p in self.tx_app.profiles_asked))
        self.assertEqual(set(self.tx_reg.hives_used), {"HIVE_FAKE"})        # registro lido do hive do perfil
        self.assertTrue((self.profile / "AppData/Roaming/Notepad++/config.xml").is_file())

    def test_unknown_source_user_is_refused(self):
        with self.assertRaises(SettingsError) as ctx:
            self.run_it(mode="express", to_user="maria", source_user="ninguem")
        self.assertIn("Disponíveis: tester", str(ctx.exception))

    def test_tx_cannot_open_the_users_registry_but_files_still_copy(self):
        summary = self.run_it(mode="express", only="putty|notepad", to_user="maria",
                              source_user="tester", tx_reg=FakeTxReg(hive_error="sem privilégio"))
        self.assertTrue(any("Aviso do TX: sem privilégio" in line for line in self.logs))
        self.assertTrue((self.profile / "AppData/Roaming/Notepad++/config.xml").is_file())
        self.assertEqual(summary["registry"], [])

    def test_current_user_mode_imports_into_hkcu(self):
        backends_roots = {"profile": self.profile, "appdata": self.profile / "AppData/Roaming",
                          "localappdata": self.profile / "AppData/Local", "documents": self.profile / "Documents"}
        backends = Backends(self.accounts, SimpleNamespace(roots=lambda: backends_roots), self.rxreg)
        pair = Pair(make_profile(Path(self.tmp.name) / f"src{next(self.counter)}"))
        client = RxClient(pair.accept())
        try:
            with mock.patch("mpm.platforms.appdata", return_value=FakeTxAppdata(self.tx_roots)), \
                    mock.patch("mpm.platforms.regtools", return_value=FakeTxReg()):
                run_settings(client, [], backends, outdir=self.outdir, mode="express", only="putty|notepad",
                             retry_wait=0, log=self.logs.append, progress_stream=io.StringIO())
        finally:
            client.close()
            pair.finish()
        self.assertEqual(self.rxreg.loaded, [])
        self.assertEqual([h for _d, h in self.rxreg.imported], [None])
        self.assertEqual(self.accounts.owned, [])                      # usuário atual: nada a passar
        self.assertTrue((self.profile / "AppData/Roaming/Notepad++/config.xml").is_file())

    def test_unknown_or_profileless_user_is_refused(self):
        with self.assertRaises(SettingsError) as ctx:
            resolve_destination("ninguem", Backends(self.accounts, None, None))
        self.assertIn("--as-user ninguem", str(ctx.exception))

    def test_tx_refuses_what_is_not_in_the_catalog(self):
        pair = Pair(make_profile(Path(self.tmp.name) / f"src{next(self.counter)}"))
        client = RxClient(pair.accept())
        try:
            with mock.patch("mpm.platforms.appdata", return_value=FakeTxAppdata(self.tx_roots)), \
                    mock.patch("mpm.platforms.regtools", return_value=FakeTxReg()):
                with self.assertRaises(RemoteError):
                    client.request({"op": "export_registry", "keys": [r"Software\Microsoft\Windows"]})
                with self.assertRaises(RemoteError):
                    client.request({"op": "scan_settings", "plugins": ["../../etc"]})
                with self.assertRaises(RemoteError):
                    client.request({"op": "scan_settings", "plugins": []})
        finally:
            client.close()
            pair.finish()

    def test_settings_entries_shape(self):
        from mpm.core.appdata import scan
        results = scan(PLUGINS, self.tx_roots, lambda key: key == r"Software\SimonTatham")
        entries = settings_entries(results)
        self.assertTrue(all(e["kind"] == "config" and e["selected"] for e in entries))
        by_label = {e["label"]: e for e in entries}
        self.assertEqual(by_label["putty"]["version"], "registro")
        self.assertIn("DPAPI", by_label["chrome"]["note"])
        self.assertEqual([e["id"] for e in entries], sorted((e["id"] for e in entries), key=str.lower))



class PickSourceProfileTest(unittest.TestCase):
    PROFILES = [{"username": "Munhos", "sid": "S-1", "path": "C:\\Users\\Munhos", "is_current": False},
                {"username": "master", "sid": "S-2", "path": "C:\\Users\\master", "is_current": True}]

    def pick(self, wanted=None, choose=None, profiles=None):
        client = SimpleNamespace(request=lambda _h: {"profiles": profiles or self.PROFILES})
        return pick_source_profile(client, wanted, choose)

    def test_wanted_by_name_or_sid(self):
        self.assertEqual(self.pick("munhos"), "Munhos")
        self.assertEqual(self.pick("S-2"), "master")

    def test_asks_when_several_and_defaults_to_current(self):
        self.assertEqual(self.pick(choose=lambda profiles: profiles[0]["username"]), "Munhos")
        self.assertEqual(self.pick(), "master")

    def test_errors(self):
        with self.assertRaises(ProfileError):
            self.pick("x")
        with self.assertRaises(ProfileError):
            self.pick(profiles=[dict(p, is_current=False) for p in self.PROFILES])
        self.assertEqual(self.pick(profiles=[dict(self.PROFILES[0])]), "Munhos")


class WindowsRegExportTest(unittest.TestCase):
    def test_other_users_headers_go_back_to_hkcu(self):
        from mpm.platforms.windows import regtools
        raw = ("Windows Registry Editor Version 5.00\r\n\r\n[HKEY_USERS\\S-1-5-21-9\\Software\\SimonTatham]\r\n"
               "\"x\"=\"HKEY_USERS\\\\keep\"\r\n[HKEY_USERS\\S-1-5-21-9\\Software\\SimonTatham\\PuTTY]\r\n").encode("utf-16")

        def fake_run(args, timeout=120):
            Path(args[3]).write_bytes(raw)
            self.assertEqual(args[2], "HKU\\S-1-5-21-9\\Software\\SimonTatham")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch.object(regtools, "_run", fake_run):
            data = regtools.export_key(r"Software\SimonTatham", hive="S-1-5-21-9")
        text = data.decode("utf-16")
        self.assertIn("[HKEY_CURRENT_USER\\Software\\SimonTatham]", text)
        self.assertIn("[HKEY_CURRENT_USER\\Software\\SimonTatham\\PuTTY]", text)
        self.assertNotIn("[HKEY_USERS", text)
        self.assertIn('"x"="HKEY_USERS\\\\keep"', text)                       # valores não são tocados


if __name__ == "__main__":
    unittest.main()
