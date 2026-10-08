"""Orquestração do RX: plano, cópia verificada em passadas e relatório.

Fluxo de cada passada:
  1. pede ao TX uma varredura nova e o inventário (arquivo a arquivo)
  2. aplica exclusões e descarta o que já está igual no destino (retomada/delta)
  3. copia o restante (em paralelo), verificando o SHA-256 de cada arquivo
  4. arquivo que mudou no TX durante a leitura é descartado e fica para a próxima passada

Mecânica inspirada no Robocopy:
  - várias conexões copiando ao mesmo tempo (como /MT)
  - arquivo interrompido continua de onde parou (como /Z): o .mpm-part é reaproveitado
    e a verificação final de SHA-256 cobre o arquivo inteiro
  - falhas transitórias (arquivo em uso, antivírus) são tentadas de novo com espera (/R /W)

Destino (modo pasta, padrão):
  <dest>/files/<pasta>/<caminho>     arquivos migrados
Destino (modo conta, 0.3 — `target` informado):
  o perfil real do usuário criado no RX (Desktop, Documents, Pictures); em ambos os modos
  os metadados abaixo ficam em <dest>/mpm:
  <dest>/mpm/manifest.json           manifesto do TX (última passada)
  <dest>/mpm/inventory.jsonl         inventário (última passada)
  <dest>/mpm/verified.jsonl          prova: pasta, caminho, tamanho e SHA-256 de cada cópia
  <dest>/mpm/transfer-report.json    resumo das passadas e falhas
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import queue
import shutil
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from fnmatch import fnmatchcase
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO

from mpm import __version__
from mpm.core.accounts import AccountError
from mpm.core.fmt import fmt_bytes, fmt_duration
from mpm.core.longpath import long_path
from mpm.core.models import KNOWN_FOLDER_KEYS
from mpm.net.protocol import CHUNK_SIZE, ProtocolError
from mpm.net.rx import RemoteError, RxClient
from mpm.net.safepath import resolve_in_root

if TYPE_CHECKING:
    from mpm.core.target import AccountTarget

FILE_ATTRIBUTE_NORMAL = 0x80
FILE_ATTRIBUTE_ARCHIVE = 0x20
ATTR_MASK = 0x1 | 0x2 | 0x4          # somente leitura, oculto, sistema


def _clear_protection(path: Path) -> None:
    """Windows: tira somente-leitura/oculto/sistema de um destino que vai ser substituído."""
    if sys.platform != "win32":
        return
    import ctypes
    ctypes.windll.kernel32.SetFileAttributesW(str(path), FILE_ATTRIBUTE_NORMAL)  # type: ignore[attr-defined]


def _set_attrs(path: Path, attrs: int) -> None:
    """Windows: aplica os atributos da origem. Melhor esforço: falha aqui não invalida a cópia."""
    if sys.platform != "win32" or not attrs:
        return
    import ctypes
    ctypes.windll.kernel32.SetFileAttributesW(  # type: ignore[attr-defined]
        str(path), (attrs & ATTR_MASK) | FILE_ATTRIBUTE_ARCHIVE)


MTIME_TOLERANCE_NS = 2_000_000_000     # 2 s: cobre sistemas de arquivos de resolução grossa (FAT/exFAT)
SMALL_FILE_BYTES = 256 * 1024
SPACE_MARGIN = 1.02
MAX_LISTED = 1000


class MigrationError(Exception):
    """Erro que interrompe a migração."""


BUSY_PREFIX = "EM USO NA ORIGEM: "


def _is_busy(text: str) -> bool:
    """Erro do TX que indica arquivo aberto/bloqueado por outro programa (Chrome, Outlook...)."""
    return any(m in text for m in ("PermissionError", "WinError 32", "WinError 33", "used by another process"))


@dataclass
class Options:
    dest: Path
    source_user: str | None = None
    excludes: list[str] = field(default_factory=list)
    passes: int = 2
    dry_run: bool = False
    assume_yes: bool = False
    retries: int = 2            # tentativas extras por arquivo (como /R)
    retry_wait: float = 2.0     # segundos entre tentativas (como /W)
    busy_is_warning: bool = False   # arquivo em uso na origem vira aviso, não falha (configurações)


@dataclass
class PassResult:
    label: str
    to_copy: int = 0
    copied: int = 0
    skipped: int = 0
    excluded: int = 0
    changed: list[str] = field(default_factory=list)
    failed: list[dict[str, str]] = field(default_factory=list)
    busy: list[str] = field(default_factory=list)      # em uso na origem (só com busy_is_warning)
    busy_count: int = 0
    bytes_copied: int = 0
    seconds: float = 0.0
    threads: int = 1
    bytes_transferred: int = 0       # bytes que realmente passaram pela rede nesta passada
    files_per_second: float = 0.0
    bytes_per_second: float = 0.0    # vazão de rede (bytes_transferred / seconds)
    small_files: int = 0             # copiados com menos de 256 KiB (limitados pela latência)
    resumed_files: int = 0           # continuados de um .mpm-part
    resumed_bytes: int = 0
    retries_used: int = 0


class Progress:
    """Progresso seguro para várias threads; a linha se ajusta à largura do terminal."""

    def __init__(self, total_files: int, total_bytes: int, stream: TextIO | None = None):
        self.stream = stream if stream is not None else sys.stderr
        self.total_files = total_files
        self.total_bytes = total_bytes
        self.done_files = 0
        self.done_bytes = 0          # inclui o que foi aproveitado de arquivos parciais
        self.transferred = 0         # só o que passou pela rede
        self._start = time.monotonic()
        self._last = 0.0
        self._lock = threading.Lock()
        self._tty = bool(getattr(self.stream, "isatty", lambda: False)())

    def add_bytes(self, nbytes: int) -> None:
        with self._lock:
            self.done_bytes += nbytes
            self.transferred += nbytes
            self._emit(final=False)

    def credit(self, nbytes: int) -> None:
        """Bytes que já estavam no destino (arquivo parcial reaproveitado)."""
        with self._lock:
            self.done_bytes += nbytes

    def file_done(self) -> None:
        with self._lock:
            self.done_files += 1
            self._emit(final=False)

    def finish(self) -> None:
        with self._lock:
            self._emit(final=True)

    def _emit(self, *, final: bool) -> None:
        now = time.monotonic()
        if not final and now - self._last < (0.5 if self._tty else 10.0):
            return
        self._last = now
        elapsed = max(now - self._start, 1e-6)
        rate = self.transferred / elapsed
        left = max(0, self.total_bytes - self.done_bytes)
        remaining = left / rate if rate > 0 else 0
        eta = f"resta ~{fmt_duration(remaining)}" if elapsed >= 3 and rate > 0 else "calculando..."
        line = (f"  {self.done_files}/{self.total_files} arq.  "
                f"{fmt_bytes(self.done_bytes)}/{fmt_bytes(self.total_bytes)}  "
                f"{fmt_bytes(rate)}/s  {eta}")
        if self._tty:
            width = max(20, shutil.get_terminal_size((100, 24)).columns - 1)
            self.stream.write("\r" + line[:width].ljust(width) + ("\n" if final else ""))
        else:
            self.stream.write(line + "\n")
        self.stream.flush()


class Migration:
    def __init__(
        self,
        client: RxClient,
        options: Options,
        *,
        log: Callable[[str], None] = print,
        ask: Callable[[str], bool] | None = None,
        choose_profile: Callable[[list[dict[str, Any]]], str] | None = None,
        pause: Callable[[int], None] | None = None,
        progress_stream: TextIO | None = None,
        workers: list[RxClient] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        target: AccountTarget | None = None,
    ):
        self.client = client
        self.workers = list(workers or [])
        self.sleep = sleep
        self._stats_lock = threading.Lock()
        self._stats = {"retries": 0, "resumed": 0, "resumed_bytes": 0}
        self.options = options
        self.log = log
        self.ask = ask
        self.choose_profile = choose_profile
        self.pause = pause
        self.progress_stream = progress_stream
        self.target = target
        self._committed = False
        self._dirs: set[Path] = set()
        self._dir_lock = threading.Lock()
        self.roots: dict[str, Path] | None = None      # modo conta: pasta -> perfil real
        self.files_root = options.dest / "files"
        self.meta = options.dest / "mpm"
        self._patterns = [p.lower() for p in options.excludes]

    # ------------------------------------------------------------------ run
    def run(self) -> dict[str, Any]:
        self.meta.mkdir(parents=True, exist_ok=True)
        if self.target is None:
            self.files_root.mkdir(parents=True, exist_ok=True)
        started = datetime.now(timezone.utc)

        info = self.client.request({"op": "info"})
        profile = self._pick_profile(info)
        src = info["system"]
        self.log(f"Origem: {src['hostname']} ({src['os_name']}), perfil '{profile}'")
        if self.target is not None:
            try:
                self.roots = self.target.plan(profile, dry_run=self.options.dry_run)
            except AccountError as exc:
                raise MigrationError(str(exc)) from exc
            self.log(f"Metadados da migração: {self.meta}")
        else:
            self.log(f"Destino: {self.options.dest}")
        self.log(f"Conexões de cópia: {len(self.workers) or 1}")

        results: list[PassResult] = []
        manifest: dict[str, Any] = {}
        aborted: str | None = None
        dry: dict[str, Any] | None = None
        summary: dict[str, Any] = {}
        try:
            for number in range(1, self.options.passes + 1):
                label = "Passada 1 (massa)" if number == 1 else (
                    "Passada final (diferenças)" if number == self.options.passes
                    else f"Passada {number} (diferenças)")
                if number > 1 and self.pause:
                    self.pause(number)

                manifest, entries = self._scan(profile)
                todo, result = self._plan(entries, label)
                self._print_plan(label, entries, todo, result)

                if number == 1:
                    self._check_space(todo)
                    if self.options.dry_run:
                        dry = {"ok": True, "dry_run": True, "profile": profile,
                               "to_copy": result.to_copy,
                               "bytes_to_copy": sum(e["size"] for e in todo)}
                        break
                    question = ("Criar o usuário/perfil e iniciar a cópia?"
                                if self.target is not None else "Iniciar a cópia?")
                    if not self.options.assume_yes and self.ask and not self.ask(question):
                        raise MigrationError("cancelado pelo usuário")
                    self._commit_target()

                if number > 1:
                    self._commit_target()       # retomada/passadas seguintes: garante as pastas
                self._copy(todo, result)
                results.append(result)
                self._print_result(result)
            if self.target is not None and results and dry is None:
                self.target.finalize()
        except (MigrationError, ConnectionError, RemoteError, OSError) as exc:
            aborted = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            if dry is None:
                summary = self._write_report(manifest, profile, started, results, aborted)
        return dry if dry is not None else summary

    def _commit_target(self) -> None:
        """Modo conta: cria usuário/perfil (uma vez) e passa a usar as pastas reais."""
        if self.target is None or self._committed:
            return
        try:
            self.roots = self.target.commit()
        except AccountError as exc:
            raise MigrationError(str(exc)) from exc
        self._committed = True

    # ------------------------------------------------------------- profile
    def _pick_profile(self, info: dict[str, Any]) -> str:
        profiles: list[dict[str, Any]] = info["profiles"]
        names = ", ".join(p["username"] for p in profiles) or "(nenhum)"
        wanted = self.options.source_user
        if wanted:
            for p in profiles:
                if wanted.lower() in (p["username"].lower(), p["sid"].lower()):
                    return p["username"]
            raise MigrationError(f"perfil {wanted!r} não existe no TX. Disponíveis: {names}")
        if self.choose_profile and len(profiles) > 1:
            return self.choose_profile(profiles)
        for p in profiles:
            if p.get("is_current"):
                return p["username"]
        if len(profiles) == 1:
            return profiles[0]["username"]
        raise MigrationError(f"indique o perfil com --source-user. Disponíveis: {names}")

    # ---------------------------------------------------------------- scan
    def _scan(self, profile: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        self.log("\nPedindo varredura ao TX ...")
        manifest = self.client.request({"op": "scan", "user": profile})["manifest"]

        inventory = self.meta / "inventory.jsonl"
        digest = hashlib.sha256()
        with inventory.open("wb") as fh:
            def sink(block: bytes) -> None:
                fh.write(block)
                digest.update(block)
            end = self.client.stream({"op": "get_inventory"}, sink)
        if end["sha256"] != digest.hexdigest():
            raise MigrationError("inventário corrompido na transferência")

        entries = [json.loads(line) for line in
                   inventory.read_text(encoding="utf-8").splitlines() if line.strip()]
        return manifest, entries

    # ---------------------------------------------------------------- plan
    def _excluded(self, rel: str) -> bool:
        low = rel.lower()
        parts = low.split("/")
        return any(fnmatchcase(low, p) or any(fnmatchcase(part, p) for part in parts)
                   for p in self._patterns)

    def _target(self, entry: dict[str, Any]) -> Path:
        folder = entry["folder"]
        if folder not in KNOWN_FOLDER_KEYS:
            raise ValueError(f"pasta fora do escopo: {folder!r}")
        root = self.roots[folder] if self.roots is not None else self.files_root / folder
        return long_path(resolve_in_root(root, entry["path"], lexical=True))

    @staticmethod
    def _up_to_date(target: Path, entry: dict[str, Any]) -> bool:
        try:
            st = target.stat()
        except OSError:
            return False
        return (st.st_size == entry["size"]
                and abs(st.st_mtime_ns - entry["mtime_ns"]) <= MTIME_TOLERANCE_NS)

    def _plan(self, entries: list[dict[str, Any]], label: str) -> tuple[list[dict[str, Any]], PassResult]:
        result = PassResult(label=label)
        todo: list[dict[str, Any]] = []
        for entry in entries:
            try:
                valid = (isinstance(entry["size"], int) and isinstance(entry["mtime_ns"], int)
                         and entry["size"] >= 0)
                if not valid:
                    raise ValueError("entrada inválida")
                if self._excluded(entry["path"]):
                    result.excluded += 1
                    continue
                if self._up_to_date(self._target(entry), entry):
                    result.skipped += 1
                    continue
            except (KeyError, ValueError, TypeError) as exc:
                if len(result.failed) < MAX_LISTED:
                    result.failed.append({"path": str(entry.get("path", "?")), "error": f"inventário: {exc}"})
                continue
            todo.append(entry)
        result.to_copy = len(todo)
        return todo, result

    def _print_plan(self, label: str, entries: list[dict[str, Any]],
                    todo: list[dict[str, Any]], result: PassResult) -> None:
        by_folder: dict[str, list[int]] = {}
        for e in todo:
            slot = by_folder.setdefault(e["folder"], [0, 0])
            slot[0] += 1
            slot[1] += e["size"]
        in_scope = len(entries) - result.excluded
        self.log(f"\n[{label}] Plano")
        self.log(f"  Arquivos na origem (após exclusões): {in_scope}")
        self.log(f"  Já presentes e iguais no destino:    {result.skipped}")
        self.log(f"  A copiar:                            {result.to_copy} "
                 f"({fmt_bytes(sum(e['size'] for e in todo))})")
        for folder in KNOWN_FOLDER_KEYS:
            if folder in by_folder:
                count, size = by_folder[folder]
                self.log(f"      {folder:<10} {count:>8} arquivos {fmt_bytes(size):>10}")
        if result.excluded:
            self.log(f"  Excluídos (--exclude):               {result.excluded}")
        if result.failed:
            self.log(f"  Entradas inválidas ignoradas:        {len(result.failed)}")

    def _check_space(self, todo: list[dict[str, Any]]) -> None:
        required = sum(e["size"] for e in todo)
        free = shutil.disk_usage(self._space_path()).free
        self.log(f"  Espaço necessário / livre no destino: {fmt_bytes(required)} / {fmt_bytes(free)}")
        if required * SPACE_MARGIN > free:
            raise MigrationError(
                f"espaço insuficiente no destino: precisa de {fmt_bytes(required)}, "
                f"há {fmt_bytes(free)} livres")

    def _space_path(self) -> Path:
        """Pasta (existente) cujo disco vai receber os arquivos."""
        if self.roots:
            probe = next(iter(self.roots.values()))
            while not probe.exists() and probe != probe.parent:
                probe = probe.parent
            return probe
        return self.options.dest

    # ---------------------------------------------------------------- copy
    def _copy(self, todo: list[dict[str, Any]], result: PassResult) -> None:
        started = time.monotonic()
        clients: list[RxClient] = list(self.workers) or [self.client]
        clients = clients[: max(1, min(len(clients), len(todo)))]
        result.threads = len(clients)
        with self._stats_lock:
            self._stats.update(retries=0, resumed=0, resumed_bytes=0)

        progress = Progress(len(todo), sum(e["size"] for e in todo), self.progress_stream)
        pending: queue.Queue[dict[str, Any]] = queue.Queue()
        for entry in todo:
            pending.put(entry)
        lock = threading.Lock()
        abort = threading.Event()
        fatal: list[BaseException] = []
        lost: list[str] = []

        with (self.meta / "verified.jsonl").open("a", encoding="utf-8", newline="\n") as verified:

            def worker(client: RxClient) -> None:
                while not abort.is_set():
                    try:
                        entry = pending.get_nowait()
                    except queue.Empty:
                        return
                    try:
                        status, detail = self._copy_one(client, entry, progress)
                    except MigrationError as exc:
                        fatal.append(exc)
                        abort.set()
                        return
                    except (ConnectionError, ProtocolError, OSError) as exc:
                        pending.put(entry)           # outra conexão assume; esta já não serve
                        with lock:
                            lost.append(f"{type(exc).__name__}: {exc}")
                        return
                    except BaseException as exc:     # noqa: BLE001 - não deixar a thread morrer calada
                        fatal.append(exc)
                        abort.set()
                        return
                    with lock:
                        if status == "copied":
                            result.copied += 1
                            result.bytes_copied += detail["size"]
                            if detail["size"] < SMALL_FILE_BYTES:
                                result.small_files += 1
                            verified.write(json.dumps(
                                {"folder": entry["folder"], "path": entry["path"],
                                 "size": detail["size"], "sha256": detail["sha256"]},
                                ensure_ascii=True) + "\n")
                            verified.flush()
                        elif status == "changed":
                            if len(result.changed) < MAX_LISTED:
                                result.changed.append(f"{entry['folder']}/{entry['path']}")
                        elif self.options.busy_is_warning and str(detail).startswith(BUSY_PREFIX):
                            result.busy_count += 1
                            if len(result.busy) < MAX_LISTED:
                                result.busy.append(f"{entry['folder']}/{entry['path']}")
                        elif len(result.failed) < MAX_LISTED:
                            result.failed.append(
                                {"path": f"{entry['folder']}/{entry['path']}", "error": detail})
                    progress.file_done()

            threads = [threading.Thread(target=worker, args=(c,), daemon=True,
                                        name=f"mpm-copy-{i}") for i, c in enumerate(clients)]
            for t in threads:
                t.start()
            try:
                for t in threads:
                    while t.is_alive():
                        t.join(0.5)          # com timeout, para o Ctrl+C funcionar no Windows
            except KeyboardInterrupt:
                abort.set()
                raise

        progress.finish()
        result.seconds = time.monotonic() - started
        result.bytes_transferred = progress.transferred
        if result.seconds > 0:
            result.files_per_second = round(result.copied / result.seconds, 2)
            result.bytes_per_second = round(progress.transferred / result.seconds, 1)
        with self._stats_lock:
            result.retries_used = self._stats["retries"]
            result.resumed_files = self._stats["resumed"]
            result.resumed_bytes = self._stats["resumed_bytes"]

        if fatal:
            raise fatal[0]
        if not pending.empty():
            raise MigrationError(
                "todas as conexões com o TX foram perdidas "
                f"({lost[0] if lost else 'motivo desconhecido'}); "
                "rode de novo com o mesmo destino para retomar")
        if lost:
            self.log(f"Aviso: {len(lost)} conexão(ões) de cópia caíram durante a passada "
                     f"({lost[0]}); as demais concluíram o trabalho.")

    def _ensure_dir(self, path: Path) -> None:
        """Cria a pasta de destino. Serializado: no Windows, threads criando a mesma pasta ao
        mesmo tempo podem receber 'acesso negado' (0.3.2)."""
        if path in self._dirs:
            return
        with self._dir_lock:
            path.mkdir(parents=True, exist_ok=True)
            self._dirs.add(path)

    def _bump(self, key: str, amount: int = 1) -> None:
        with self._stats_lock:
            self._stats[key] += amount

    def _copy_one(self, client: RxClient, entry: dict[str, Any],
                  progress: Progress | None = None) -> tuple[str, Any]:
        """Copia um arquivo com tentativas (como /R e /W do Robocopy)."""
        try:
            target = self._target(entry)
            part = target.with_name(target.name + ".mpm-part")
        except (OSError, ValueError) as exc:
            return "failed", f"{type(exc).__name__}: {exc}"

        attempts = max(0, self.options.retries) + 1
        last = "falha desconhecida"
        for attempt in range(attempts):
            if attempt:
                self._bump("retries")
                if self.options.retry_wait > 0:
                    self.sleep(self.options.retry_wait)
            try:
                self._ensure_dir(target.parent)      # dentro das tentativas: falha transitória é refeita
            except OSError as exc:
                last = f"{type(exc).__name__}: {exc}"
                continue
            status, detail, retryable = self._attempt(client, entry, target, part, progress)
            if status != "failed" or not retryable:
                return status, detail
            last = detail
        return "failed", f"{last} (após {attempts} tentativa(s))"

    @staticmethod
    def _open_part(part: Path, size: int):
        """Abre o .mpm-part para continuar de onde parou. Retorna (arquivo, hash do prefixo, offset)."""
        digest = hashlib.sha256()
        try:
            existing = part.stat().st_size
        except OSError:
            existing = 0
        if 0 < existing <= size:
            fh = part.open("r+b")
            read = 0
            while read < existing:
                block = fh.read(min(CHUNK_SIZE, existing - read))
                if not block:
                    break
                digest.update(block)
                read += len(block)
            if read == existing:
                fh.seek(existing)
                return fh, digest, existing
            fh.close()
            digest = hashlib.sha256()
        return part.open("wb"), digest, 0

    def _attempt(self, client: RxClient, entry: dict[str, Any], target: Path, part: Path,
                 progress: Progress | None) -> tuple[str, Any, bool]:
        """Uma tentativa. Retorna (status, detalhe, vale_tentar_de_novo)."""
        try:
            fh, digest, offset = self._open_part(part, entry["size"])
        except OSError as exc:
            return "failed", f"{type(exc).__name__}: {exc}", True

        request: dict[str, Any] = {"op": "fetch", "folder": entry["folder"], "path": entry["path"]}
        if offset:
            request["offset"] = offset
            self._bump("resumed")
            self._bump("resumed_bytes", offset)
            if progress:
                progress.credit(offset)
        state: dict[str, Any] = {"written": 0, "error": None}

        def sink(block: bytes) -> None:
            if state["error"] is not None:      # segue consumindo para manter o fluxo sincronizado
                return
            try:
                fh.write(block)
            except OSError as exc:
                state["error"] = exc
                return
            digest.update(block)
            state["written"] += len(block)
            if progress:
                progress.add_bytes(len(block))

        try:
            end = client.stream(request, sink)
        except RemoteError as exc:
            fh.close()
            self._discard(part)
            text = str(exc)
            return "failed", (BUSY_PREFIX + text if _is_busy(text) else text), True
        except BaseException:
            try:
                fh.close()          # conexão caiu: o parcial fica no disco para a retomada
            except OSError:
                pass
            raise

        try:
            fh.close()
        except OSError as exc:
            state["error"] = state["error"] or exc

        if state["error"] is not None:
            self._discard(part)
            if getattr(state["error"], "errno", None) == errno.ENOSPC:
                raise MigrationError("disco do destino cheio")
            return "failed", f"{type(state['error']).__name__}: {state['error']}", True
        if end.get("changed"):
            self._discard(part)
            return "changed", None, False
        if (end["sha256"] == digest.hexdigest()
                and end["size"] == offset + state["written"]
                and end.get("offset", 0) == offset):
            try:
                _clear_protection(target)        # destino de uma cópia anterior pode estar somente-leitura
                os.replace(part, target)
                mtime = int(end["mtime_ns"])
                os.utime(target, ns=(mtime, mtime))
                _set_attrs(target, int(end.get("attrs", 0)))     # por último: somente-leitura trava o resto
            except OSError as exc:
                # arquivo em uso/antivírus: o .mpm-part completo fica e a próxima tentativa só reconfere
                return "failed", f"{type(exc).__name__}: {exc}", True
            return "copied", {"sha256": end["sha256"], "size": end["size"]}, False

        self._discard(part)
        return "failed", "hash divergente", True

    @staticmethod
    def _discard(part: Path) -> None:
        try:
            part.unlink()
        except OSError:
            pass

    # -------------------------------------------------------------- report
    def _print_result(self, r: PassResult) -> None:
        self.log(f"[{r.label}] copiados {r.copied}/{r.to_copy} ({fmt_bytes(r.bytes_copied)}) "
                 f"em {fmt_duration(r.seconds)}; mudaram durante a cópia: {len(r.changed)}; "
                 f"falhas: {len(r.failed)}")
        if r.busy_count:
            self.log(f"    em uso na origem, não copiados: {r.busy_count} (aviso; feche o programa no TX e rode de novo "
                     "se precisar desses arquivos)")
        if r.to_copy:
            self.log(f"    {r.files_per_second:.1f} arq/s, {fmt_bytes(r.bytes_per_second)}/s pela rede, "
                     f"{r.threads} conexão(ões); {r.small_files} arquivos < 256 KiB; "
                     f"continuados: {r.resumed_files}; tentativas extras: {r.retries_used}")

    def _write_report(self, manifest: dict[str, Any], profile: str, started: datetime,
                      results: list[PassResult], aborted: str | None) -> dict[str, Any]:
        last = results[-1] if results else None
        ok = bool(last) and not last.failed and not last.changed and aborted is None
        summary = {
            "ok": ok,
            "aborted": aborted,
            "tool_version": __version__,
            "migration_id": manifest.get("migration_id"),
            "profile": profile,
            "dest": str(self.options.dest),
            "target_user": getattr(self.target, "name", None),
            "target_roots": {k: str(v) for k, v in (self.roots or {}).items()} or None,
            "excludes": self.options.excludes,
            "started": started.isoformat(),
            "finished": datetime.now(timezone.utc).isoformat(),
            "passes": [asdict(r) for r in results],
        }
        if manifest:
            (self.meta / "manifest.json").write_text(
                json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        (self.meta / "transfer-report.json").write_text(
            json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return summary
