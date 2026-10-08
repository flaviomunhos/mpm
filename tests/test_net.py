import hashlib
import io
import json
import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from mpm.net.migration import Migration, MigrationError, Options
from mpm.net.rx import RemoteError, RxClient, RxListener, close_workers, open_workers
from mpm.net.safepath import resolve_in_root
from mpm.net.security import AuthError, auth_mac, new_pairing_code, normalize_code
from mpm.net.tx import run_tx

from tests.helpers import make_profile

try:
    import cryptography  # noqa: F401
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False

needs_crypto = unittest.skipUnless(HAS_CRYPTO, "pacote 'cryptography' não instalado")
quiet = lambda _msg: None  # noqa: E731


class Pair:
    """RX (thread principal) e TX (thread) conversando por loopback."""

    def __init__(self, profile, code=None):
        self.listener = RxListener(host="127.0.0.1", port=0)
        self.tx_error = None
        use_code = code or self.listener.code

        def tx():
            try:
                run_tx("127.0.0.1", self.listener.port, use_code, profile=profile, log=quiet)
            except Exception as exc:     # noqa: BLE001 - capturado para o teste inspecionar
                self.tx_error = exc

        self.thread = threading.Thread(target=tx, daemon=True)
        self.thread.start()

    def accept(self, **kwargs):
        return self.listener.accept_authenticated(timeout=10, log=quiet, **kwargs)

    def finish(self):
        self.listener.close()
        self.thread.join(10)


def run_migration(profile, dest, *, pause=None, threads=1, **opts):
    pair = Pair(profile)
    client = RxClient(pair.accept())
    workers = []
    try:
        if threads > 1:
            workers = open_workers(client, pair.listener, threads, timeout=10, log=quiet)
        opts.setdefault("retry_wait", 0)
        options = Options(dest=Path(dest), assume_yes=True, **opts)
        return Migration(client, options, log=quiet, pause=pause, workers=workers,
                         progress_stream=io.StringIO()).run()
    finally:
        close_workers(workers)
        client.close()
        pair.finish()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class CodeHelpersTest(unittest.TestCase):
    def test_code_format(self):
        self.assertRegex(new_pairing_code(), r"^[A-HJKMNP-Z2-9]{4}-[A-HJKMNP-Z2-9]{4}$")

    def test_normalize(self):
        self.assertEqual(normalize_code(" abcd-efgh "), "ABCDEFGH")

    def test_mac_depends_on_fingerprint(self):
        a = auth_mac("ABCD-EFGH", b"RX", "aa" * 32, b"1" * 16, b"2" * 16)
        b = auth_mac("ABCD-EFGH", b"RX", "bb" * 32, b"1" * 16, b"2" * 16)
        c = auth_mac("abcd efgh", b"RX", "aa" * 32, b"1" * 16, b"2" * 16)
        self.assertNotEqual(a, b)       # certificado diferente (MITM) => MAC diferente
        self.assertEqual(a, c)          # normalização do código


class SafePathTest(unittest.TestCase):
    def test_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(resolve_in_root(root, "a/b.txt"), root / "a" / "b.txt")

    def test_rejects_escapes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for bad in ("..", "a/../b", "../x", "/etc/passwd", "", "a//b", ".", "a/./b", "a\x00b"):
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    resolve_in_root(root, bad)

    def test_rejects_symlink_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, outside = Path(tmp) / "root", Path(tmp) / "out"
            root.mkdir()
            outside.mkdir()
            (outside / "secret").write_text("x")
            try:
                os.symlink(outside, root / "link", target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("sem permissão para criar symlink")
            with self.assertRaises(ValueError):
                resolve_in_root(root, "link/secret")


@needs_crypto
class PairingTest(unittest.TestCase):
    def test_pairing_and_info(self):
        with tempfile.TemporaryDirectory() as tmp:
            pair = Pair(make_profile(Path(tmp)))
            client = RxClient(pair.accept())
            info = client.request({"op": "info"})
            client.close()
            pair.finish()
        self.assertEqual(info["profiles"][0]["username"], "tester")
        self.assertIsNone(pair.tx_error)

    def test_tcp_nodelay_is_enabled(self):
        # Sem TCP_NODELAY, cada arquivo pequeno custa ~40 ms (Nagle + ACK atrasado): 23 arq/s medidos.
        with tempfile.TemporaryDirectory() as tmp:
            pair = Pair(make_profile(Path(tmp)))
            conn = pair.accept()
            nodelay = conn.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY)
            RxClient(conn).close()
            pair.finish()
        self.assertTrue(nodelay)

    def test_wrong_code_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            pair = Pair(make_profile(Path(tmp)), code="AAAA-BBBB")
            with self.assertRaises(AuthError):
                pair.accept(max_failures=1)
            pair.finish()
        self.assertIsNotNone(pair.tx_error)


@needs_crypto
class ServerSafetyTest(unittest.TestCase):
    def _session(self, tmp):
        pair = Pair(make_profile(Path(tmp)))
        return pair, RxClient(pair.accept())

    def test_fetch_rejects_hostile_requests(self):
        with tempfile.TemporaryDirectory() as tmp:
            pair, client = self._session(tmp)
            try:
                with self.assertRaises(RemoteError):    # antes do scan não há pastas liberadas
                    client.stream({"op": "fetch", "folder": "desktop", "path": "a.txt"}, lambda b: None)
                client.request({"op": "scan", "user": None})
                for folder, path in [("documents", "../Desktop/a.txt"), ("documents", "/etc/passwd"),
                                     ("music", "fora.mp3"), ("downloads", "baixado.zip"),
                                     ("desktop", "nao-existe.txt")]:
                    with self.subTest(folder=folder, path=path), self.assertRaises(RemoteError):
                        client.stream({"op": "fetch", "folder": folder, "path": path}, lambda b: None)
                got = bytearray()
                end = client.stream({"op": "fetch", "folder": "desktop", "path": "a.txt"}, got.extend)
                self.assertEqual(bytes(got), b"x" * 10)     # a sessão continua utilizável
                self.assertFalse(end["changed"])
            finally:
                client.close()
                pair.finish()


@needs_crypto
class TransferTest(unittest.TestCase):
    def test_full_transfer(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = make_profile(Path(tmp) / "src")
            dest = Path(tmp) / "dest"
            summary = run_migration(profile, dest, passes=1)

            self.assertTrue(summary["ok"])
            self.assertEqual(summary["passes"][0]["copied"], 4)
            files = dest / "files"
            self.assertEqual((files / "desktop" / "a.txt").read_bytes(), b"x" * 10)
            self.assertEqual((files / "documents" / "sub" / "b.bin").read_bytes(), b"y" * 100)
            self.assertEqual((files / "pictures" / "d.jpg").read_bytes(), b"p" * 1000)
            self.assertFalse((files / "downloads").exists())      # fora do escopo
            self.assertFalse((files / "music").exists())
            self.assertEqual(list(dest.rglob("*.mpm-part")), [])

            src_a = Path(profile.path) / "Desktop" / "a.txt"
            self.assertAlmostEqual((files / "desktop" / "a.txt").stat().st_mtime,
                                   src_a.stat().st_mtime, delta=2)

            lines = [json.loads(l) for l in
                     (dest / "mpm" / "verified.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(lines), 4)
            for line in lines:
                local = files / line["folder"] / Path(*line["path"].split("/"))
                self.assertEqual(line["sha256"], sha(local))
            report = json.loads((dest / "mpm" / "transfer-report.json").read_text(encoding="utf-8"))
            self.assertTrue(report["ok"])
            self.assertTrue((dest / "mpm" / "manifest.json").is_file())

    def test_second_run_resumes_without_recopying(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = make_profile(Path(tmp) / "src")
            dest = Path(tmp) / "dest"
            run_migration(profile, dest, passes=1)
            again = run_migration(profile, dest, passes=1)
        self.assertEqual(again["passes"][0]["copied"], 0)
        self.assertEqual(again["passes"][0]["skipped"], 4)
        self.assertTrue(again["ok"])

    def test_delta_pass_picks_up_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = make_profile(Path(tmp) / "src")
            home = Path(profile.path)
            dest = Path(tmp) / "dest"

            def change_between_passes(number):
                (home / "Desktop" / "a.txt").write_bytes(b"novo conteudo maior")
                (home / "Pictures" / "novo.png").write_bytes(b"png")

            summary = run_migration(profile, dest, passes=2, pause=change_between_passes)
            files = dest / "files"
            self.assertEqual((files / "desktop" / "a.txt").read_bytes(), b"novo conteudo maior")
            self.assertEqual((files / "pictures" / "novo.png").read_bytes(), b"png")
        self.assertEqual(summary["passes"][0]["copied"], 4)
        self.assertEqual(summary["passes"][1]["copied"], 2)
        self.assertEqual(summary["passes"][1]["skipped"], 3)
        self.assertTrue(summary["ok"])

    def test_exclude_patterns(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = make_profile(Path(tmp) / "src")
            dest = Path(tmp) / "dest"
            summary = run_migration(profile, dest, passes=1, excludes=["sub", "*.JPG"])
            files = dest / "files"
            self.assertFalse((files / "documents" / "sub").exists())
            self.assertFalse((files / "pictures" / "d.jpg").exists())
            self.assertTrue((files / "desktop" / "a.txt").exists())
        self.assertEqual(summary["passes"][0]["excluded"], 2)
        self.assertEqual(summary["passes"][0]["copied"], 2)

    def test_dry_run_copies_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = make_profile(Path(tmp) / "src")
            dest = Path(tmp) / "dest"
            summary = run_migration(profile, dest, passes=2, dry_run=True)
            copied = [p for p in (dest / "files").rglob("*") if p.is_file()]
            has_report = (dest / "mpm" / "transfer-report.json").exists()
        self.assertTrue(summary["dry_run"])
        self.assertEqual(summary["to_copy"], 4)
        self.assertEqual(summary["bytes_to_copy"], 10 + 101 + 1000)
        self.assertEqual(copied, [])
        self.assertFalse(has_report)

    def test_insufficient_space_aborts_before_copying(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = make_profile(Path(tmp) / "src")
            dest = Path(tmp) / "dest"
            with mock.patch("mpm.net.migration.shutil.disk_usage",
                            return_value=mock.Mock(free=10)):
                with self.assertRaises(MigrationError):
                    run_migration(profile, dest, passes=1)
            copied = [p for p in (dest / "files").rglob("*") if p.is_file()]
        self.assertEqual(copied, [])

    def test_unknown_source_user(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = make_profile(Path(tmp) / "src")
            with self.assertRaises(MigrationError):
                run_migration(profile, Path(tmp) / "dest", source_user="ninguem")


class FakeClient:
    """Cliente falso para testar a lógica de cópia sem rede."""

    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.calls = 0
        self.headers = []

    def stream(self, header, sink):
        self.calls += 1
        self.headers.append(dict(header))
        return self.behaviour(sink)


def _end(data, **extra):
    end = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
           "mtime_ns": 1, "changed": False, "offset": 0}
    end.update(extra)
    return end


class CopyOneTest(unittest.TestCase):
    ENTRY = {"folder": "desktop", "path": "a.txt", "size": 5, "mtime_ns": 1_700_000_000_000_000_000}

    def _migration(self, tmp, behaviour, **opts):
        client = FakeClient(behaviour)
        opts.setdefault("retry_wait", 0)
        migration = Migration(client, Options(dest=Path(tmp), **opts), log=quiet,
                              sleep=lambda _s: None)
        return migration, client

    def test_changed_during_copy_is_discarded(self):
        def behaviour(sink):
            sink(b"abcde")
            return _end(b"abcde", changed=True)
        with tempfile.TemporaryDirectory() as tmp:
            migration, client = self._migration(tmp, behaviour)
            status, _detail = migration._copy_one(client, self.ENTRY)
            leftovers = list(Path(tmp).rglob("a.txt*"))
        self.assertEqual(status, "changed")
        self.assertEqual(client.calls, 1)           # mudou: fica para a próxima passada, sem insistir
        self.assertEqual(leftovers, [])

    def test_hash_mismatch_retries_then_fails(self):
        def behaviour(sink):
            sink(b"abcde")
            return _end(b"abcde", sha256="0" * 64)
        with tempfile.TemporaryDirectory() as tmp:
            migration, client = self._migration(tmp, behaviour)
            status, detail = migration._copy_one(client, self.ENTRY)
            leftovers = list(Path(tmp).rglob("a.txt*"))
        self.assertEqual(status, "failed")
        self.assertIn("hash", detail)
        self.assertEqual(client.calls, 3)
        self.assertEqual(leftovers, [])

    def test_remote_error_is_retried_then_reported(self):
        def behaviour(sink):
            raise RemoteError("PermissionError: negado")
        with tempfile.TemporaryDirectory() as tmp:
            migration, client = self._migration(tmp, behaviour)
            status, detail = migration._copy_one(client, self.ENTRY)
        self.assertEqual(status, "failed")
        self.assertIn("negado", detail)
        self.assertEqual(client.calls, 3)           # 1 + 2 tentativas extras (como /R:2)

    def test_retries_zero_means_a_single_attempt(self):
        def behaviour(sink):
            raise RemoteError("negado")
        with tempfile.TemporaryDirectory() as tmp:
            migration, client = self._migration(tmp, behaviour, retries=0)
            migration._copy_one(client, self.ENTRY)
        self.assertEqual(client.calls, 1)

    def test_transient_error_then_success_waits_between_attempts(self):
        outcomes = [RemoteError("em uso"), None]

        def behaviour(sink):
            outcome = outcomes.pop(0)
            if outcome:
                raise outcome
            sink(b"abcde")
            return _end(b"abcde")
        waits = []
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient(behaviour)
            migration = Migration(client, Options(dest=Path(tmp), retry_wait=1.5), log=quiet,
                                  sleep=waits.append)
            status, detail = migration._copy_one(client, self.ENTRY)
            content = (Path(tmp) / "files" / "desktop" / "a.txt").read_bytes()
        self.assertEqual(status, "copied")
        self.assertEqual(content, b"abcde")
        self.assertEqual(waits, [1.5])
        self.assertEqual(detail["sha256"], hashlib.sha256(b"abcde").hexdigest())

    def test_hostile_inventory_entries_never_write_outside_dest(self):
        with tempfile.TemporaryDirectory() as tmp:
            migration, client = self._migration(tmp, lambda sink: {})
            for entry in ({"folder": "desktop", "path": "../../evil", "size": 1, "mtime_ns": 1},
                          {"folder": "evil", "path": "x", "size": 1, "mtime_ns": 1},
                          {"folder": "desktop", "path": "/abs", "size": 1, "mtime_ns": 1}):
                status, _ = migration._copy_one(client, entry)
                self.assertEqual(status, "failed")
            self.assertEqual(client.calls, 0)


class PartialResumeTest(unittest.TestCase):
    """Retomada no meio do arquivo (como o /Z do Robocopy)."""

    ENTRY = CopyOneTest.ENTRY

    def _setup(self, tmp, part_bytes, behaviour):
        client = FakeClient(behaviour)
        migration = Migration(client, Options(dest=Path(tmp), retry_wait=0), log=quiet,
                              sleep=lambda _s: None)
        target_dir = Path(tmp) / "files" / "desktop"
        target_dir.mkdir(parents=True)
        part = target_dir / "a.txt.mpm-part"
        if part_bytes is not None:
            part.write_bytes(part_bytes)
        return migration, client, target_dir

    def test_resumes_from_existing_prefix(self):
        def behaviour(sink):
            sink(b"de")                              # o TX só reenvia o que falta
            return _end(b"abcde", offset=3)
        with tempfile.TemporaryDirectory() as tmp:
            migration, client, folder = self._setup(tmp, b"abc", behaviour)
            status, _ = migration._copy_one(client, self.ENTRY)
            content = (folder / "a.txt").read_bytes()
            part_left = (folder / "a.txt.mpm-part").exists()
        self.assertEqual(status, "copied")
        self.assertEqual(content, b"abcde")
        self.assertFalse(part_left)
        self.assertEqual(client.headers[0]["offset"], 3)
        self.assertEqual(migration._stats["resumed"], 1)
        self.assertEqual(migration._stats["resumed_bytes"], 3)

    def test_corrupt_prefix_is_caught_and_the_file_restarts_from_zero(self):
        def behaviour(sink):
            if "offset" in client.headers[-1]:
                sink(b"de")
                return _end(b"abcde", offset=client.headers[-1]["offset"])
            sink(b"abcde")
            return _end(b"abcde")
        with tempfile.TemporaryDirectory() as tmp:
            migration, client, folder = self._setup(tmp, b"xyz", behaviour)
            status, _ = migration._copy_one(client, self.ENTRY)
            content = (folder / "a.txt").read_bytes()
        self.assertEqual(status, "copied")
        self.assertEqual(content, b"abcde")           # nunca fica o prefixo estragado
        self.assertEqual(client.calls, 2)
        self.assertNotIn("offset", client.headers[1])

    def test_part_larger_than_the_file_starts_over(self):
        def behaviour(sink):
            sink(b"abcde")
            return _end(b"abcde")
        with tempfile.TemporaryDirectory() as tmp:
            migration, client, folder = self._setup(tmp, b"abcdefghij", behaviour)
            status, _ = migration._copy_one(client, self.ENTRY)
            content = (folder / "a.txt").read_bytes()
        self.assertEqual(status, "copied")
        self.assertEqual(content, b"abcde")
        self.assertNotIn("offset", client.headers[0])

    def test_complete_part_only_needs_the_final_check(self):
        def behaviour(sink):
            return _end(b"abcde", offset=5)          # nada a transferir
        with tempfile.TemporaryDirectory() as tmp:
            migration, client, folder = self._setup(tmp, b"abcde", behaviour)
            status, _ = migration._copy_one(client, self.ENTRY)
            content = (folder / "a.txt").read_bytes()
        self.assertEqual(status, "copied")
        self.assertEqual(content, b"abcde")
        self.assertEqual(client.headers[0]["offset"], 5)

    def test_lost_connection_keeps_the_partial_file(self):
        def behaviour(sink):
            sink(b"ab")
            raise ConnectionError("conexão encerrada")
        with tempfile.TemporaryDirectory() as tmp:
            migration, client, folder = self._setup(tmp, None, behaviour)
            with self.assertRaises(ConnectionError):
                migration._copy_one(client, self.ENTRY)
            partial = (folder / "a.txt.mpm-part").read_bytes()
        self.assertEqual(partial, b"ab")


class ParallelCopyTest(unittest.TestCase):
    """Várias conexões; uma que cai não perde arquivos."""

    def _migration(self, tmp, clients):
        migration = Migration(clients[0], Options(dest=Path(tmp), retry_wait=0), log=quiet,
                              workers=clients, sleep=lambda _s: None,
                              progress_stream=io.StringIO())
        migration.meta.mkdir(parents=True)
        return migration

    @staticmethod
    def _entries(n):
        return [{"folder": "documents", "path": f"f{i}.txt", "size": 3,
                 "mtime_ns": 1_700_000_000_000_000_000} for i in range(n)]

    @staticmethod
    def _good(sink):
        sink(b"abc")
        return _end(b"abc")

    @staticmethod
    def _dead(sink):
        raise ConnectionError("caiu")

    def test_all_files_are_copied_across_connections(self):
        from mpm.net.migration import PassResult
        clients = [FakeClient(self._good) for _ in range(4)]
        with tempfile.TemporaryDirectory() as tmp:
            migration = self._migration(tmp, clients)
            result = PassResult(label="t")
            migration._copy(self._entries(40), result)
            written = sorted(p.name for p in (Path(tmp) / "files" / "documents").iterdir())
            verified = (Path(tmp) / "mpm" / "verified.jsonl").read_text().splitlines()
        self.assertEqual(result.copied, 40)
        self.assertEqual(result.threads, 4)
        self.assertEqual(len(written), 40)
        self.assertEqual(len(verified), 40)
        self.assertEqual(sum(c.calls for c in clients), 40)
        self.assertGreater(min(c.calls for c in clients), 0)    # o trabalho foi dividido

    def test_a_dead_connection_hands_its_file_to_the_others(self):
        from mpm.net.migration import PassResult
        clients = [FakeClient(self._dead), FakeClient(self._good), FakeClient(self._good)]
        with tempfile.TemporaryDirectory() as tmp:
            migration = self._migration(tmp, clients)
            result = PassResult(label="t")
            migration._copy(self._entries(12), result)
        self.assertEqual(result.copied, 12)
        self.assertEqual(result.failed, [])

    def test_all_connections_dead_aborts_with_a_clear_error(self):
        from mpm.net.migration import PassResult
        clients = [FakeClient(self._dead), FakeClient(self._dead)]
        with tempfile.TemporaryDirectory() as tmp:
            migration = self._migration(tmp, clients)
            with self.assertRaises(MigrationError) as ctx:
                migration._copy(self._entries(5), PassResult(label="t"))
        self.assertIn("conexões", str(ctx.exception))


@needs_crypto
class ParallelTransferTest(unittest.TestCase):
    def _big_profile(self, tmp):
        profile = make_profile(Path(tmp) / "src")
        docs = Path(profile.path) / "Documents" / "muitos"
        docs.mkdir()
        for i in range(60):
            (docs / f"arq{i:03}.dat").write_bytes(bytes([i]) * (i * 997 + 1))
        (docs / "grande.bin").write_bytes(os.urandom(3 * 1024 * 1024 + 123))
        return profile

    def test_parallel_copy_is_identical_to_the_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = self._big_profile(tmp)
            dest = Path(tmp) / "dest"
            summary = run_migration(profile, dest, passes=1, threads=4)
            src, out = Path(profile.path) / "Documents", dest / "files" / "documents"
            self.assertTrue(summary["ok"])
            self.assertEqual(summary["passes"][0]["threads"], 4)
            self.assertEqual(summary["passes"][0]["copied"], 4 + 61)
            for f in src.rglob("*"):
                if f.is_file():
                    self.assertEqual(sha(f), sha(out / f.relative_to(src)))
            self.assertEqual(list(dest.rglob("*.mpm-part")), [])
            lines = (dest / "mpm" / "verified.jsonl").read_text().splitlines()
            self.assertEqual(len(lines), 4 + 61)

    def test_second_parallel_run_skips_everything(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = self._big_profile(tmp)
            dest = Path(tmp) / "dest"
            run_migration(profile, dest, passes=1, threads=3)
            again = run_migration(profile, dest, passes=1, threads=3)
        self.assertEqual(again["passes"][0]["copied"], 0)
        self.assertEqual(again["passes"][0]["skipped"], 4 + 61)

    def test_resume_from_a_real_partial_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = make_profile(Path(tmp) / "src")
            dest = Path(tmp) / "dest"
            folder = dest / "files" / "documents" / "sub"
            folder.mkdir(parents=True)
            (folder / "b.bin.mpm-part").write_bytes(b"y" * 40)        # b.bin tem 100 bytes de "y"
            summary = run_migration(profile, dest, passes=1, threads=2)
            content = (folder / "b.bin").read_bytes()
        first = summary["passes"][0]
        self.assertEqual(content, b"y" * 100)
        self.assertEqual(first["resumed_files"], 1)
        self.assertEqual(first["resumed_bytes"], 40)
        self.assertEqual(first["bytes_transferred"], 10 + 100 - 40 + 1 + 1000)

    def test_corrupt_real_partial_file_is_replaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = make_profile(Path(tmp) / "src")
            dest = Path(tmp) / "dest"
            folder = dest / "files" / "documents" / "sub"
            folder.mkdir(parents=True)
            (folder / "b.bin.mpm-part").write_bytes(b"LIXO" * 10)
            summary = run_migration(profile, dest, passes=1, threads=2)
            content = (folder / "b.bin").read_bytes()
        self.assertEqual(content, b"y" * 100)
        self.assertTrue(summary["ok"])
        self.assertGreaterEqual(summary["passes"][0]["retries_used"], 1)

    def test_report_has_throughput_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = self._big_profile(tmp)
            summary = run_migration(profile, Path(tmp) / "dest", passes=1, threads=4)
        first = summary["passes"][0]
        self.assertGreater(first["files_per_second"], 0)
        self.assertGreater(first["bytes_per_second"], 0)
        self.assertGreater(first["small_files"], 0)


@needs_crypto
class WorkerSecurityTest(unittest.TestCase):
    def _session(self, tmp):
        pair = Pair(make_profile(Path(tmp)))
        return pair, RxClient(pair.accept())

    def test_workers_are_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            pair, client = self._session(tmp)
            workers = []
            try:
                workers = open_workers(client, pair.listener, 2, timeout=10, log=quiet)
                client.request({"op": "scan", "user": None})
                for op in ("scan", "info", "get_inventory", "open_workers"):
                    with self.subTest(op=op), self.assertRaises(RemoteError):
                        workers[0].request({"op": op})
                got = bytearray()
                end = workers[1].stream({"op": "fetch", "folder": "desktop", "path": "a.txt"}, got.extend)
                self.assertEqual(bytes(got), b"x" * 10)         # fetch funciona nas conexões extras
                self.assertFalse(end["changed"])
                self.assertTrue(workers[0].request({"op": "ping", "t": 1})["ok"])
            finally:
                close_workers(workers)
                client.close()
                pair.finish()

    def test_pairing_code_does_not_open_a_worker_connection(self):
        with tempfile.TemporaryDirectory() as tmp:
            pair, client = self._session(tmp)
            try:
                secret = "a1" * 16
                errors = []

                def intruder():
                    try:        # alguém que só conhece o código digitado, não o segredo da sessão
                        run_tx("127.0.0.1", pair.listener.port, pair.listener.code,
                               profile=make_profile(Path(tmp) / "x"), log=quiet)
                    except Exception as exc:     # noqa: BLE001
                        errors.append(exc)

                t = threading.Thread(target=intruder, daemon=True)
                t.start()
                with self.assertRaises(AuthError):
                    pair.listener.accept_authenticated(code=secret, max_failures=1,
                                                       timeout=10, log=quiet)
                t.join(10)
                self.assertTrue(errors)
            finally:
                client.close()
                pair.finish()

    def test_open_workers_rejects_bad_parameters(self):
        with tempfile.TemporaryDirectory() as tmp:
            pair, client = self._session(tmp)
            try:
                for count, secret in [(0, "a" * 32), (99, "a" * 32), (True, "a" * 32),
                                      (2, "curto"), (2, "a b" * 8), (2, 123), ("2", "a" * 32)]:
                    with self.subTest(count=count, secret=secret), self.assertRaises(RemoteError):
                        client.request({"op": "open_workers", "count": count, "secret": secret})
            finally:
                client.close()
                pair.finish()

    def test_fetch_offset_validation_and_whole_file_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            pair, client = self._session(tmp)
            try:
                client.request({"op": "scan", "user": None})
                base = {"op": "fetch", "folder": "documents", "path": "sub/b.bin"}      # 100 bytes de "y"
                for bad in (-1, 101, "3", True, 1.5):
                    with self.subTest(offset=bad), self.assertRaises(RemoteError):
                        client.stream({**base, "offset": bad}, lambda b: None)
                got = bytearray()
                end = client.stream({**base, "offset": 30}, got.extend)
                self.assertEqual(bytes(got), b"y" * 70)                  # só o que falta
                self.assertEqual(end["offset"], 30)
                self.assertEqual(end["size"], 100)                       # tamanho total do arquivo
                self.assertEqual(end["sha256"], hashlib.sha256(b"y" * 100).hexdigest())
                got = bytearray()
                end = client.stream({**base, "offset": 100}, got.extend)
                self.assertEqual(bytes(got), b"")
                self.assertEqual(end["sha256"], hashlib.sha256(b"y" * 100).hexdigest())
            finally:
                client.close()
                pair.finish()

    def test_bench_and_ping(self):
        with tempfile.TemporaryDirectory() as tmp:
            pair, client = self._session(tmp)
            try:
                self.assertTrue(client.request({"op": "ping", "t": 7})["ok"])
                got = []
                end = client.stream({"op": "bench", "size": 3 * 1024 * 1024 + 5},
                                    lambda b: got.append(len(b)))
                self.assertEqual(sum(got), 3 * 1024 * 1024 + 5)
                self.assertEqual(end["size"], 3 * 1024 * 1024 + 5)
                for bad in (0, -1, 10 ** 12, "9", True):
                    with self.subTest(size=bad), self.assertRaises(RemoteError):
                        client.stream({"op": "bench", "size": bad}, lambda b: None)
            finally:
                client.close()
                pair.finish()


@needs_crypto
class NetcheckTest(unittest.TestCase):
    def test_netcheck_reports_latency_and_throughput(self):
        from mpm.net.netcheck import run_netcheck
        lines = []
        with tempfile.TemporaryDirectory() as tmp:
            pair = Pair(make_profile(Path(tmp)))
            client = RxClient(pair.accept())
            workers = open_workers(client, pair.listener, 3, timeout=10, log=quiet)
            try:
                result = run_netcheck(client, workers, pings=5, size=3 * 1024 * 1024, log=lines.append)
            finally:
                close_workers(workers)
                client.close()
                pair.finish()
        self.assertGreater(result["rtt_avg_ms"], 0)
        self.assertGreater(result["single_bytes_per_s"], 0)
        self.assertEqual(result["parallel_connections"], 3)
        self.assertGreater(result["parallel_bytes_per_s"], 0)
        text = "\n".join(lines)
        self.assertIn("Latência", text)
        self.assertIn("1 conexão", text)
        self.assertIn("3 conexões", text)


class ProgressTest(unittest.TestCase):
    class FakeTty(io.StringIO):
        def isatty(self):
            return True

    def test_line_fits_the_terminal_width(self):
        from mpm.net.migration import Progress
        out = self.FakeTty()
        with mock.patch("mpm.net.migration.shutil.get_terminal_size",
                        return_value=os.terminal_size((50, 24))):
            progress = Progress(39919, 26_500_000_000, out)
            progress.add_bytes(3_200_000_000)
            progress.file_done()
        last = out.getvalue().split("\r")[-1]
        self.assertLessEqual(len(last), 49)      # nunca quebra a linha: sem "rolar" a tela
        self.assertNotIn("\n", last)

    def test_thread_safe_totals(self):
        from mpm.net.migration import Progress
        progress = Progress(8000, 8000, io.StringIO())

        def hammer():
            for _ in range(1000):
                progress.add_bytes(1)
                progress.file_done()

        threads = [threading.Thread(target=hammer) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual((progress.done_files, progress.done_bytes, progress.transferred),
                         (8000, 8000, 8000))

    def test_no_absurd_eta_in_the_first_seconds(self):
        from mpm.net.migration import Progress
        out = io.StringIO()
        progress = Progress(100, 10_000_000_000, out)
        progress.add_bytes(3)                      # 3 bytes no primeiro instante não é uma taxa
        self.assertIn("calculando", out.getvalue())
        self.assertNotIn("resta ~", out.getvalue())

    def test_credit_counts_as_done_but_not_as_transferred(self):
        from mpm.net.migration import Progress
        progress = Progress(1, 100, io.StringIO())
        progress.credit(40)
        progress.add_bytes(60)
        self.assertEqual((progress.done_bytes, progress.transferred), (100, 60))


if __name__ == "__main__":
    unittest.main()
