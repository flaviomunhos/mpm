"""Enquadramento de mensagens: [4B tam_cabeçalho][4B tam_payload][JSON][payload]."""

from __future__ import annotations

import json
import socket
import struct
from typing import Any

PROTOCOL_VERSION = 1
DEFAULT_PORT = 47800
CHUNK_SIZE = 1024 * 1024
MAX_HEADER = 16 * 1024 * 1024
MAX_PAYLOAD = 8 * 1024 * 1024


class ProtocolError(Exception):
    """Mensagem malformada ou fora do protocolo."""


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    if n == 0:
        return b""
    buf = bytearray(n)
    view = memoryview(buf)
    got = 0
    while got < n:
        read = sock.recv_into(view[got:], n - got)
        if read == 0:
            raise ConnectionError("conexão encerrada pelo outro lado")
        got += read
    return bytes(buf)


def send_msg(sock: socket.socket, header: dict[str, Any], payload: bytes = b"") -> None:
    raw = json.dumps(header, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    sock.sendall(struct.pack(">II", len(raw), len(payload)) + raw + payload)


def recv_msg(sock: socket.socket) -> tuple[dict[str, Any], bytes]:
    header_len, payload_len = struct.unpack(">II", _recv_exact(sock, 8))
    if header_len > MAX_HEADER or payload_len > MAX_PAYLOAD:
        raise ProtocolError("mensagem acima do limite permitido")
    try:
        header = json.loads(_recv_exact(sock, header_len))
    except ValueError as exc:
        raise ProtocolError("cabeçalho inválido") from exc
    if not isinstance(header, dict):
        raise ProtocolError("cabeçalho deve ser um objeto JSON")
    return header, _recv_exact(sock, payload_len)
