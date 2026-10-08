"""Descoberta do RX na rede local (broadcast UDP).

O RX responde a sondagens enquanto espera o TX. O TX envia a sondagem por todas
as placas de rede e junta as respostas.

Segurança:
- A resposta só leva versão, nome do computador e porta TCP. Nada derivado do código
  de pareamento circula aqui (isso permitiria descobri-lo por tentativa offline).
  Quem prova que conhece o código continua sendo o handshake dentro do TLS.
- A sondagem é preenchida até PROBE_MIN_SIZE e o RX ignora sondagens menores, para
  que a resposta nunca seja maior que o pedido (sem amplificação por IP forjado).
- O RX limita o ritmo de respostas e para de responder assim que um TX é pareado.
"""

from __future__ import annotations

import json
import secrets
import select
import socket
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from mpm import __version__
from mpm.net.protocol import DEFAULT_PORT

DISCOVERY_PORT = DEFAULT_PORT        # UDP; o número da porta TCP vem dentro da resposta
PROBE_MIN_SIZE = 256
MAX_DATAGRAM = 1024
MAX_REPLIES_PER_SECOND = 20
_MAGIC = "mpm-discovery"
_DISCOVERY_VERSION = 1


@dataclass(frozen=True)
class RxAnnouncement:
    hostname: str
    address: str
    port: int
    mpm_version: str


def local_addresses() -> list[str]:
    """Endereços IPv4 desta máquina (sem loopback)."""
    addresses: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addresses.add(info[4][0])
    except OSError:
        pass
    try:      # descobre a interface de saída; UDP connect não envia nenhum pacote
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("10.255.255.255", 1))
        addresses.add(probe.getsockname()[0])
        probe.close()
    except OSError:
        pass
    return sorted(a for a in addresses if not a.startswith("127."))


def build_probe(nonce: str) -> bytes:
    body = {"magic": _MAGIC, "v": _DISCOVERY_VERSION, "type": "probe", "nonce": nonce, "pad": ""}
    size = len(json.dumps(body, separators=(",", ":")))
    body["pad"] = "." * max(0, PROBE_MIN_SIZE - size)
    return json.dumps(body, separators=(",", ":")).encode()


def parse_probe(data: bytes) -> str | None:
    """Devolve o nonce se for uma sondagem válida e com o tamanho mínimo; senão None."""
    if len(data) < PROBE_MIN_SIZE or len(data) > MAX_DATAGRAM:
        return None
    try:
        body = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        return None
    if (not isinstance(body, dict) or body.get("magic") != _MAGIC
            or body.get("v") != _DISCOVERY_VERSION or body.get("type") != "probe"):
        return None
    nonce = body.get("nonce")
    if not isinstance(nonce, str) or not 8 <= len(nonce) <= 64:
        return None
    return nonce


def build_reply(nonce: str, hostname: str, tcp_port: int) -> bytes:
    body = {
        "magic": _MAGIC, "v": _DISCOVERY_VERSION, "type": "announce", "nonce": nonce,
        "hostname": hostname[:63], "port": tcp_port, "mpm": __version__,
    }
    return json.dumps(body, separators=(",", ":")).encode()


def parse_reply(data: bytes, nonce: str, address: str) -> RxAnnouncement | None:
    if len(data) > MAX_DATAGRAM:
        return None
    try:
        body = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        return None
    if (not isinstance(body, dict) or body.get("magic") != _MAGIC
            or body.get("type") != "announce" or body.get("nonce") != nonce):
        return None
    hostname, port, version = body.get("hostname"), body.get("port"), body.get("mpm")
    if (not isinstance(hostname, str) or not isinstance(port, int)
            or isinstance(port, bool) or not 1 <= port <= 65535):
        return None
    return RxAnnouncement(
        hostname=_clean(hostname) or address,
        address=address,
        port=port,
        mpm_version=_clean(str(version))[:16] if version is not None else "?",
    )


def _clean(text: str) -> str:
    """Nome vindo da rede é só exibição: remove caracteres de controle."""
    return "".join(ch for ch in text if ch.isprintable()).strip()[:63]


class DiscoveryResponder:
    """Thread do RX que responde às sondagens enquanto ele espera o TX."""

    def __init__(
        self,
        tcp_port: int,
        *,
        host: str = "",
        udp_port: int = DISCOVERY_PORT,
        hostname: str | None = None,
    ):
        self.tcp_port = tcp_port
        self.hostname = hostname or socket.gethostname()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self._sock.bind((host, udp_port))
        except OSError:
            self._sock.close()
            raise
        self.udp_port: int = self._sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="mpm-discovery", daemon=True)

    def start(self) -> DiscoveryResponder:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(2)
        try:
            self._sock.close()
        except OSError:
            pass

    def _loop(self) -> None:
        window_start = time.monotonic()
        sent_in_window = 0
        self._sock.settimeout(0.25)
        while not self._stop.is_set():
            try:
                data, addr = self._sock.recvfrom(MAX_DATAGRAM + 1)
            except TimeoutError:
                continue
            except OSError:
                if self._stop.is_set():
                    return
                continue          # ex.: ICMP "porta inacessível" no Windows (WSAECONNRESET)
            now = time.monotonic()
            if now - window_start >= 1.0:
                window_start, sent_in_window = now, 0
            nonce = parse_probe(data)
            if nonce is None or sent_in_window >= MAX_REPLIES_PER_SECOND:
                continue
            sent_in_window += 1
            reply = build_reply(nonce, self.hostname, self.tcp_port)
            assert len(reply) <= PROBE_MIN_SIZE          # resposta nunca maior que o pedido
            try:
                self._sock.sendto(reply, addr)
            except OSError:
                pass


def _broadcast_targets(addresses: Iterable[str], port: int) -> dict[str, list[tuple[str, int]]]:
    """Para cada endereço local, os destinos de broadcast a tentar (melhor esforço)."""
    targets: dict[str, list[tuple[str, int]]] = {}
    for address in addresses:
        dests = [("255.255.255.255", port)]
        octets = address.split(".")
        if len(octets) == 4:
            dests.append((".".join(octets[:3] + ["255"]), port))     # supõe máscara /24
        targets[address] = dests
    return targets


def discover_rx(
    *,
    port: int = DISCOVERY_PORT,
    timeout: float = 3.0,
    targets: list[tuple[str, int]] | None = None,
    log: Callable[[str], None] | None = None,
) -> list[RxAnnouncement]:
    """Procura RX na rede local. `targets` (usado em testes) substitui o broadcast."""
    nonce = secrets.token_hex(8)
    probe = build_probe(nonce)

    sockets: list[tuple[socket.socket, list[tuple[str, int]]]] = []
    try:
        if targets is not None:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind(("127.0.0.1", 0))
            sockets.append((sock, targets))
        else:
            for address, dests in _broadcast_targets(local_addresses(), port).items():
                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                    sock.bind((address, 0))      # força a saída por esta placa
                except OSError as exc:
                    if log:
                        log(f"Descoberta: ignorando {address}: {exc}")
                    continue
                sockets.append((sock, dests))

        found: dict[tuple[str, int], RxAnnouncement] = {}
        deadline = time.monotonic() + timeout
        send_times = [0.0, timeout * 0.25, timeout * 0.5]    # 3 envios: UDP pode perder pacote
        start = time.monotonic()
        while True:
            now = time.monotonic()
            if now >= deadline:
                break
            if send_times and now - start >= send_times[0]:
                send_times.pop(0)
                for sock, dests in sockets:
                    for dest in dests:
                        try:
                            sock.sendto(probe, dest)
                        except OSError:
                            pass
            if not sockets:
                break
            wait = max(0.0, min(0.1, deadline - now))
            ready, _, _ = select.select([s for s, _ in sockets], [], [], wait)
            for sock in ready:
                try:
                    data, addr = sock.recvfrom(MAX_DATAGRAM + 1)
                except OSError:
                    continue
                announcement = parse_reply(data, nonce, addr[0])
                if announcement:
                    found[(announcement.address, announcement.port)] = announcement
        return sorted(found.values(), key=lambda a: (a.hostname.lower(), a.address))
    finally:
        for sock, _ in sockets:
            sock.close()
