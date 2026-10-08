"""Pareamento e canal seguro.

Modelo:
- O RX gera, a cada sessão, um certificado autoassinado efêmero (EC P-256) e
  um código de pareamento (8 caracteres) exibido na tela.
- O canal é TLS 1.3. O certificado não é validado por CA: a autenticidade vem
  do código, provado por HMAC que inclui a impressão digital do certificado
  que cada lado realmente viu. Um intermediário (MITM) tem outro certificado
  e não conhece o código, então não consegue produzir um HMAC válido.
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import secrets
import ssl
import tempfile
from pathlib import Path

CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"   # sem I, L, O, 0, 1
CODE_LENGTH = 8


class MissingDependency(RuntimeError):
    """Pacote opcional ausente (cryptography)."""


class AuthError(Exception):
    """Falha de autenticação no pareamento."""


def new_pairing_code() -> str:
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
    return f"{raw[:4]}-{raw[4:]}"


def normalize_code(code: str) -> str:
    return "".join(ch for ch in code.upper() if ch.isalnum())


def fingerprint(der: bytes) -> str:
    return hashlib.sha256(der).hexdigest()


def short_fingerprint(fp_hex: str) -> str:
    """Formato legível para conferência visual (primeiros 16 hex em grupos de 4)."""
    head = fp_hex[:16].upper()
    return "-".join(head[i:i + 4] for i in range(0, len(head), 4))


def auth_mac(code: str, label: bytes, fp_hex: str, nonce_rx: bytes, nonce_tx: bytes) -> str:
    message = label + bytes.fromhex(fp_hex) + nonce_rx + nonce_tx
    return hmac.new(normalize_code(code).encode("ascii"), message, hashlib.sha256).hexdigest()


def make_server_context() -> tuple[ssl.SSLContext, str]:
    """Contexto TLS do RX com certificado efêmero. Retorna (contexto, fingerprint)."""
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID
    except ImportError as exc:
        raise MissingDependency(
            "O modo de rede requer o pacote 'cryptography' (pip install cryptography)."
        ) from exc

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "MPM-RX")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    der = cert.public_bytes(serialization.Encoding.DER)

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    with tempfile.TemporaryDirectory() as tmp:       # o ssl só carrega de arquivo
        cert_path, key_path = Path(tmp) / "c.pem", Path(tmp) / "k.pem"
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ))
        context.load_cert_chain(str(cert_path), str(key_path))
    return context, fingerprint(der)


def make_client_context() -> ssl.SSLContext:
    """Contexto TLS do TX: sem validação por CA (a autenticação é pelo código)."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context
