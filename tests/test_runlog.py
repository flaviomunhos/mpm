import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from mpm import cli
from mpm.core.runlog import KEEP, RunLog, redact


class RedactTest(unittest.TestCase):
    def test_secrets_are_masked(self):
        self.assertEqual(redact("  Código de pareamento : 483920"), "  Código de pareamento : ***")
        self.assertEqual(redact("mpm.exe tx --rx 10.0.0.2 --code 483920"), "mpm.exe tx --rx 10.0.0.2 --code ***")
        self.assertEqual(redact("senha: abc123"), "senha: ***")
        self.assertEqual(redact("<keyMaterial>segredo</keyMaterial>"), "<keyMaterial>***</keyMaterial>")
        self.assertEqual(redact("Copiando 5 arquivos"), "Copiando 5 arquivos")


class RunLogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"MPM_LOG_DIR": self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def files(self):
        return sorted(Path(self.tmp.name).glob("mpm-*.log"))

    def test_tee_writes_timestamped_lines_and_restores_streams(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            before = (sys.stdout, sys.stderr)
            run = RunLog.start("rx", ["rx", "--code", "999999"])
            print("olá mundo")
            print("Código de pareamento : 123456")
            sys.stdout.write("progresso 10%\rprogresso 90%\n")
            print("falhou", file=sys.stderr)
            sys.stdout.write("linha sem quebra")
            run.stop(0)
            self.assertEqual((sys.stdout, sys.stderr), before)
        self.assertIn("olá mundo", out.getvalue())          # a tela continua igual
        text = self.files()[0].read_text(encoding="utf-8")
        self.assertIn("[OUT] olá mundo", text)
        self.assertIn("[ERR] falhou", text)
        self.assertIn("progresso 90%", text)
        self.assertNotIn("progresso 10%", text)
        self.assertIn("linha sem quebra", text)
        self.assertIn("Fim · código de saída: 0", text)
        for secret in ("123456", "999999"):
            self.assertNotIn(secret, text)

    def test_unwritable_dir_does_not_break(self):
        blocker = Path(self.tmp.name) / "arquivo"
        blocker.write_text("x")
        with mock.patch.dict(os.environ, {"MPM_LOG_DIR": str(blocker / "sub")}):
            run = RunLog.start("tx", [])
            self.assertIsNone(run.path)
            run.stop(0)

    def test_keeps_only_recent_logs(self):
        for i in range(KEEP + 5):
            p = Path(self.tmp.name) / f"mpm-rx-old{i:03}.log"
            p.write_text("x")
            os.utime(p, (1000 + i, 1000 + i))
        run = RunLog.start("rx", [])
        run.stop(0)
        self.assertLessEqual(len(self.files()), KEEP)
        self.assertTrue(any("old004" not in f.name for f in self.files()))

    def test_main_logs_rx_validation_error_and_no_log_flag(self):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["rx", "--full"]), 2)
        text = self.files()[0].read_text(encoding="utf-8")
        self.assertIn("--as-user NOME", text)
        self.assertIn("código de saída: 2", text)
        before = len(self.files())
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            cli.main(["rx", "--full", "--no-log"])
        self.assertEqual(len(self.files()), before)

    def test_exception_traceback_is_logged_and_streams_restored(self):
        orig = sys.stdout
        with mock.patch("mpm.cli._parse_hostport", side_effect=RuntimeError("boom")), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            with self.assertRaises(RuntimeError):
                cli.main(["tx", "--rx", "1.2.3.4"])
        self.assertIs(sys.stdout, orig)
        self.assertIn("RuntimeError: boom", self.files()[0].read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
