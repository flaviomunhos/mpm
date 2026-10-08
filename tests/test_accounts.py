import io
import tempfile
import unittest
from pathlib import Path

from mpm.core.accounts import AccountError, NotSupported, validate_username
from mpm.core.target import AccountTarget
from mpm.net.migration import Migration, MigrationError, Options
from mpm.net.rx import RxClient
from tests.helpers import make_profile
from tests.test_net import Pair, needs_crypto, quiet


class FakeBackend:
    """Backend de contas falso: grava em um diretório temporário em vez de tocar no Windows."""

    def __init__(self, profiles_dir: Path, *, elevated=True, existing=None):
        self.profiles_dir = profiles_dir
        self.elevated = elevated
        self.users = dict(existing or {})          # nome(lower) -> sid
        self.created: list[tuple] = []
        self.profiles_made: list[str] = []
        self.owners: list[tuple] = []
        self.owner_error = None

    def is_elevated(self):
        return self.elevated

    def lookup_user(self, name):
        return self.users.get(name.lower())

    def create_user(self, name, password, admin):
        sid = f"S-1-5-21-1-2-3-{1000 + len(self.users)}"
        self.users[name.lower()] = sid
        self.created.append((name, password, admin))
        return sid

    def predict_profile_path(self, name):
        return self.profiles_dir / name

    def ensure_profile(self, sid, name):
        self.profiles_made.append(name)
        path = self.profiles_dir / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def set_owner(self, path, name):
        self.owners.append((Path(path).name, name))
        return self.owner_error


class ValidateNameTest(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(validate_username("  joao.silva "), "joao.silva")

    def test_invalid(self):
        for bad in ("", "   ", "a" * 21, "ana/b", "ana@x", "ana.", "12345", "Guest", "a\tb"):
            with self.subTest(bad=bad), self.assertRaises(AccountError):
                validate_username(bad)


class AccountTargetTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.passwords: list[str] = []

    def target(self, backend, **kw):
        def get_password(name):
            self.passwords.append(name)
            return "Senha!123"
        opts = dict(name=None, admin=False, merge=False, meta_dir=self.tmp / "meta",
                    get_password=get_password, log=quiet)
        opts.update(kw)
        return AccountTarget(backend, **opts)

    def test_plan_creates_nothing(self):
        be = FakeBackend(self.tmp / "Users")
        roots = self.target(be).plan("joao")
        self.assertEqual(set(roots), {"desktop", "documents", "pictures"})
        self.assertEqual(roots["documents"], self.tmp / "Users" / "joao" / "Documents")
        self.assertEqual((be.created, be.profiles_made, self.passwords), ([], [], []))
        self.assertFalse((self.tmp / "Users").exists())

    def test_commit_creates_user_profile_and_state(self):
        be = FakeBackend(self.tmp / "Users")
        t = self.target(be, name="maria", admin=True)
        t.plan("joao")
        roots = t.commit()
        self.assertEqual(be.created, [("maria", "Senha!123", True)])
        self.assertTrue(all(p.is_dir() for p in roots.values()))
        self.assertTrue((self.tmp / "meta" / "target.json").is_file())

    def test_resume_reuses_user_created_by_mpm(self):
        be = FakeBackend(self.tmp / "Users")
        first = self.target(be)
        first.plan("joao")
        first.commit()
        again = self.target(be)
        again.plan("joao")                      # não exige --merge: o MPM criou este usuário
        again.commit()
        self.assertEqual(len(be.created), 1)
        self.assertEqual(self.passwords, ["joao"])

    def test_existing_user_needs_merge(self):
        be = FakeBackend(self.tmp / "Users", existing={"joao": "S-1-5-21-9"})
        with self.assertRaisesRegex(AccountError, "--merge"):
            self.target(be).plan("joao")
        t = self.target(be, merge=True)
        t.plan("joao")
        t.commit()
        self.assertEqual((be.created, self.passwords), ([], []))     # sem criar nem pedir senha

    def test_requires_elevation_except_dry_run(self):
        be = FakeBackend(self.tmp / "Users", elevated=False)
        with self.assertRaisesRegex(AccountError, "Administrador"):
            self.target(be).plan("joao")
        self.target(be).plan("joao", dry_run=True)

    def test_invalid_name(self):
        with self.assertRaises(AccountError):
            self.target(FakeBackend(self.tmp)).plan("a/b")

    def test_finalize_sets_owner_and_tolerates_failure(self):
        be = FakeBackend(self.tmp / "Users")
        t = self.target(be)
        t.plan("joao")
        t.commit()
        be.owner_error = "acesso negado"
        t.finalize()
        self.assertEqual(sorted(n for n, _ in be.owners), ["Desktop", "Documents", "Pictures"])

    def test_linux_backend_not_supported(self):
        from mpm.platforms.linux import accounts
        with self.assertRaises(NotSupported):
            accounts.create_user("x", "y", False)

    def test_windows_module_imports_without_windows(self):
        from mpm.platforms.windows import accounts
        self.assertEqual(accounts.SID_USERS, "S-1-5-32-545")


@needs_crypto
class AccountMigrationTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.profile = make_profile(self.tmp / "src")

    def run_it(self, backend, *, dry_run=False, **topts):
        meta = self.tmp / "dest"
        target = AccountTarget(backend, name=topts.pop("name", None), admin=False, merge=False,
                               meta_dir=meta / "mpm", get_password=lambda _n: "x", log=quiet)
        pair = Pair(self.profile)
        client = RxClient(pair.accept())
        try:
            return Migration(client, Options(dest=meta, assume_yes=True, dry_run=dry_run,
                                             retry_wait=0, passes=2),
                             log=quiet, target=target, progress_stream=io.StringIO()).run()
        finally:
            client.close()
            pair.finish()

    def test_copies_into_real_profile(self):
        be = FakeBackend(self.tmp / "Users")
        summary = self.run_it(be, name="novo")
        home = self.tmp / "Users" / "novo"
        self.assertTrue(summary["ok"])
        self.assertEqual((home / "Desktop" / "a.txt").read_bytes(), b"x" * 10)
        self.assertEqual((home / "Documents" / "sub" / "b.bin").read_bytes(), b"y" * 100)
        self.assertEqual((home / "Pictures" / "d.jpg").read_bytes(), b"p" * 1000)
        self.assertFalse((home / "Music").exists())                       # fora do escopo
        self.assertFalse((self.tmp / "dest" / "files").exists())          # nada de pasta files/
        self.assertTrue((self.tmp / "dest" / "mpm" / "verified.jsonl").is_file())
        self.assertEqual(summary["target_user"], "novo")
        self.assertEqual(len(be.created), 1)
        self.assertEqual(len(be.owners), 3)                               # finalize rodou
        self.assertEqual(list(home.rglob("*.mpm-part")), [])

    def test_default_name_is_source_profile(self):
        be = FakeBackend(self.tmp / "Users")
        self.run_it(be)
        self.assertTrue((self.tmp / "Users" / "tester" / "Desktop" / "a.txt").is_file())

    def test_dry_run_creates_nothing(self):
        be = FakeBackend(self.tmp / "Users")
        summary = self.run_it(be, dry_run=True)
        self.assertTrue(summary["dry_run"])
        self.assertEqual((be.created, be.profiles_made, be.owners), ([], [], []))
        self.assertFalse((self.tmp / "Users").exists())

    def test_existing_user_without_merge_aborts_before_copy(self):
        be = FakeBackend(self.tmp / "Users", existing={"tester": "S-1-5-21-7"})
        with self.assertRaises(MigrationError):
            self.run_it(be)
        self.assertEqual((be.created, be.profiles_made), ([], []))

    def test_second_run_resumes_and_skips_everything(self):
        be = FakeBackend(self.tmp / "Users")
        self.run_it(be, name="novo")
        summary = self.run_it(be, name="novo")
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["passes"][0]["copied"], 0)
        self.assertEqual(len(be.created), 1)


if __name__ == "__main__":
    unittest.main()


@needs_crypto
class FileAttributesTest(unittest.TestCase):
    """0.3.1: atributos Oculto/Sistema/Somente leitura (simulados: o sandbox não é Windows)."""

    def run_copy(self, attrs):
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            profile = make_profile(Path(tmp) / "src")
            pair = Pair(profile)
            client = RxClient(pair.accept())
            try:
                with mock.patch("mpm.net.tx._file_attrs", return_value=attrs), \
                     mock.patch("mpm.net.migration._set_attrs") as set_attrs, \
                     mock.patch("mpm.net.migration._clear_protection") as clear:
                    Migration(client, Options(dest=Path(tmp) / "dest", assume_yes=True, passes=1,
                                              retry_wait=0),
                              log=quiet, progress_stream=io.StringIO()).run()
                return set_attrs.call_args_list, clear.call_count
            finally:
                client.close()
                pair.finish()

    def test_attrs_travel_and_are_applied_after_copy(self):
        calls, cleared = self.run_copy(0x6)           # oculto + sistema
        self.assertEqual(len(calls), 4)               # os 4 arquivos do perfil de teste
        self.assertTrue(all(c.args[1] == 6 for c in calls))
        self.assertEqual(cleared, 4)

    def test_no_attrs_sends_zero(self):
        calls, _ = self.run_copy(0)
        self.assertTrue(all(c.args[1] == 0 for c in calls))

    def test_file_attrs_is_zero_without_windows_stat(self):
        import os
        from mpm.net import tx
        self.assertEqual(tx._file_attrs(os.stat(".")), 0)       # Linux: st_file_attributes não existe


@needs_crypto
class DirectoryRaceTest(unittest.TestCase):
    """0.3.2: erro transitório ao criar a pasta de destino é refeito, não vira falha."""

    def test_transient_mkdir_error_is_retried(self):
        from unittest import mock
        original = Path.mkdir
        state = {"n": 0}

        def flaky(self, *a, **k):
            if self.name == "sub" and state["n"] < 1:
                state["n"] += 1
                raise PermissionError(13, "acesso negado")
            return original(self, *a, **k)

        with tempfile.TemporaryDirectory() as tmp:
            profile = make_profile(Path(tmp) / "src")
            pair = Pair(profile)
            client = RxClient(pair.accept())
            try:
                with mock.patch.object(Path, "mkdir", flaky):
                    summary = Migration(client, Options(dest=Path(tmp) / "dest", assume_yes=True,
                                                        passes=1, retry_wait=0),
                                        log=quiet, progress_stream=io.StringIO()).run()
            finally:
                client.close()
                pair.finish()
        first = summary["passes"][0]
        self.assertEqual(first["failed"], [])
        self.assertGreaterEqual(first["retries_used"], 1)
        self.assertEqual(state["n"], 1)


class LexicalPathTest(unittest.TestCase):
    """0.3.3: o RX não pode depender de realpath (corrida com criação de pastas)."""

    def test_lexical_ignores_realpath_results(self):
        from unittest import mock
        from mpm.net.safepath import resolve_in_root
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # realpath "instável": simula o resultado divergente visto no Windows
            with mock.patch("os.path.realpath", side_effect=lambda p: "/outro/lugar" if "b.txt" in str(p) else str(p)):
                with self.assertRaises(ValueError):
                    resolve_in_root(root, "a/b.txt")                       # modo antigo falha
                self.assertEqual(resolve_in_root(root, "a/b.txt", lexical=True),
                                 root / "a" / "b.txt")                     # modo lexical não

    def test_lexical_still_rejects_hostile_paths(self):
        from mpm.net.safepath import resolve_in_root
        root = Path("/tmp/x")
        for bad in ("../x", "a/../b", "/abs", "a//b", "a/./b", "", "a/\x00b"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                resolve_in_root(root, bad, lexical=True)

    def test_lexical_rejects_symlink_component(self):
        import os
        from mpm.net.safepath import resolve_in_root
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            outside = Path(tmp) / "fora"
            root.mkdir()
            outside.mkdir()
            os.symlink(outside, root / "atalho")
            with self.assertRaises(ValueError):
                resolve_in_root(root, "atalho/arquivo.txt", lexical=True)
