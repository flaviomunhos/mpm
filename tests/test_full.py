import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
from unittest import mock

from mpm import cli


def run_main(*argv):
    err = io.StringIO()
    with redirect_stderr(err), redirect_stdout(io.StringIO()):
        code = cli.main(list(argv))
    return code, err.getvalue()


class FullValidationTest(unittest.TestCase):
    def test_requires_as_user_name(self):
        code, err = run_main("rx", "--full")
        self.assertEqual(code, 2)
        self.assertIn("--as-user NOME", err)
        self.assertEqual(run_main("rx", "--full", "--as-user")[0], 2)

    def test_does_not_combine(self):
        for extra in (["--settings"], ["--apps"], ["--install"], ["--to-user", "x"], ["--netcheck"]):
            code, err = run_main("rx", "--as-user", "maria", "--full", *extra)
            self.assertEqual(code, 2, extra)

    def test_custom_with_yes_rejected(self):
        self.assertEqual(run_main("rx", "--as-user", "maria", "--full", "custom", "-y")[0], 2)

    def test_pin_versions_allowed_with_full_validation(self):
        # passa da validação (falha só depois, ao escutar/pareamento é evitado por mock)
        with mock.patch("mpm.net.rx.RxListener", side_effect=OSError("porta")):
            code, err = run_main("rx", "--as-user", "maria", "--full", "express", "--pin-versions")
        self.assertEqual(code, 2)
        self.assertIn("porta", err)


class FullOrchestrationTest(unittest.TestCase):
    def args(self, **kw):
        base = dict(full="express", as_user="maria", admin=False, merge=False, dest="/tmp/mpm-full-test",
                    source_user=None, exclude=None, passes=1, dry_run=False, yes=True, retries=0,
                    retry_wait=0.0, ignore_app=None, pin_versions=False)
        base.update(kw)
        return SimpleNamespace(**base)

    def run_full(self, migration_summary, install_code=0, settings_ok=True):
        calls = []
        accounts = SimpleNamespace(is_elevated=lambda: True)
        backends = SimpleNamespace(accounts=accounts)
        migration = SimpleNamespace(run=lambda: calls.append("perfil") or migration_summary)
        with mock.patch("mpm.platforms.accounts", return_value=accounts), \
                mock.patch("mpm.platforms.appdata"), mock.patch("mpm.platforms.regtools"), \
                mock.patch("mpm.platforms.devices"), \
                mock.patch("mpm.net.settings.Backends", return_value=backends), \
                mock.patch("mpm.net.appdata_inventory.pick_source_profile", return_value="Munhos"), \
                mock.patch("mpm.cli._make_migration", return_value=migration) as mk, \
                mock.patch("mpm.net.apps_inventory.run_apps_inventory", side_effect=lambda *a, **k: calls.append("apps") or {}), \
                mock.patch("mpm.cli._install_programs", return_value=install_code), \
                mock.patch("mpm.net.settings.run_settings",
                           side_effect=lambda *a, **k: calls.append(("settings", k["to_user"], k["source_user"], k["mode"]))
                           or {"ok": settings_ok}):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = cli._run_full(self.args(), object(), [])
        return code, calls, mk

    def test_all_three_stages_in_order_with_one_source(self):
        code, calls, mk = self.run_full({"ok": True})
        self.assertEqual(code, 0)
        self.assertEqual(calls, ["perfil", "apps", ("settings", "maria", "Munhos", "express")])
        self.assertEqual(mk.call_args[0][3], "Munhos")

    def test_profile_failure_stops_next_stages(self):
        code, calls, _ = self.run_full({"ok": False})
        self.assertEqual((code, calls), (1, ["perfil"]))

    def test_install_or_settings_failure_sets_exit_code(self):
        self.assertEqual(self.run_full({"ok": True}, install_code=5)[0], 5)
        self.assertEqual(self.run_full({"ok": True}, settings_ok=False)[0], 5)


if __name__ == "__main__":
    unittest.main()
