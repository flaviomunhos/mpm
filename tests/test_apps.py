import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mpm.core.apps import _norm, build_report, import_json, parse_winget_export, render_text
from mpm.net.apps_inventory import run_apps_inventory
from mpm.net.rx import RxClient
from tests.helpers import make_profile
from tests.test_net import Pair, needs_crypto, quiet

SOURCE = {"Name": "winget", "Argument": "https://cdn.winget.microsoft.com/cache", "Type": "Microsoft.PreIndexed.Package"}


def app(name, scope="machine64", **extra):
    return {"name": name, "version": "1.0", "publisher": "", "scope": scope,
            "system_component": False, "is_update": False, **extra}


# Amostra baseada na saída real de `winget list` do TX (máquina real de teste, Windows 10).
REGISTRY = [
    app("7-Zip 24.01 (x64 edition)"), app("Notepad++ (64-bit x64)"), app("WinRAR 5.61 (64-bit)"),
    app("UltiMaker Cura 5.3.1"), app("UltiMaker Cura 5.4.0"),
    app("Mozilla Firefox (x64 pt-BR)"), app("WinSCP 6.5.6", scope="machine32"), app("XAMPP"),
    app("Python 3.14.8 (64-bit)", scope="user"),
    app("Microsoft Visual C++ 2015-2022 Redistributable (x64) - 14.50.35719"),
    app("Dell SupportAssist"), app("Intel(R) Management Engine Components"),
    app("Realtek Ethernet Controller Driver"), app("Samsung USB Driver for Mobile Phones"),
    app("Pacote de Driver do Windows - Fresco Logic (FLDUSB) USB"),
    app("Arduino IDE 2.3.10"), app("draw.io 28.2.5"), app("Java 8 Update 501 (64-bit)"),
    app("OpenVPN 2.6.7-I001 amd64"), app("PuTTY release 0.84 (64-bit)"), app("VLC media player"),
    app("Python Launcher"), app("Teams Machine-Wide Installer"), app("Mozilla Maintenance Service"),
    app("TAP-Windows 9.24.2"), app("Assistente de Atualização do Windows 10"),
    app("Assistente de Instalação do Windows 11"), app("Microsoft Teams Meeting Add-in for Microsoft Office"),
    app("Uninstall USM USB Display"), app("Google Chrome"), app("Brave"),
    app("Copilot"), app("\u00a0Assistente de\u00a0Instala\u00e7\u00e3o do Windows 11 "),
    app("McAfee Agent"), app("Warsaw 2.50.1.6 64 bits"), app("Microsoft 365 Apps for enterprise - pt-br"),
    app("Update for x64-based Systems (KB5001716)", is_update=True),
    app("", system_component=True),
]
EXPORT = {"Sources": [{"SourceDetails": SOURCE, "Packages": [
    {"PackageIdentifier": "7zip.7zip", "Version": "24.01.00.0"},
    {"PackageIdentifier": "Notepad++.Notepad++", "Version": "8.9.6.4"},
    {"PackageIdentifier": "RARLab.WinRAR", "Version": "5.61.0"},
    {"PackageIdentifier": "Ultimaker.Cura", "Version": "5.7.2"},
    {"PackageIdentifier": "Microsoft.VCRedist.2015+.x64", "Version": "14.50.35719.0"},
    {"PackageIdentifier": "Microsoft.DotNet.Runtime.8", "Version": "8.0.14"},
    {"PackageIdentifier": "Microsoft.Edge", "Version": "154.0.4258.53"},
    {"PackageIdentifier": "Python.Launcher", "Version": "3.14.7"},
    {"PackageIdentifier": "ArduinoSA.IDE.stable", "Version": "2.3.10"},
    {"PackageIdentifier": "JGraph.Draw", "Version": "28.2.5"},
    {"PackageIdentifier": "Oracle.JavaRuntimeEnvironment", "Version": "8.0.5010.8"},
    {"PackageIdentifier": "OpenVPNTechnologies.OpenVPN", "Version": "2.6.701"},
    {"PackageIdentifier": "PuTTY.PuTTY", "Version": "0.84.0.0"},
    {"PackageIdentifier": "VideoLAN.VLC", "Version": "3.0.24"},
    {"PackageIdentifier": "Microsoft.365Copilot", "Version": "19.2609.52011.0"},
    {"PackageIdentifier": "Dell.DisplayAndPeripheralManager", "Version": "2.1.1.12"},
]}]}


def names(group):
    return [i["name"] for i in group]


class ClassifyTest(unittest.TestCase):
    def setUp(self):
        self.report = build_report(REGISTRY, EXPORT)
        self.g = self.report["groups"]

    def test_norm(self):
        self.assertEqual(_norm("7-Zip 24.01 (x64 edition)"), "7zip")
        self.assertEqual(_norm("Notepad++ (64-bit x64)"), "notepad")

    def test_parse_export(self):
        self.assertEqual(len(parse_winget_export(EXPORT)), 16)
        self.assertEqual(parse_winget_export(None), [])

    def test_installable_by_winget(self):
        self.assertEqual({p["id"] for p in self.g["winget"]},
                         {"7zip.7zip", "Notepad++.Notepad++", "RARLab.WinRAR", "Ultimaker.Cura",
                          "ArduinoSA.IDE.stable", "JGraph.Draw", "Oracle.JavaRuntimeEnvironment",
                          "OpenVPNTechnologies.OpenVPN", "PuTTY.PuTTY", "VideoLAN.VLC"})
        self.assertTrue(all(p["selected"] for p in self.g["winget"]))

    def test_system_ids_and_names(self):
        self.assertTrue({"Microsoft.VCRedist.2015+.x64", "Microsoft.DotNet.Runtime.8",
                         "Microsoft.Edge", "Python.Launcher", "Microsoft.365Copilot"} <= set(names(self.g["system"])))
        for noise in ("Python Launcher", "Teams Machine-Wide Installer", "Mozilla Maintenance Service",
                      "TAP-Windows 9.24.2", "Assistente de Atualização do Windows 10",
                      "Assistente de Instalação do Windows 11",
                      "Microsoft Teams Meeting Add-in for Microsoft Office"):
            self.assertIn(noise, names(self.g["system"]))
        self.assertIn("Microsoft Visual C++ 2015-2022 Redistributable (x64) - 14.50.35719",
                      names(self.g["system"]))

    def test_oem(self):
        oem = names(self.g["oem"])
        for expected in ("Dell.DisplayAndPeripheralManager", "Dell SupportAssist", "Uninstall USM USB Display",
                         "Intel(R) Management Engine Components", "Realtek Ethernet Controller Driver",
                         "Samsung USB Driver for Mobile Phones"):
            self.assertIn(expected, oem)
        self.assertTrue(any("Fresco Logic" in n for n in oem))

    def test_manual_has_notes(self):
        manual = {i["name"]: i["note"] for i in self.g["manual"]}
        self.assertEqual(len(manual), 3)
        self.assertTrue(any("política" in n for n in manual.values()))
        self.assertTrue(any(n.startswith("Microsoft 365") for n in manual))

    def test_covered_registry_entries_are_not_duplicated(self):
        unmatched = names(self.g["unmatched"])
        for covered in ("7-Zip", "Notepad++", "WinRAR", "UltiMaker", "Arduino", "draw.io", "Java 8",
                        "OpenVPN", "PuTTY", "VLC"):
            self.assertFalse(any(covered in n for n in unmatched), covered)
        self.assertEqual(self.report["counts"]["covered_by_winget"], 11)  # 7zip, notepad, winrar, 2x cura + 6

    def test_unmatched_are_real_programs(self):
        self.assertEqual(sorted(names(self.g["unmatched"])),
                         ["Brave", "Google Chrome", "Mozilla Firefox (x64 pt-BR)", "Python 3.14.8 (64-bit)",
                          "WinSCP 6.5.6", "XAMPP"])

    def test_components_hidden(self):
        self.assertEqual(self.report["counts"]["components_hidden"], 2)

    def test_extra_ignore(self):
        report = build_report(REGISTRY, EXPORT, extra_ignore=[r"^xampp$"])
        self.assertNotIn("XAMPP", names(report["groups"]["unmatched"]))
        self.assertIn("XAMPP", names(report["groups"]["system"]))

    def test_without_winget_everything_is_registry_only(self):
        report = build_report(REGISTRY, None)
        self.assertEqual(report["groups"]["winget"], [])
        self.assertIn("7-Zip 24.01 (x64 edition)", names(report["groups"]["unmatched"]))

    def test_import_json_filters_and_keeps_source(self):
        out = import_json(EXPORT, {"7zip.7zip"})
        self.assertEqual(out["Sources"][0]["SourceDetails"]["Name"], "winget")
        self.assertEqual([p["PackageIdentifier"] for p in out["Sources"][0]["Packages"]], ["7zip.7zip"])
        self.assertEqual(len(EXPORT["Sources"][0]["Packages"]), 16)          # original intacto
        self.assertEqual(import_json(EXPORT, set())["Sources"], [])

    def test_render_text(self):
        text = render_text(self.report, show_all=True)
        for fragment in ("INVENTÁRIO", "7zip.7zip", "SEM PACOTE", "MANUAIS", "Dell SupportAssist"):
            self.assertIn(fragment, text)
        self.assertNotIn("Dell SupportAssist", render_text(self.report, show_all=False))


class FakeAppsBackend:
    def __init__(self, fail_winget=False):
        self.fail_winget = fail_winget

    def list_installed_apps(self):
        return REGISTRY

    def winget_export(self, timeout=300):
        if self.fail_winget:
            raise RuntimeError("winget não encontrado nesta máquina")
        return EXPORT


@needs_crypto
class AppsInventoryNetTest(unittest.TestCase):
    def run_inventory(self, backend, tmp, **kw):
        pair = Pair(make_profile(Path(tmp) / "src"))
        client = RxClient(pair.accept())
        try:
            with mock.patch("mpm.platforms.apps", return_value=backend):
                return run_apps_inventory(client, Path(tmp) / "out", log=quiet, **kw)
        finally:
            client.close()
            pair.finish()

    def test_end_to_end_writes_report_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = self.run_inventory(FakeAppsBackend(), tmp)
            out = Path(tmp) / "out"
            self.assertEqual(report["counts"]["winget"], 10)
            self.assertIn("7zip.7zip", (out / "apps-report.txt").read_text(encoding="utf-8"))
            imported = json.loads((out / "winget-import.json").read_text(encoding="utf-8"))
            ids = [p["PackageIdentifier"] for s in imported["Sources"] for p in s["Packages"]]
            self.assertEqual(len(ids), 10)
            self.assertIn("VideoLAN.VLC", ids)
            inventory = json.loads((out / "apps-inventory.json").read_text(encoding="utf-8"))
            self.assertEqual(len(inventory["registry"]), len(REGISTRY))

    def test_winget_failure_is_a_warning_not_an_error(self):
        logs = []
        with tempfile.TemporaryDirectory() as tmp:
            pair = Pair(make_profile(Path(tmp) / "src"))
            client = RxClient(pair.accept())
            try:
                with mock.patch("mpm.platforms.apps", return_value=FakeAppsBackend(fail_winget=True)):
                    report = run_apps_inventory(client, Path(tmp) / "out", log=logs.append)
            finally:
                client.close()
                pair.finish()
        self.assertEqual(report["counts"]["winget"], 0)
        self.assertTrue(any("winget" in line and "Aviso" in line for line in logs))



def _pkg(pid, version="1.0"):
    return {"PackageIdentifier": pid, "Version": version}


class FakeWinget:
    def __init__(self, missing=(), fail=(), already=()):
        self.missing, self.fail, self.already = set(missing), set(fail), set(already)
        self.installed: list[tuple[str, str | None]] = []
        self.checked: list[str] = []

    def winget_exists(self, pkg_id, timeout=120):
        self.checked.append(pkg_id)
        return pkg_id not in self.missing

    def winget_install(self, pkg_id, version=None, timeout=1800):
        self.installed.append((pkg_id, version))
        if pkg_id in self.fail:
            return "falhou", "código 0x80070005: acesso negado"
        if pkg_id in self.already:
            return "ja_instalado", "já instalado"
        return "instalado", ""


class InstallTest(unittest.TestCase):
    def setUp(self):
        export = {"Sources": [{"SourceDetails": SOURCE, "Packages": [
            _pkg("7zip.7zip", "24.01"), _pkg("VideoLAN.VLC", "3.0.24"), _pkg("AETEurope.SafeSignICMiniDriver"),
            _pkg("Apple.Bonjour")]}]}
        registry = [app("Google Chrome"), app("Brave"), app("XAMPP"), app("Peritus V 2.8.4")]
        self.report = build_report(registry, export)
        self.entries = None
        from mpm.net.apps_install import plan
        self.entries = plan(self.report)

    def ids(self, **flt):
        return [e["id"] for e in self.entries if all(e[k] == v for k, v in flt.items())]

    def test_plan_marks_and_suggests(self):
        self.assertEqual(self.ids(kind="winget", selected=True), ["7zip.7zip", "VideoLAN.VLC"])
        self.assertEqual(self.ids(kind="winget", selected=False),
                         ["AETEurope.SafeSignICMiniDriver", "Apple.Bonjour"])
        self.assertEqual(self.ids(kind="sugestão"), ["Brave.Brave", "Google.Chrome", "ApacheFriends.Xampp.8.2"])
        self.assertNotIn("Peritus", " ".join(e["label"] for e in self.entries))
        text = render_text(self.report)
        self.assertIn("[desmarcado:", text)
        self.assertIn("→ Google.Chrome", text)

    def run_it(self, backend, entries=None, **kw):
        from mpm.net.apps_install import run_install
        logs: list[str] = []
        results = run_install(self.entries if entries is None else entries, backend, log=logs.append, **kw)
        return results, logs

    def test_express_installs_marked_plus_confirmed_suggestions(self):
        from mpm.net.apps_install import express
        backend = FakeWinget(missing={"Brave.Brave"})
        entries = express(self.entries)
        self.assertEqual([e["id"] for e in entries if e["selected"]],
                         ["7zip.7zip", "VideoLAN.VLC", "Brave.Brave", "Google.Chrome", "ApacheFriends.Xampp.8.2"])
        results, _ = self.run_it(backend, entries)
        self.assertEqual([i for i, _v in backend.installed],
                         ["7zip.7zip", "VideoLAN.VLC", "Google.Chrome", "ApacheFriends.Xampp.8.2"])
        self.assertEqual({r["id"]: r["status"] for r in results}["Brave.Brave"], "indisponível")
        self.assertNotIn("Apple.Bonjour", [r["id"] for r in results])        # desmarcado segue de fora

    def test_unselected_entries_are_not_touched(self):
        backend = FakeWinget()
        results, _ = self.run_it(backend)                  # plano cru: só os marcados
        self.assertEqual([i for i, _v in backend.installed], ["7zip.7zip", "VideoLAN.VLC"])
        self.assertEqual(backend.checked, [])

    def test_dry_run_installs_nothing(self):
        from mpm.net.apps_install import express
        backend = FakeWinget()
        results, _ = self.run_it(backend, express(self.entries), dry_run=True)
        self.assertEqual(backend.installed, [])
        self.assertEqual({r["status"] for r in results}, {"simulado"})
        self.assertEqual(backend.checked, [])

    def test_pin_versions(self):
        backend = FakeWinget()
        self.run_it(backend, pin_versions=True)
        self.assertEqual(backend.installed, [("7zip.7zip", "24.01"), ("VideoLAN.VLC", "3.0.24")])

    def test_failures_reported_and_saved(self):
        backend = FakeWinget(fail={"VideoLAN.VLC"}, already={"7zip.7zip"})
        with tempfile.TemporaryDirectory() as tmp:
            results, logs = self.run_it(backend, outdir=Path(tmp))
            saved = json.loads((Path(tmp) / "install-report.json").read_text(encoding="utf-8"))
        status = {r["id"]: r["status"] for r in results}
        self.assertEqual((status["7zip.7zip"], status["VideoLAN.VLC"]), ("ja_instalado", "falhou"))
        self.assertEqual(saved["results"], results)
        self.assertTrue(any("! VideoLAN.VLC" in line and "0x80070005" in line for line in logs))

    def test_missing_winget_is_a_failure_not_a_crash(self):
        class NoWinget(FakeWinget):
            def winget_install(self, *a, **k):
                raise RuntimeError("winget não encontrado nesta máquina")
        results, _ = self.run_it(NoWinget())
        self.assertEqual({r["status"] for r in results}, {"falhou"})

    def test_parse_selection(self):
        from mpm.net.apps_install import parse_selection
        self.assertEqual(parse_selection("3, 5-7 9", 10), [3, 5, 6, 7, 9])
        for bad in ("0", "11", "7-5", "a", "2-", "1;2"):
            self.assertIsNone(parse_selection(bad, 10), bad)

    def choose_with(self, answers):
        from mpm.net.apps_install import choose
        queue = list(answers)
        logs: list[str] = []
        picked = choose(self.entries, ask=lambda _p: queue.pop(0), log=logs.append)
        return picked, logs

    def test_custom_toggle_all_none_default(self):
        picked, _ = self.choose_with(["n", "2 3", ""])                      # nenhum; liga SafeSign e Bonjour
        self.assertEqual([e["id"] for e in picked if e["selected"]],
                         ["AETEurope.SafeSignICMiniDriver", "Apple.Bonjour"])
        picked, _ = self.choose_with(["1", "5-7", "p", ""])                 # mexe e volta ao padrão
        self.assertEqual([e["id"] for e in picked if e["selected"]], ["7zip.7zip", "VideoLAN.VLC"])
        picked, _ = self.choose_with(["a", "1-4", ""])                      # todos; desliga os 4 primeiros
        self.assertEqual(sum(e["selected"] for e in picked), len(self.entries) - 4)
        self.assertEqual(self.entries[0]["selected"], True)                 # a lista original não é alterada

    def test_custom_invalid_input_and_cancel(self):
        picked, logs = self.choose_with(["99", "xx", "n", "", "q"])         # inválidos; nada marcado não avança
        self.assertIsNone(picked)
        self.assertEqual(sum("Não entendi" in line for line in logs), 2)
        self.assertTrue(any("Nada marcado" in line for line in logs))

    def test_ask_mode(self):
        from mpm.net.apps_install import ask_mode
        for answer, expected in (("1", "express"), ("Custom", "custom"), ("3", None), ("", None)):
            self.assertEqual(ask_mode(ask=lambda _p, a=answer: a, log=lambda _l: None), expected)
        queue = ["x", "2"]
        self.assertEqual(ask_mode(ask=lambda _p: queue.pop(0), log=lambda _l: None), "custom")


class MenuTest(unittest.TestCase):
    setUp = InstallTest.setUp
    """Menu interativo (setas/espaço) e --only; reaproveita a mesma amostra do InstallTest."""

    def drive(self, keys, size=(80, 14)):
        from mpm.net.apps_install import choose_tui
        queue = list(keys)
        frames: list[str] = []
        picked = choose_tui(self.entries, read_key=lambda: queue.pop(0), write=frames.append, size=lambda: size)
        self.assertEqual(queue, [])                      # consumiu exatamente as teclas dadas
        return picked, frames

    def test_arrows_space_enter(self):
        # padrão: 7zip e VLC marcados. Desce 1 (SafeSign), marca; desce 1 (Bonjour), marca; ENTER.
        picked, _ = self.drive(["down", "space", "down", "space", "enter"])
        self.assertEqual([e["id"] for e in picked if e["selected"]],
                         ["7zip.7zip", "AETEurope.SafeSignICMiniDriver", "Apple.Bonjour", "VideoLAN.VLC"])
        self.assertTrue(self.entries[0]["selected"] and not self.entries[1]["selected"])   # original intacto

    def test_keys_all_none_default_home_end_cancel(self):
        picked, _ = self.drive(["n", "space", "end", "space", "enter"])      # nenhum; marca o 1º e o último
        self.assertEqual([e["id"] for e in picked if e["selected"]], ["7zip.7zip", "ApacheFriends.Xampp.8.2"])
        picked, _ = self.drive(["a", "n", "p", "enter"])
        self.assertEqual([e["id"] for e in picked if e["selected"]], ["7zip.7zip", "VideoLAN.VLC"])
        picked, _ = self.drive(["up", "pgdn", "pgup", "home", "esc"])        # limites sem erro; cancela
        self.assertIsNone(picked)
        picked, _ = self.drive(["q"])
        self.assertIsNone(picked)

    def test_enter_with_nothing_marked_stays(self):
        picked, frames = self.drive(["n", "enter", "space", "enter"])
        self.assertEqual([e["id"] for e in picked if e["selected"]], ["7zip.7zip"])
        self.assertTrue(any("Nada marcado" in f for f in frames))

    def test_frames_always_fit_the_window(self):
        import re as _re
        lines, cols = 12, 50
        _, frames = self.drive(["pgdn", "end", "up", "down", "home", "enter"], size=(cols, lines))
        draws = [f for f in frames if f.startswith("\x1b[H")]
        self.assertGreater(len(draws), 4)
        for frame in draws:
            plain = _re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", frame)
            rows = plain.split("\n")
            self.assertLessEqual(len(rows), lines - 1)
            self.assertTrue(all(len(r) <= cols - 1 for r in rows), max(map(len, rows)))
        # o item sob o cursor sempre aparece, inclusive depois de "end" numa janela pequena
        last = _re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", draws[2])
        self.assertIn("ApacheFriends.Xampp.8.2", last)

    def test_only_filters_by_id_or_label(self):
        from mpm.net.apps_install import express, only
        self.assertEqual([e["id"] for e in only(self.entries, "7zip") if e["selected"]], ["7zip.7zip"])
        self.assertEqual([e["id"] for e in only(self.entries, "chrome") if e["selected"]], ["Google.Chrome"])
        picked = only(express(self.entries), "^(vlc|videolan)")
        self.assertEqual([e["id"] for e in picked if e["selected"]], ["VideoLAN.VLC"])
        self.assertEqual([e["id"] for e in only(self.entries, "nao-existe") if e["selected"]], [])



class PresentTest(unittest.TestCase):
    def setUp(self):
        InstallTest.setUp(self)

    def local(self, registry=(), packages=()):
        from mpm.net.apps_install import mark_present
        return mark_present(self.entries, list(registry), list(packages))

    def present_ids(self, entries):
        return [e["id"] for e in entries if e.get("present")]

    def test_matches_by_winget_id_and_by_registry_name(self):
        entries = self.local(
            registry=[app("7-Zip 26.03 (x64 edition)", version="26.03"), app("Google Chrome"),
                      app("Lenovo System Update"), app("Python Launcher"), app("Microsoft Edge")],
            packages=[{"id": "videolan.vlc", "version": "3.0.99", "source": "winget"}])
        self.assertEqual(sorted(self.present_ids(entries)), ["7zip.7zip", "Google.Chrome", "VideoLAN.VLC"])
        by_id = {e["id"]: e for e in entries}
        self.assertFalse(by_id["7zip.7zip"]["selected"])
        self.assertIn("26.03", by_id["7zip.7zip"]["note"])
        self.assertIn("3.0.99", by_id["VideoLAN.VLC"]["note"])
        self.assertTrue(all(not e.get("present") for e in entries if e["id"] in ("Brave.Brave", "ApacheFriends.Xampp.8.2")))

    def test_ignores_system_components_and_updates(self):
        hidden = [app("7-Zip 26.03", system_component=True), app("Update for 7-Zip", is_update=True)]
        self.assertEqual(self.present_ids(self.local(registry=hidden)), [])

    def test_express_and_menu_skip_present(self):
        from mpm.net.apps_install import express
        entries = self.local(registry=[app("7-Zip 26.03"), app("Google Chrome")])
        chosen = [e["id"] for e in express(entries) if e["selected"]]
        self.assertEqual(chosen, ["VideoLAN.VLC", "Brave.Brave", "ApacheFriends.Xampp.8.2"])
        line = __import__("mpm.net.apps_install", fromlist=["_item_line"])._item_line(entries[0])
        self.assertIn("✓ já instalado", line)

    def test_scan_local_survives_failures(self):
        from mpm.net.apps_install import scan_local

        class Broken:
            def list_installed_apps(self):
                raise RuntimeError("sem registro")

            def winget_export(self, timeout=300):
                raise RuntimeError("winget não encontrado")

        logs: list[str] = []
        self.assertEqual(scan_local(Broken(), logs.append), ([], []))
        self.assertEqual(sum("Aviso" in line for line in logs), 2)

        class Fine:
            def list_installed_apps(self):
                return [app("7-Zip 26.03")]

            def winget_export(self, timeout=300):
                return {"Sources": [{"SourceDetails": SOURCE, "Packages": [_pkg("7zip.7zip", "26.03")]}]}

        registry, packages = scan_local(Fine(), lambda _l: None)
        self.assertEqual((len(registry), packages[0]["id"]), (1, "7zip.7zip"))


if __name__ == "__main__":
    unittest.main()
