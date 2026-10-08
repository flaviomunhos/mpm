"""Lado RX (máquina nova): escuta, autentica o TX e envia pedidos."""

from __future__ import annotations

import secrets
import socket
import ssl
import time
from collections.abc import Callable
from typing import Any

from mpm.net.discovery import local_addresses  # noqa: F401  (reexportado para o CLI)
from mpm.net.handshake import authenticate_as_rx
from mpm.net.protocol import DEFAULT_PORT, ProtocolError, recv_msg, send_msg
from mpm.net.security import AuthError, make_server_context, new_pairing_code


class RemoteError(Exception):
    """O TX respondeu com erro."""


class RxListener:
    def __init__(self, host: str = "0.0.0.0", port: int = DEFAULT_PORT):
        self.context, self.fingerprint = make_server_context()
        self.code = new_pairing_code()
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind((host, port))
        self._server.listen(64)
        self.port: int = self._server.getsockname()[1]

    def accept_authenticated(
        self,
        *,
        max_failures: int = 3,
        timeout: float | None = None,
        log: Callable[[str], None] = print,
        code: str | None = None,
    ) -> ssl.SSLSocket:
        """Espera o TX conectar e provar o código. Falhas repetidas abortam a sessão.

        `code` substitui o código de pareamento (usado nas conexões de trabalho, que
        se autenticam com um segredo de sessão em vez do código digitado)."""
        expected_code = code or self.code
        failures = 0
        deadline = time.monotonic() + timeout if timeout else None
        while True:
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("nenhum TX se conectou a tempo")
                self._server.settimeout(remaining)
            else:
                self._server.settimeout(None)

            raw, addr = self._server.accept()
            raw.settimeout(30)
            raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)   # evita Nagle + ACK atrasado
            conn: ssl.SSLSocket | None = None
            try:
                conn = self.context.wrap_socket(raw, server_side=True)
                authenticate_as_rx(conn, expected_code, self.fingerprint)
                conn.settimeout(None)
                conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                if code is None:
                    log(f"TX conectado e autenticado: {addr[0]}")
                return conn
            except (AuthError, ProtocolError, ssl.SSLError, OSError) as exc:
                failures += 1
                for sock in (conn, raw):     # wrap_socket assume o descritor: fechar os dois
                    try:
                        if sock is not None:
                            sock.close()
                    except OSError:
                        pass
                log(f"Conexão recusada de {addr[0]}: {exc}")
                if failures >= max_failures:
                    raise AuthError("excesso de tentativas de pareamento") from exc

    def close(self) -> None:
        self._server.close()


class RxClient:
    """Pedidos do RX ao TX sobre a conexão autenticada."""

    def __init__(self, conn: ssl.SSLSocket):
        self.conn = conn

    def request(self, header: dict[str, Any]) -> dict[str, Any]:
        send_msg(self.conn, header)
        reply, _ = recv_msg(self.conn)
        if not reply.get("ok"):
            raise RemoteError(str(reply.get("error", "erro desconhecido no TX")))
        return reply

    def stream(self, header: dict[str, Any], sink: Callable[[bytes], None]) -> dict[str, Any]:
        """Recebe uma sequência de blocos; devolve o cabeçalho final (hash, tamanho...)."""
        send_msg(self.conn, header)
        while True:
            reply, payload = recv_msg(self.conn)
            if not reply.get("ok"):
                raise RemoteError(str(reply.get("error", "erro desconhecido no TX")))
            if reply.get("chunk"):
                sink(payload)
            elif reply.get("end"):
                return reply
            else:
                raise ProtocolError("resposta de fluxo inesperada")

    def close(self) -> None:
        try:
            send_msg(self.conn, {"op": "bye"})
            recv_msg(self.conn)
        except (OSError, ProtocolError):
            pass
        try:
            self.conn.close()
        except OSError:
            pass


def open_workers(
    client: RxClient,
    listener: RxListener,
    count: int,
    *,
    timeout: float = 30.0,
    log: Callable[[str], None] = print,
) -> list[RxClient]:
    """Pede ao TX `count` conexões de trabalho e as aceita (cópia em paralelo).

    Cada conexão se autentica com um segredo aleatório de sessão, enviado pelo canal
    principal já autenticado. Qualquer outra máquina que tente se conectar nesse
    intervalo não conhece o segredo e é recusada (sem derrubar a sessão)."""
    secret = secrets.token_hex(16)
    client.request({"op": "open_workers", "count": count, "secret": secret})
    workers: list[RxClient] = []
    try:
        for _ in range(count):
            conn = listener.accept_authenticated(
                code=secret, timeout=timeout, log=log, max_failures=20)
            workers.append(RxClient(conn))
    except BaseException:
        close_workers(workers)
        raise
    return workers


def close_workers(workers: list[RxClient]) -> None:
    for worker in workers:
        worker.close()
