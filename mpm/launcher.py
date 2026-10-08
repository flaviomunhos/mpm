"""Menu simples para quem abre o mpm.exe com duplo clique (sem argumentos).

Só monta a linha de comando e chama `main`; nada aqui copia ou grava dados por conta própria.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable

def _banner() -> str:
    from mpm import __version__
    return ("=" * 52 + "\n"
            "            MUNHOS PC MIGRATOR  ·  MPM\n"
            f"                  versão {__version__}\n" + "=" * 52)


BANNER = _banner()
_BAD_NAME_CHARS = set('\\/:*?"<>|@[];,=+ ')


def _ask_name(ask: Callable[[str], str], out: Callable[[str], None], prompt: str) -> str | None:
    """Pede um nome de usuário do Windows; repete se inválido; vazio devolve None (volta ao menu)."""
    while True:
        name = ask(prompt).strip()
        if not name:
            out("  Informe um nome.")
            return None
        if len(name) > 20 or any(c in _BAD_NAME_CHARS for c in name):
            out("  Nome inválido: use até 20 caracteres, sem espaços nem \\ / : * ? \" < > | @ [ ] ; , = +")
            continue
        return name


def screen_clear() -> None:
    """Limpa a tela do console (Windows: cls; demais: sequência ANSI). Falha em silêncio."""
    try:
        if os.name == "nt":
            os.system("cls")
        elif sys.stdout.isatty():
            sys.stdout.write("\033[2J\033[H")
            sys.stdout.flush()
    except OSError:
        pass


def _new_pc_menu(ask: Callable[[str], str], out: Callable[[str], None],
                 clear: Callable[[], None]) -> list[str] | None:
    """Submenu do PC NOVO. None = voltar ao menu inicial."""
    notice = ""
    while True:
        clear()
        out("MPM · PC NOVO\n"
            "  1) Tudo de uma vez (recomendado)\n"
            "  2) Só o perfil e os arquivos\n"
            "  3) Só os programas\n"
            "  4) Só configurações e Wi-Fi\n"
            "  5) Meu perfil + Outlook (pessoal, sem programas)\n"
            "  v) Voltar")
        if notice:
            out(notice)
        choice = ask("Escolha [1]: ").strip().lower() or "1"
        if choice in ("v", "voltar"):
            return None
        notice = ""
        if choice in ("1", "2", "5"):
            name = _ask_name(ask, out, "Nome do usuário novo: ")
            if not name:
                continue
            args = ["rx", "--as-user", name]
            if ask("Administrador? [s/N] ").strip().lower() in ("s", "sim", "y", "yes"):
                args.append("--admin")
            if choice == "1":
                args.append("--full")
            elif choice == "5":
                args.append("--meu")
            return args
        if choice == "3":
            return ["rx", "--apps", "--install"]
        if choice == "4":
            name = _ask_name(ask, out, "Usuário que recebe (já com perfil): ")
            if not name:
                continue
            return ["rx", "--settings", "--to-user", name]
        notice = "  Opção inválida."


def choose_command(ask: Callable[[str], str] = input, out: Callable[[str], None] = print,
                   elevated: bool | None = None, clear: Callable[[], None] | None = None) -> list[str] | None:
    """Pergunta o que fazer e devolve os argumentos para `main` (None = sair).

    `clear` limpa a tela entre os menus (None = não limpa).
    """
    clear = clear or (lambda: None)
    notice = ""
    while True:
        clear()
        out(BANNER)
        if elevated is True:
            out("  Administrador: sim")
        elif elevated is False:
            out("\nATENÇÃO: não está como administrador. Feche e abra de novo com botão direito -> "
                "'Executar como administrador'\n(o PC novo precisa disso; no PC antigo ele é necessário para "
                "ler outros perfis e as senhas do Wi-Fi).")
        out("\nEste computador é:\n"
            "  1) o ANTIGO — de onde saem os dados (TX)\n"
            "  2) o NOVO   — para onde vão os dados (RX)\n"
            "  q) Sair\n"
            "  Dica: comece pelo NOVO; ele mostra um código que o ANTIGO vai pedir.")
        if notice:
            out(notice)
        choice = ask("Escolha: ").strip().lower()
        if choice in ("q", "sair", ""):
            return None
        if choice == "1":
            clear()
            return ["tx"]
        if choice == "2":
            args = _new_pc_menu(ask, out, clear)
            if args is not None:
                clear()
                return args
            notice = ""
            continue
        notice = "  Opção inválida."


def run_launcher(main: Callable[[list[str]], int], ask: Callable[[str], str] = input,
                 out: Callable[[str], None] = print, elevated: bool | None = None,
                 clear: Callable[[], None] | None = None) -> int:
    try:
        args = choose_command(ask, out, elevated, clear)
    except (EOFError, KeyboardInterrupt):
        return 0
    if args is None:
        return 0
    out("\nExecutando: mpm " + " ".join(args) + "\n")
    try:
        code = main(args)
    except KeyboardInterrupt:
        code = 130
    try:
        ask("\nPressione ENTER para fechar ")
    except (EOFError, KeyboardInterrupt):
        pass
    return code
