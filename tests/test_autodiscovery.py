import contextlib
import io
import socket
import tempfile
import threading
from pathlib import Path
import time
import unittest
from argparse import Namespace
from unittest import mock

from mpm import cli
from mpm.net import discovery
from mpm.net.discovery import (
    PROBE_MIN_SIZE,
    DiscoveryResponder,
    RxAnnouncement,
    build_probe,
    build_reply,
    discover_rx,
    parse_probe,
    parse_reply,
)
from mpm.net.rx import RxListener
from mpm.net.security import AuthError
from mpm.net.tx import run_tx

from tests.helpers import make_profile

try:
    import cryptography  # noqa: F401
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False

needs_crypto = unittest.skipUnless(HAS_CRYPTO, "pacote 'cryptography' não instalado")
quiet = lambda _msg: None  # noqa: E731
NONCE = "0123456789abcdef"


def responder(hostname, tcp_port):
    return DiscoveryResponder(tcp_port, host="127.0.0.1", udp_port=0, hostname=hostname).start()


def find(*responders, timeout=1.0):
    targets = [("127.0.0.1", r.udp_port) for r in responders]
    return discover_rx(targets=targets, timeout=timeout)


class MessageTest(unittest.TestCase):
    def test_probe_roundtrip_and_minimum_size(self):
        probe = build_probe(NONCE)
        self.assertGreaterEqual(len(probe), PROBE_MIN_SIZE)
        self.assertEqual(parse_probe(probe), NONCE)

    def test_short_or_garbage_probes_are_rejected(self):
        self.assertIsNone(parse_probe(build_probe(NONCE)[:100]))
        self.assertIsNone(parse_probe(b"x" * 300))
        self.assertIsNone(parse_probe(b'{"magic":"outro"}'.ljust(300)))
        self.assertIsNone(parse_probe(b"\xff\xfe" * 200))

    def test_reply_is_never_larger_than_the_probe(self):
        reply = build_reply(NONCE, "H" * 500, 47800)
        self.assertLessEqual(len(reply), len(build_probe(NONCE)))

    def test_reply_parsing(self):
        ok = parse_reply(build_reply(NONCE, "NOTE-01", 47800), NONCE, "10.0.0.5")
        self.assertEqual(ok, RxAnnouncement("NOTE-01", "10.0.0.5", 47800, ok.mpm_version))
        self.assertIsNone(parse_reply(build_reply("outrononce00000", "X", 47800), NONCE, "10.0.0.5"))
        self.assertIsNone(parse_reply(build_reply(NONCE, "X", 0), NONCE, "10.0.0.5"))
        self.assertIsNone(parse_reply(build_reply(NONCE, "X", 70000), NONCE, "10.0.0.5"))
        self.assertIsNone(parse_reply(b"lixo", NONCE, "10.0.0.5"))

    def test_hostname_from_the_network_is_sanitized(self):
        ann = parse_reply(build_reply(NONCE, "A\x1b[31mB\nC", 47800), NONCE, "10.0.0.5")
        self.assertTrue(ann.hostname.isprintable())
        self.assertNotIn("\x1b", ann.hostname)


class ResponderTest(unittest.TestCase):
    def test_finds_one_rx(self):
        r = responder("RX-A", 47801)
        self.addCleanup(r.stop)
        found = find(r)
        self.assertEqual(len(found), 1)
        self.assertEqual((found[0].hostname, found[0].address, found[0].port),
                         ("RX-A", "127.0.0.1", 47801))

    def test_finds_several_rx_sorted_by_hostname(self):
        a, b = responder("ZEBRA", 47802), responder("alfa", 47803)
        self.addCleanup(a.stop)
        self.addCleanup(b.stop)
        found = find(a, b)
        self.assertEqual([f.hostname for f in found], ["alfa", "ZEBRA"])
        self.assertEqual([f.port for f in found], [47803, 47802])

    def test_nobody_answers(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        self.addCleanup(sock.close)
        found = discover_rx(targets=[("127.0.0.1", sock.getsockname()[1])], timeout=0.5)
        self.assertEqual(found, [])

    def test_stopped_responder_is_not_found(self):
        r = responder("RX-A", 47801)
        port = r.udp_port
        r.stop()
        self.assertEqual(discover_rx(targets=[("127.0.0.1", port)], timeout=0.5), [])

    def _raw_exchange(self, r, datagrams, wait=0.6):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        sock.settimeout(wait)
        self.addCleanup(sock.close)
        for data in datagrams:
            sock.sendto(data, ("127.0.0.1", r.udp_port))
        replies = []
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            try:
                replies.append(sock.recvfrom(2048)[0])
            except TimeoutError:
                break
        return replies

    def test_ignores_short_and_malformed_probes(self):
        r = responder("RX-A", 47801)
        self.addCleanup(r.stop)
        replies = self._raw_exchange(r, [build_probe(NONCE)[:100], b"oi", b"\x00" * 300])
        self.assertEqual(replies, [])

    def test_valid_probe_gets_a_small_reply_without_secrets(self):
        r = responder("RX-A", 47801)
        self.addCleanup(r.stop)
        probe = build_probe(NONCE)
        (reply,) = self._raw_exchange(r, [probe])
        self.assertLessEqual(len(reply), len(probe))
        text = reply.decode()
        for forbidden in ("code", "codigo", "fingerprint", "hmac"):
            self.assertNotIn(forbidden, text.lower())

    def test_reply_rate_is_limited(self):
        r = responder("RX-A", 47801)
        self.addCleanup(r.stop)
        replies = self._raw_exchange(r, [build_probe(NONCE)] * 100, wait=0.8)
        self.assertLessEqual(len(replies), discovery.MAX_REPLIES_PER_SECOND)
        self.assertGreater(len(replies), 0)


class CliFlowTest(unittest.TestCase):
    A = RxAnnouncement("NOTE-A", "10.0.0.1", 47800, "0.2.1")
    B = RxAnnouncement("NOTE-B", "10.0.0.2", 47800, "0.2.1")

    def run_tx_cmd(self, *, rx=None, code=None, inputs=(), found=(), tx_side_effect=None):
        args = Namespace(rx=rx, code=code, profile_path=None)
        out, err = io.StringIO(), io.StringIO()
        run = mock.Mock(side_effect=tx_side_effect)
        with mock.patch("mpm.net.discovery.discover_rx", side_effect=list(found)) as disc, \
                mock.patch("mpm.net.tx.run_tx", run), \
                mock.patch("builtins.input", side_effect=list(inputs)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.cmd_tx(args)
        return rc, run, disc, out.getvalue()

    def test_explicit_rx_and_code_skip_discovery(self):
        rc, run, disc, _ = self.run_tx_cmd(rx="192.168.0.9:5000", code="ABCD-EFGH")
        self.assertEqual(rc, 0)
        disc.assert_not_called()
        self.assertEqual(run.call_args.args[:3], ("192.168.0.9", 5000, "ABCD-EFGH"))

    def test_single_rx_goes_straight_to_the_code_prompt(self):
        rc, run, _, out = self.run_tx_cmd(inputs=["ABCD-EFGH"], found=[[self.A]])
        self.assertEqual(rc, 0)
        self.assertIn("NOTE-A", out)
        self.assertEqual(run.call_args.args[:3], ("10.0.0.1", 47800, "ABCD-EFGH"))

    def test_several_rx_show_a_list_then_ask_the_code(self):
        rc, run, _, out = self.run_tx_cmd(inputs=["2", "WXYZ-2345"], found=[[self.A, self.B]])
        self.assertEqual(rc, 0)
        self.assertIn("NOTE-A", out)
        self.assertIn("NOTE-B", out)
        self.assertEqual(run.call_args.args[:3], ("10.0.0.2", 47800, "WXYZ-2345"))

    def test_invalid_choice_is_asked_again_and_r_rescans(self):
        rc, run, disc, _ = self.run_tx_cmd(
            inputs=["9", "r", "1", "ABCD-EFGH"], found=[[self.A, self.B], [self.A, self.B]])
        self.assertEqual(rc, 0)
        self.assertEqual(disc.call_count, 2)
        self.assertEqual(run.call_args.args[0], "10.0.0.1")

    def test_quit_from_the_list(self):
        rc, run, _, _ = self.run_tx_cmd(inputs=["q"], found=[[self.A, self.B]])
        self.assertEqual(rc, 4)
        run.assert_not_called()

    def test_nobody_found_then_manual_address(self):
        rc, run, _, _ = self.run_tx_cmd(inputs=["192.168.0.50", "ABCD-EFGH"], found=[[]])
        self.assertEqual(rc, 0)
        self.assertEqual(run.call_args.args[:2], ("192.168.0.50", 47800))

    def test_wrong_code_is_asked_again_up_to_three_times(self):
        rc, run, _, _ = self.run_tx_cmd(
            inputs=["AAAA-AAAA", "BBBB-BBBB", "CCCC-CCCC"], found=[[self.A]],
            tx_side_effect=[AuthError("código incorreto"), AuthError("código incorreto"), None])
        self.assertEqual(rc, 0)
        self.assertEqual(run.call_count, 3)

    def test_three_wrong_codes_give_up(self):
        rc, run, _, _ = self.run_tx_cmd(
            inputs=["A", "B", "C"], found=[[self.A]], tx_side_effect=AuthError("x"))
        self.assertEqual(rc, 3)
        self.assertEqual(run.call_count, 3)

    def test_code_from_the_command_line_is_not_retried(self):
        rc, run, _, _ = self.run_tx_cmd(
            code="AAAA-AAAA", found=[[self.A]], tx_side_effect=AuthError("x"))
        self.assertEqual(rc, 3)
        self.assertEqual(run.call_count, 1)


@needs_crypto
class EndToEndTest(unittest.TestCase):
    def test_discover_then_pair_then_serve(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        profile = make_profile(Path(tmp.name))
        listener = RxListener(host="127.0.0.1", port=0)
        r = responder("RX-LAB", listener.port)
        self.addCleanup(listener.close)
        self.addCleanup(r.stop)

        (rx,) = find(r)
        self.assertEqual(rx.port, listener.port)

        errors = []

        def tx():
            try:
                run_tx(rx.address, rx.port, listener.code, profile=profile, log=quiet)
            except Exception as exc:     # noqa: BLE001
                errors.append(exc)

        thread = threading.Thread(target=tx, daemon=True)
        thread.start()
        conn = listener.accept_authenticated(timeout=10, log=quiet)
        from mpm.net.rx import RxClient
        client = RxClient(conn)
        self.assertTrue(client.request({"op": "info"})["ok"])
        client.close()
        thread.join(10)
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
