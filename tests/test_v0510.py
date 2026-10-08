import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mpm import cli
from mpm.core.longpath import PREFIX, long_path
from mpm.core.runlog import RunLog
from mpm.net.apps_install import friendly_error, run_install
from mpm.net.migration import BUSY_PREFIX, Migration, Options, PassResult
from mpm.net.rx import RemoteError
from tests.test_net import FakeClient, quiet


class LongPathTest(unittest.TestCase):
    def test_only_long_windows_paths_get_the_prefix(self):
        short = Path("C:/Users/a/b.txt")
        self.assertEqual(long_path(short, windows=True), short)
        long = Path("C:/Users/" + "x" * 250 + "/f.txt")
        self.assertTrue(str(long_path(long, windows=True)).startswith(PREFIX))
        self.assertEqual(long_path(long, windows=False), long)
        already = Path(PREFIX + "C:/a")
        self.assertEqual(long_path(already, windows=True), already)

    def test_unc_path_uses_unc_prefix(self):
        unc = Path("\\\\srv\\share\\" + "d" * 250)
        self.assertTrue(str(long_path(unc, windows=True)).startswith(PREFIX + "UNC\\"))


class BusyFilesTest(unittest.TestCase):
    def run_copy(self, busy_is_warning):
        def behaviour(sink):
            raise RemoteError("PermissionError: [Errno 13] Permission denied: 'x'")
        entries = [{"folder": "documents", "path": "Cookies", "size": 3, "mtime_ns": 1_700_000_000_000_000_000}]
        with tempfile.TemporaryDirectory() as tmp:
            migration = Migration(FakeClient(behaviour),
                                  Options(dest=Path(tmp), retry_wait=0, busy_is_warning=busy_is_warning),
                                  log=quiet, sleep=lambda _s: None, progress_stream=io.StringIO())
            migration.meta.mkdir(parents=True)
            result = PassResult(label="t")
            migration._copy(entries, result)
        return result

    def test_busy_file_is_warning_in_settings_mode(self):
        result = self.run_copy(True)
        self.assertEqual((result.failed, result.busy_count, result.busy), ([], 1, ["documents/Cookies"]))

    def test_busy_file_is_failure_in_profile_mode(self):
        result = self.run_copy(False)
        self.assertEqual(len(result.failed), 1)
        self.assertIn(BUSY_PREFIX, result.failed[0]["error"])
        self.assertEqual(result.busy_count, 0)


class AdminCheckTest(unittest.TestCase):
    def test_rx_checks_admin_before_listening(self):
        listener = mock.Mock(side_effect=AssertionError("não deveria escutar sem administrador"))
        for argv in (["rx", "--as-user", "maria", "--full"], ["rx", "--as-user", "maria"], ["rx", "--settings"]):
            with mock.patch("mpm.platforms.accounts", return_value=SimpleNamespace(is_elevated=lambda: False)), \
                    mock.patch("mpm.net.rx.RxListener", listener), \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
                self.assertEqual(cli.main(argv + ["--no-log"]), 2, argv)
            self.assertIn("administrador", err.getvalue())
        listener.assert_not_called()

    def test_dry_run_does_not_require_admin(self):
        with mock.patch("mpm.platforms.accounts", return_value=SimpleNamespace(is_elevated=lambda: False)), \
                mock.patch("mpm.net.rx.RxListener", side_effect=OSError("porta")), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
            self.assertEqual(cli.main(["rx", "--settings", "--dry-run", "--no-log"]), 2)
        self.assertIn("porta", err.getvalue())


class InstallSummaryTest(unittest.TestCase):
    class Backend:
        def winget_exists(self, _id):
            return False

        def winget_install(self, pkg_id, version=None):
            return "falhou", "código 0x80190194: 404"

    def test_friendly_error(self):
        self.assertIn("hash", friendly_error("código 0x8A150011: Installer hash does not match"))
        self.assertIn("404", friendly_error("código 0x80190194: x"))
        self.assertEqual(friendly_error("algo novo"), "algo novo")

    def test_manual_list_and_exit_code(self):
        logs: list[str] = []
        entries = [
            {"id": "A.B", "label": "A.B", "version": "", "kind": "winget", "selected": True, "note": ""},
            {"id": "C.D", "label": "Programa C", "version": "", "kind": "sugestão", "selected": True, "note": ""},
        ]
        results = run_install(entries, self.Backend(), log=logs.append)
        self.assertEqual([r["status"] for r in results], ["falhou", "indisponível"])
        text = "\n".join(logs)
        self.assertIn("INSTALE À MÃO", text)
        self.assertIn("Programa C", text)
        self.assertIn("erro 404", text)

    def test_only_unavailable_suggestion_is_not_a_failure(self):
        report = {"groups": {"winget": [], "unmatched": [{"name": "X", "candidate": "X.Y"}]}}
        backend = SimpleNamespace(winget_exists=lambda _i: False)
        fake_platforms = SimpleNamespace(apps=lambda: backend)
        with mock.patch("mpm.platforms.apps", fake_platforms.apps), \
                mock.patch("mpm.net.apps_install.scan_local", return_value=([], [])), \
                mock.patch("mpm.net.apps_install.mark_present", side_effect=lambda e, *a: e), \
                redirect_stdout(io.StringIO()):
            code = cli._install_programs(report, "express", only=None, dest=Path(tempfile.gettempdir()) / "mpm-t",
                                         dry_run=False, pin_versions=False, assume_yes=True)
        self.assertEqual(code, 0)


class ProgressTagTest(unittest.TestCase):
    def test_progress_is_not_logged_as_error(self):
        import os
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"MPM_LOG_DIR": tmp}):
            run = RunLog.start("rx", [])
            run.line("ERR", "  39919/39919 arq.  26.5 GB/26.5 GB  17.1 MB/s  resta ~0s")
            run.line("ERR", "Erro de verdade")
            run.stop(0)
            text = next(Path(tmp).glob("*.log")).read_text(encoding="utf-8")
        self.assertIn("[PROG]", text)
        self.assertNotIn("[ERR]   39919", text)
        self.assertIn("[ERR] Erro de verdade", text)


if __name__ == "__main__":
    unittest.main()


class EveryTargetUsesLongPathTest(unittest.TestCase):
    """Regressão: o SettingsMigration tem o próprio _target e ficou sem o prefixo na 0.5.10."""

    def test_both_migrations_wrap_long_targets(self):
        import inspect

        from mpm.net.migration import Migration
        from mpm.net.settings_migration import SettingsMigration
        for cls in (Migration, SettingsMigration):
            self.assertIn("long_path(", inspect.getsource(cls._target), cls.__name__)
