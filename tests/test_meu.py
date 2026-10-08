import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mpm import cli
from mpm.core import appdata, outlook
from mpm.launcher import choose_command


def reg_bytes(*paths):
    """.reg UTF-16 com os caminhos em valores binários (como o Outlook grava), quebrando linhas com `\\`."""
    lines = ["Windows Registry Editor Version 5.00", "",
             r"[HKEY_CURRENT_USER\Software\Microsoft\Office\16.0\Outlook\Profiles\Outlook\9375CFF0]"]
    for i, p in enumerate(paths):
        raw = (p + "\0").encode("utf-16-le").hex()
        pairs = [raw[j:j + 2] for j in range(0, len(raw), 2)]
        body = ",".join(pairs[:6]) + ",\\\r\n  " + ",".join(pairs[6:])
        lines.append(f'"001f6700{i}"=hex:{body}')
    lines.append('"Account Name"="maria@exemplo.com"')
    return "\r\n".join(lines).encode("utf-16")


class OutlookRegistryTest(unittest.TestCase):
    def test_plugin_carries_profile_keys_and_extras(self):
        plugin = appdata.plugin_by_id("outlook")
        self.assertIn(r"Software\Microsoft\Office\16.0\Outlook", plugin.registry)
        self.assertIn(("appdata", "Microsoft/Templates"), plugin.paths)
        self.assertEqual(appdata.registry_allowlist(["outlook"]), set(outlook.REGISTRY_KEYS))

    def test_finds_pst_paths_in_binary_values(self):
        data = reg_bytes(r"C:\Users\Munhos\Documents\Arquivos do Outlook\a@b.pst", r"D:\Email\antigo.pst")
        self.assertEqual(outlook.find_data_files(data),
                         [r"C:\Users\Munhos\Documents\Arquivos do Outlook\a@b.pst", r"D:\Email\antigo.pst"])

    def test_classification(self):
        files = outlook.classify([r"C:\Users\Munhos\Documents\x.pst", r"D:\Email\y.pst",
                                  r"C:\Users\Munhos\AppData\Local\Microsoft\Outlook\a.ost"], "Munhos", "munhos")
        self.assertEqual([f.status for f in files], ["no-perfil", "fora-do-perfil", "ost"])
        other = outlook.classify([r"C:\Users\Munhos\Documents\x.pst"], "Munhos", "Flavio")
        self.assertEqual(other[0].status, "outro-usuario")

    def test_report_reads_saved_reg_files(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "01.reg").write_bytes(reg_bytes(r"E:\Mail\z.pst"))
            self.assertEqual([f.status for f in outlook.report(Path(d), "Munhos", "Munhos")], ["fora-do-perfil"])

    def test_garbage_does_not_crash(self):
        self.assertEqual(outlook.find_data_files(b"\xff\xfe\x00"), [])


class MeuFlowTest(unittest.TestCase):
    def args(self, **kw):
        base = dict(full="express", meu=True, as_user="maria", admin=False, merge=False, dest="/tmp/mpm-meu-test",
                    source_user=None, exclude=None, passes=1, dry_run=False, yes=False, retries=0,
                    retry_wait=0.0, ignore_app=None, pin_versions=False)
        base.update(kw)
        return SimpleNamespace(**base)

    def test_profile_then_outlook_only_no_apps_no_wifi(self):
        calls, seen = [], {}
        accounts = SimpleNamespace(is_elevated=lambda: True)

        def fake_backends(*a):
            seen["devices"] = a[3]
            return SimpleNamespace(accounts=accounts)

        migration = SimpleNamespace(run=lambda: calls.append("perfil") or {"ok": True})
        with mock.patch("mpm.platforms.accounts", return_value=accounts), mock.patch("mpm.platforms.appdata"), \
                mock.patch("mpm.platforms.regtools"), mock.patch("mpm.platforms.devices"), \
                mock.patch("mpm.net.settings.Backends", side_effect=fake_backends), \
                mock.patch("mpm.net.appdata_inventory.pick_source_profile", return_value="Munhos"), \
                mock.patch("mpm.cli._make_migration", return_value=migration), \
                mock.patch("mpm.net.apps_inventory.run_apps_inventory", side_effect=lambda *a, **k: calls.append("apps")), \
                mock.patch("mpm.cli._install_programs") as inst, \
                mock.patch("mpm.net.settings.run_settings",
                           side_effect=lambda *a, **k: calls.append(("settings", k["only"], k["mode"])) or {"ok": True}):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = cli._run_full(self.args(), object(), [])
        self.assertEqual(code, 0)
        self.assertEqual(calls, ["perfil", ("settings", "^outlook$", "express")])
        inst.assert_not_called()
        self.assertIsNone(seen["devices"])

    def test_no_pause_between_passes(self):
        with mock.patch("mpm.net.migration.Migration") as m, mock.patch("mpm.platforms.accounts"), \
                mock.patch("mpm.core.target.AccountTarget"):
            cli._make_migration(self.args(), object(), [], "Munhos")
        self.assertIsNone(m.call_args.kwargs["pause"])
        with mock.patch("mpm.net.migration.Migration") as m, mock.patch("mpm.platforms.accounts"), \
                mock.patch("mpm.core.target.AccountTarget"):
            cli._make_migration(self.args(meu=False), object(), [], "Munhos")
        self.assertIsNotNone(m.call_args.kwargs["pause"])


class MeuValidationTest(unittest.TestCase):
    def run_main(self, *argv):
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            return cli.main(list(argv)), err.getvalue()

    def test_needs_as_user(self):
        code, err = self.run_main("rx", "--meu")
        self.assertEqual(code, 2)
        self.assertIn("--as-user", err)

    def test_does_not_combine(self):
        for extra in (["--full"], ["--settings"], ["--apps"], ["--only", "x"]):
            self.assertEqual(self.run_main("rx", "--as-user", "maria", "--meu", *extra)[0], 2, extra)


class MeuMenuTest(unittest.TestCase):
    def test_menu_option_5(self):
        answers = iter(["2", "5", "maria", "s"])
        cmd = choose_command(lambda _p: next(answers), lambda _t: None, True)
        self.assertEqual(cmd, ["rx", "--as-user", "maria", "--admin", "--meu"])


if __name__ == "__main__":
    unittest.main()
