"""RX: instala na máquina nova os programas do relatório (0.4.1+).

Dois modos, escolhidos antes de instalar:
  express  instala tudo o que está marcado, sem perguntar nada (inclui as sugestões de ID que o winget confirmar);
  custom   mostra a lista numerada e deixa marcar/desmarcar cada item antes de começar.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mpm.core.apps import _covered, _norm, parse_winget_export

MODES = ("express", "custom")


def plan(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Itens que podem ser instalados: pacotes do winget e, depois, sugestões de ID para os sem pacote."""
    entries: list[dict[str, Any]] = []
    for pkg in report["groups"]["winget"]:
        entries.append({"id": pkg["id"], "label": pkg["id"], "version": pkg.get("version", ""),
                        "kind": "winget", "selected": bool(pkg.get("selected", True)),
                        "note": pkg.get("note", "")})
    for app in report["groups"]["unmatched"]:
        if app.get("candidate"):
            entries.append({"id": app["candidate"], "label": app["name"], "version": "",
                            "kind": "sugestão", "selected": False, "note": "ID provável, não confirmado"})
    return entries


def scan_local(backend: Any, log: Callable[[str], None] = print) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """O que ESTA máquina (RX) já tem: registro (Uninstall) e `winget export`. Falhas viram aviso, nunca erro."""
    log("\nVerificando o que esta máquina já tem instalado (pode levar cerca de 1 minuto) ...")
    registry: list[dict[str, Any]] = []
    packages: list[dict[str, str]] = []
    try:
        registry = backend.list_installed_apps()
    except (RuntimeError, OSError) as exc:
        log(f"  Aviso: não foi possível ler o registro desta máquina ({exc}).")
    try:
        packages = parse_winget_export(backend.winget_export())
    except Exception as exc:                    # winget ausente, tempo esgotado, JSON inválido ...
        log(f"  Aviso: não foi possível listar os pacotes do winget desta máquina ({exc}).")
    return registry, packages


def mark_present(entries: list[dict[str, Any]], registry: list[dict[str, Any]],
                 packages: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Marca como `present` (e desmarca) o que já está instalado aqui: mesmo ID no winget local ou mesmo
    programa no registro local. Não mexe no que o usuário marcou depois (isso é decidido no menu)."""
    by_id = {p["id"].casefold(): p["version"] for p in packages}
    named = [(a["name"], a.get("version", "")) for a in registry
             if a.get("name") and not a.get("system_component") and not a.get("is_update")]
    out = []
    for e in entries:
        version = by_id.get(e["id"].casefold())
        if version is None:
            wanted = [_norm(e["id"])]
            version = next((v for n, v in named if _covered(n, wanted)), None)
        if version is None:
            out.append(dict(e))
            continue
        shown = f" (versão aqui: {version})" if version and version != "Unknown" else ""
        out.append({**e, "present": True, "selected": False, "note": f"já instalado nesta máquina{shown}"})
    return out


def express(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Tudo o que falta aqui, menos o que o relatório desmarcou (driver de token, Bonjour). Sugestões entram: o ID é
    conferido na hora. O que esta máquina já tem fica de fora."""
    return [{**e, "selected": (e["kind"] == "sugestão" or e["selected"]) and not e.get("present")} for e in entries]


def parse_selection(text: str, count: int) -> list[int] | None:
    """'3, 5-8 12' -> [3, 5, 6, 7, 8, 12] (base 1). None se algo for inválido."""
    picked: list[int] = []
    for token in re.split(r"[,\s]+", text.strip()):
        if not token:
            continue
        match = re.fullmatch(r"(\d+)(?:-(\d+))?", token)
        if not match:
            return None
        start = int(match.group(1))
        end = int(match.group(2) or start)
        if not 1 <= start <= end <= count:
            return None
        picked.extend(range(start, end + 1))
    return picked


def only(entries: list[dict[str, Any]], pattern: str) -> list[dict[str, Any]]:
    """Marca somente os itens cujo ID ou nome casar com a expressão (sem diferenciar maiúsculas)."""
    rx = re.compile(pattern, re.I)
    return [{**e, "selected": bool(rx.search(e["id"]) or rx.search(e["label"]))} for e in entries]


# --- menu interativo (setas + espaço) ---------------------------------------------------------------

def _fit(text: str, width: int) -> str:
    return text if len(text) <= width else text[: max(1, width - 1)] + "…"


def _item_line(e: dict[str, Any]) -> str:
    mark = "x" if e["selected"] else " "
    extra = f"   ← {e['label']} (sugestão)" if e["kind"] == "sugestão" else (f"   {e['version']}" if e["version"] else "")
    return f"[{mark}] {e['id']}{extra}" + ("   ✓ já instalado" if e.get("present") else "")


def choose_tui(
    entries: list[dict[str, Any]],
    *,
    read_key: Callable[[], str],
    write: Callable[[str], None],
    size: Callable[[], tuple[int, int]],
    title: str = "MODO CUSTOM — escolha o que instalar nesta máquina",
    action: str = "instalar",
) -> list[dict[str, Any]] | None:
    """Menu de marcar itens: ↑↓ movem, ESPAÇO marca, ENTER instala, q cancela. A lista rola dentro da janela
    (cada quadro tem menos linhas que o terminal, então a tela nunca "sobe")."""
    items = [dict(e) for e in entries]
    defaults = [e["selected"] for e in items]
    cursor = top = 0
    message = ""
    write("\x1b[?25l\x1b[2J")
    try:
        while True:
            cols, lines = size()
            cols = max(cols, 20)
            rows = max(3, lines - 7)
            top = min(max(top, cursor - rows + 1), cursor) if cursor >= 0 else 0
            top = max(0, min(top, max(0, len(items) - rows)))
            body = []
            for index in range(top, min(top + rows, len(items))):
                text = _fit(("› " if index == cursor else "  ") + _item_line(items[index]), cols - 1)
                body.append(f"\x1b[7m{text}\x1b[0m" if index == cursor else text)
            current = items[cursor]
            marked = sum(e["selected"] for e in items)
            first, last = top + 1, min(top + rows, len(items))
            frame = [
                _fit(title, cols - 1),
                _fit(f"  ↑↓ mover   ESPAÇO marcar/desmarcar   ENTER {action}", cols - 1),
                _fit("  a todos   n nenhum   p padrão   q cancelar", cols - 1),
                *body,
                *[""] * (rows - len(body)),
                _fit(f"  {marked} de {len(items)} marcados  ·  itens {first}-{last}", cols - 1),
                _fit(f"  {current['note']}" if current["note"] and current["kind"] != "sugestão" else "", cols - 1),
                _fit(f"  {message}", cols - 1),
            ]
            write("\x1b[H" + "\n".join(line + "\x1b[K" for line in frame) + "\x1b[J")
            message = ""
            key = read_key()
            if key == "up":
                cursor = max(0, cursor - 1)
            elif key == "down":
                cursor = min(len(items) - 1, cursor + 1)
            elif key == "pgup":
                cursor = max(0, cursor - rows)
            elif key == "pgdn":
                cursor = min(len(items) - 1, cursor + rows)
            elif key == "home":
                cursor = 0
            elif key == "end":
                cursor = len(items) - 1
            elif key == "space":
                current["selected"] = not current["selected"]
            elif key == "a":
                for e in items:
                    e["selected"] = True
            elif key == "n":
                for e in items:
                    e["selected"] = False
            elif key == "p":
                for e, d in zip(items, defaults):
                    e["selected"] = d
            elif key == "enter":
                if any(e["selected"] for e in items):
                    return items
                message = "Nada marcado: use ESPAÇO para marcar algum item (ou q para cancelar)."
            elif key in ("q", "esc"):
                return None
    finally:
        write("\x1b[2J\x1b[H\x1b[?25h")


def _windows_console() -> tuple[Callable[[], str], Callable[[str], None], Callable[[], tuple[int, int]]] | None:
    """Teclado/tela do console do Windows (VT habilitado), ou None se não for um console interativo."""
    if sys.platform != "win32" or not (sys.stdin.isatty() and sys.stdout.isatty()):
        return None
    try:
        import ctypes
        import msvcrt
        kernel = ctypes.windll.kernel32
        handle = kernel.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not kernel.GetConsoleMode(handle, ctypes.byref(mode)) \
                or not kernel.SetConsoleMode(handle, mode.value | 0x0004):   # ENABLE_VIRTUAL_TERMINAL_PROCESSING
            return None
    except (OSError, AttributeError, ImportError):
        return None

    def read_key() -> str:
        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            return {"H": "up", "P": "down", "I": "pgup", "Q": "pgdn", "G": "home", "O": "end"}.get(msvcrt.getwch(), "")
        if ch == "\x03":
            raise KeyboardInterrupt
        return {"\r": "enter", " ": "space", "\x1b": "esc"}.get(ch, ch.lower())

    def write(text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()

    return read_key, write, lambda: tuple(shutil.get_terminal_size((80, 24)))  # type: ignore[return-value]


def _show(entries: list[dict[str, Any]], log: Callable[[str], None]) -> None:
    width = len(str(len(entries)))
    log("")
    for number, e in enumerate(entries, 1):
        mark = "x" if e["selected"] else " "
        extra = f"  ← {e['label']} (sugestão)" if e["kind"] == "sugestão" else (f"  {e['version']}" if e["version"] else "")
        log(f"  [{mark}] {number:>{width}}. {e['id']}{extra}")
        if e["note"] and e["kind"] != "sugestão":
            log(f"       {' ' * width}  {e['note']}")
    chosen = sum(e["selected"] for e in entries)
    log(f"\n  {chosen} de {len(entries)} marcados.")


def choose(entries: list[dict[str, Any]], *, ask: Callable[[str], str] = input,
           log: Callable[[str], None] = print, title: str = "MODO CUSTOM — escolha o que instalar nesta máquina",
           action: str = "instalar") -> list[dict[str, Any]] | None:
    """Modo Custom: lista numerada; números e faixas alternam a marca. Retorna os itens, ou None se cancelar."""
    console = None if ask is not input else _windows_console()
    if console is not None:
        read_key, write, size = console
        return choose_tui(entries, read_key=read_key, write=write, size=size, title=title, action=action)
    items = [dict(e) for e in entries]
    defaults = [e["selected"] for e in items]
    log("\n" + title)
    while True:
        _show(items, log)
        answer = ask("  Números/faixas alternam a marca (ex.: 3,5-8) | a = todos | n = nenhum | "
                     "p = padrão | ENTER = instalar | q = cancelar\n  > ").strip().lower()
        if answer in ("", "ok"):
            if any(e["selected"] for e in items):
                return items
            log("  Nada marcado.")
        elif answer == "q":
            return None
        elif answer == "a":
            for e in items:
                e["selected"] = True
        elif answer == "n":
            for e in items:
                e["selected"] = False
        elif answer == "p":
            for e, d in zip(items, defaults):
                e["selected"] = d
        else:
            picked = parse_selection(answer, len(items))
            if picked is None:
                log(f"  Não entendi '{answer}'. Use números de 1 a {len(items)}, faixas (3-6) ou a/n/p/q.")
                continue
            for number in picked:
                items[number - 1]["selected"] = not items[number - 1]["selected"]


def ask_mode(*, ask: Callable[[str], str] = input, log: Callable[[str], None] = print,
             title: str = "INSTALAÇÃO DOS PROGRAMAS NESTA MÁQUINA", verb: str = "instala",
             cancel: str = "Não instalar agora") -> str | None:
    log(f"\n{title}\n"
        f"  1) Express — {verb} tudo o que está marcado, sem perguntar nada\n"
        "  2) Custom  — você escolhe cada item da lista\n"
        f"  3) {cancel}")
    while True:
        answer = ask("  Escolha [1/2/3]: ").strip().lower()
        if answer in ("1", "express", "e"):
            return "express"
        if answer in ("2", "custom", "c"):
            return "custom"
        if answer in ("3", "q", "n", ""):
            return None


_FRIENDLY = (
    ("0x8A150011", "o instalador do winget não confere com o hash do pacote (pacote desatualizado)"),
    ("0x80190194", "o instalador não existe mais no endereço do pacote (erro 404)"),
    ("0x8A150056", "o instalador exige uma permissão que não foi concedida"),
)


def friendly_error(detail: str) -> str:
    """Troca o código do winget por uma explicação curta; mantém o texto original no fim."""
    for code, text in _FRIENDLY:
        if code in detail:
            return f"{text} [{code}]"
    return detail


def run_install(
    entries: list[dict[str, Any]],
    backend: Any,
    *,
    outdir: Path | None = None,
    dry_run: bool = False,
    pin_versions: bool = False,
    log: Callable[[str], None] = print,
) -> list[dict[str, Any]]:
    """Instala, em ordem, os itens marcados. Falha em um item não interrompe os demais."""
    todo = [e for e in entries if e["selected"]]
    results: list[dict[str, Any]] = []
    log(f"\nINSTALANDO {len(todo)} PROGRAMAS" + ("  [SIMULAÇÃO]" if dry_run else ""))

    for number, entry in enumerate(todo, 1):
        log(f"[{number}/{len(todo)}] {entry['id']}" + (f"  ({entry['label']})" if entry["kind"] == "sugestão" else ""))
        if dry_run:
            status, detail = "simulado", f"winget install --id {entry['id']}"
        else:
            try:
                if entry["kind"] == "sugestão" and not backend.winget_exists(entry["id"]):
                    status, detail = "indisponível", "ID não existe no winget"
                else:
                    version = entry["version"] if pin_versions and entry["kind"] == "winget" \
                        and entry["version"] not in ("", "Unknown") else None
                    log("    instalando (pode demorar e abrir pedido de permissão do Windows) ...")
                    status, detail = backend.winget_install(entry["id"], version)
            except (RuntimeError, OSError) as exc:
                status, detail = "falhou", str(exc)
        results.append({"id": entry["id"], "label": entry["label"], "kind": entry["kind"],
                        "status": status, "detail": detail})
        log(f"    → {status}" + (f": {detail}" if detail else ""))

    counts: dict[str, int] = {}
    for item in results:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    log("\nRESUMO: " + (", ".join(f"{n} {s}" for s, n in sorted(counts.items())) or "nada foi tentado"))
    manual = [i for i in results if i["status"] in ("falhou", "indisponível")]
    if manual:
        log("\nINSTALE À MÃO (baixe do site do fabricante):")
        for item in manual:
            name = item["label"] if item["kind"] == "sugestão" else item["id"]
            log(f"  ! {name}: {friendly_error(item['detail'])}")
    if outdir is not None:
        outdir.mkdir(parents=True, exist_ok=True)
        (outdir / "install-report.json").write_text(
            json.dumps({"dry_run": dry_run, "results": results}, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8")
        log(f"Relatório gravado em {outdir / 'install-report.json'}")
    return results
