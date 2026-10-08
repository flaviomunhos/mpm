"""Autenticação mútua por código de pareamento (executada dentro do TLS)."""

from __future__ import annotations

import hmac
import secrets
import socket

from mpm import __version__
from mpm.net.protocol import PROTOCOL_VERSION, ProtocolError, recv_msg, send_msg
from mpm.net.security import AuthError, auth_mac, fingerprint


def authenticate_as_rx(conn: socket.socket, code: str, own_fingerprint: str) -> str:
    """RX: desafia o TX e prova que também conhece o código. Retorna a versão do TX."""
    nonce_rx = secrets.token_bytes(16)
    send_msg(conn, {
        "op": "challenge", "protocol": PROTOCOL_VERSION,
        "nonce": nonce_rx.hex(), "version": __version__,
    })
    header, _ = recv_msg(conn)
    if header.get("op") != "auth" or header.get("protocol") != PROTOCOL_VERSION:
        send_msg(conn, {"ok": False, "error": "protocolo incompatível"})
        raise AuthError("protocolo incompatível")
    try:
        nonce_tx = bytes.fromhex(str(header["nonce"]))
        mac = str(header["mac"])
    except (KeyError, ValueError) as exc:
        send_msg(conn, {"ok": False, "error": "autenticação malformada"})
        raise ProtocolError("autenticação malformada") from exc

    expected = auth_mac(code, b"TX", own_fingerprint, nonce_rx, nonce_tx)
    if not hmac.compare_digest(mac, expected):
        send_msg(conn, {"ok": False, "error": "código de pareamento inválido"})
        raise AuthError("código de pareamento inválido")

    send_msg(conn, {"ok": True, "mac": auth_mac(code, b"RX", own_fingerprint, nonce_rx, nonce_tx)})
    return str(header.get("version", "?"))


def authenticate_as_tx(conn, code: str) -> str:
    """TX: responde ao desafio e confere se o RX conhece o código. Retorna a versão do RX."""
    header, _ = recv_msg(conn)
    if header.get("op") != "challenge" or header.get("protocol") != PROTOCOL_VERSION:
        raise AuthError("protocolo incompatível (versões diferentes do MPM?)")
    try:
        nonce_rx = bytes.fromhex(str(header["nonce"]))
    except (KeyError, ValueError) as exc:
        raise ProtocolError("desafio malformado") from exc

    seen_fp = fingerprint(conn.getpeercert(binary_form=True))   # certificado que este lado viu
    nonce_tx = secrets.token_bytes(16)
    send_msg(conn, {
        "op": "auth", "protocol": PROTOCOL_VERSION, "version": __version__,
        "nonce": nonce_tx.hex(), "mac": auth_mac(code, b"TX", seen_fp, nonce_rx, nonce_tx),
    })

    reply, _ = recv_msg(conn)
    if not reply.get("ok"):
        raise AuthError(str(reply.get("error", "autenticação recusada pelo RX")))
    expected = auth_mac(code, b"RX", seen_fp, nonce_rx, nonce_tx)
    if not hmac.compare_digest(str(reply.get("mac", "")), expected):
        raise AuthError("o RX não provou conhecer o código (possível interceptação)")
    return str(header.get("version", "?"))
