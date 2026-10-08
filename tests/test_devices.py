import io
import itertools
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mpm.core.devices import parse_wifi_xml, render_devices, wifi_entries
from mpm.net.rx import RemoteError, RxClient
from mpm.net.settings import Backends, run_settings
from tests.helpers import make_profile
from tests.test_appdata import make_roots
from tests.test_net import Pair, needs_crypto
from tests.test_settings import FakeAccounts, FakeRxReg, FakeTxAppdata, FakeTxReg

SECRET = "segredo-wifi-9731"


def wifi_xml(name, auth="WPA2PSK", key=SECRET, onex=False):
    sec = f"<authEncryption><authentication>{auth}</authentication><encryption>AES</encryption></authEncryption>"
    if key:
        sec += f"<sharedKey><keyType>passPhrase</keyType><protected>false</protected><keyMaterial>{key}</keyMaterial></sharedKey>"
    if onex:
        sec += "<OneX xmlns=\"http://www.microsoft.com/networking/OneX/v1\"><authMode>user</authMode></OneX>"
    return ("<?xml version=\"1.0\"?><WLANProfile xmlns=\"http://www.microsoft.com/networking/WLAN/profile/v1\">"
            f"<name>{name}</name><MSM><security>{sec}</security></MSM><connectionMode>auto</connectionMode></WLANProfile>")


class FakeTxDevices:
    def __init__(self, elevated=True):
        self.elevated = elevated

    def is_elevated(self):
        return self.elevated

    def export_wifi_xml(self):
        return [wifi_xml("Casa"), wifi_xml("Aberta", auth="open", key=""), wifi_xml("Corp", auth="WPA2", key="", onex=True),
                wifi_xml("SemSenha", key="")]


class FakeRxDevices:
    def __init__(self, existing_wifi=()):
        self.wifi = set(existing_wifi)
        self.imported = []

    def existing_wifi_names(self):
        return {n.casefold() for n in self.wifi}

    def add_wifi_profile(self, xml):
        self.imported.append(parse_wifi_xml(xml)["name"])
        return None


class CoreDevicesTest(unittest.TestCase):
    def test_parse_wifi_xml(self):
        info = parse_wifi_xml(wifi_xml("Casa"))
        self.assertEqual((info["name"], info["auth"], info["has_key"], info["enterprise"], info["open"]),
                         ("Casa", "WPA2PSK", True, False, False))
        self.assertNotIn(SECRET, json.dumps(info))                      # o resumo nunca leva a senha
        self.assertTrue(parse_wifi_xml(wifi_xml("A", auth="open", key=""))["open"])
        self.assertTrue(parse_wifi_xml(wifi_xml("C", auth="WPA2", key="", onex=True))["enterprise"])
        self.assertFalse(parse_wifi_xml(wifi_xml("S", key=""))["has_key"])

    def test_parse_wifi_xml_rejects_bad_input(self):
        for bad in ("<WLANProfile", "", wifi_xml("X").replace("<name>X</name>", ""),
                    "<!DOCTYPE a [<!ENTITY x 'y'>]>" + wifi_xml("X")):
            with self.assertRaises(ValueError, msg=bad[:30]):
                parse_wifi_xml(bad)

    def test_entries_and_render(self):
        wifi = [parse_wifi_xml(x) for x in FakeTxDevices().export_wifi_xml()]
        selected = {e["label"]: e["selected"] for e in wifi_entries(wifi)}
        self.assertEqual(selected, {"wifi:Casa": True, "wifi:Aberta": True, "wifi:Corp": True, "wifi:SemSenha": False})
        text = render_devices(wifi, elevated=False)
        self.assertIn("Casa", text)
        self.assertIn("não está como administrador", text)
        self.assertNotIn(SECRET, text)


@needs_crypto
class DevicesNetTest(unittest.TestCase):
    counter = itertools.count()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.tx_roots = make_roots(str(base / "tx"))
        self.profile = base / "rx" / "maria"
        self.profile.mkdir(parents=True)
        self.outdir = base / "out"
        self.logs: list[str] = []
        self.rx = FakeRxDevices()

    def run_it(self, *, tx_dev=None, rx_dev=None, **kw):
        rx_dev = rx_dev or self.rx
        backends = Backends(FakeAccounts(self.profile), SimpleNamespace(roots=lambda: {}), FakeRxReg(), rx_dev)
        pair = Pair(make_profile(Path(self.tmp.name) / f"src{next(self.counter)}"))
        client = RxClient(pair.accept())
        try:
            with mock.patch("mpm.platforms.appdata", return_value=FakeTxAppdata(self.tx_roots)), \
                    mock.patch("mpm.platforms.regtools", return_value=FakeTxReg()), \
                    mock.patch("mpm.platforms.devices", return_value=tx_dev or FakeTxDevices()):
                return run_settings(client, [], backends, outdir=self.outdir, retry_wait=0, log=self.logs.append,
                                    progress_stream=io.StringIO(), to_user="maria", **kw)
        finally:
            client.close()
            pair.finish()

    def test_express_imports_only_good_wifi(self):
        summary = self.run_it(mode="express")
        self.assertTrue(summary["ok"], self.logs)
        self.assertEqual(sorted(self.rx.imported), ["Aberta", "Casa", "Corp"])          # SemSenha vem desmarcada
        self.assertEqual({r["name"]: r["status"] for r in summary["wifi"]},
                         {"Casa": "importado", "Aberta": "importado", "Corp": "importado"})
        self.assertNotIn("printers", summary)

    def test_secret_never_reaches_reports_or_logs(self):
        self.run_it(mode="express")
        self.assertFalse(any(SECRET in line for line in self.logs))
        for path in self.outdir.rglob("*"):
            if path.is_file():
                self.assertNotIn(SECRET.encode(), path.read_bytes(), str(path))

    def test_existing_items_are_not_touched(self):
        self.rx = FakeRxDevices(existing_wifi=["CASA"])
        summary = self.run_it(mode="express")
        self.assertEqual(sorted(self.rx.imported), ["Aberta", "Corp"])
        self.assertEqual({r["name"]: r["status"] for r in summary["wifi"]}["Casa"], "ja_existe")

    def test_only_devices(self):
        summary = self.run_it(mode="express", only="^wifi:Casa$")
        self.assertEqual((summary["programs"], self.rx.imported), ([], ["Casa"]))

    def test_dry_run(self):
        summary = self.run_it(mode="express", dry_run=True)
        self.assertEqual(self.rx.imported, [])
        self.assertTrue(summary["wifi"] and all(r["status"] == "simulado" for r in summary["wifi"]))

    def test_tx_export_wifi_returns_only_requested_and_validates(self):
        pair = Pair(make_profile(Path(self.tmp.name) / f"src{next(self.counter)}"))
        client = RxClient(pair.accept())
        try:
            with mock.patch("mpm.platforms.devices", return_value=FakeTxDevices()):
                reply = client.request({"op": "export_wifi", "names": ["casa"]})
                self.assertEqual([p["name"] for p in reply["profiles"]], ["Casa"])
                for bad in ([], "Casa", [1], ["a"] * 300):
                    with self.assertRaises(RemoteError):
                        client.request({"op": "export_wifi", "names": bad})
        finally:
            client.close()
            pair.finish()


if __name__ == "__main__":
    unittest.main()
