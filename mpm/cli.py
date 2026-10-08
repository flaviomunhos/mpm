"""Interface de linha de comando do MPM."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from mpm import __version__
from mpm.core.filesystem import discover_appdata, discover_folders
from mpm.core.fmt import fmt_bytes
from mpm.core.manifest import Manifest, build_manifest
from mpm.core.models import ProfileInfo
from mpm.core.profiles import ProfileNotFound, list_profiles, select_profile
from mpm.core.system import discover_system
from mpm.core.user import discover_user
from mpm.net.discovery import DiscoveryResponder, RxAnnouncement
from mpm.net.protocol import DEFAULT_PORT

BANNER = """\
===================================
       MUNHOS PC MIGRATOR
             MPM
===================================
"""


def _print_rows(title: str, rows: list[tuple[str, object]]) -> None:
    print(f"[DISCOVERY] {title}\n")
    width = max(len(k) for k, _ in rows)
    for key, value in rows:
        print(f"{key:<{width}} : {'-' if value in (None, '') else value}")
    print()


def _add_profile_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--user", help="Perfil a migrar (nome ou SID). Padrão: usuário atual")
    p.add_argument("--profile-path", help="Trata este diretório como raiz de um perfil "
                                          "(ex.: disco de outro PC montado)")


def _resolve_profile(args: argparse.Namespace) -> ProfileInfo:
    if getattr(args, "profile_path", None):
        path = Path(args.profile_path)
        if not path.is_dir():
            raise ProfileNotFound(f"Diretório inexistente: {path}")
        return ProfileInfo(username=path.name, sid="", path=str(path), is_current=False)

    profile = select_profile(getattr(args, "user", None))
    if not profile.is_current and not discover_user().is_elevated:
        print("Aviso: lendo o perfil de outro usuário sem privilégio elevado; "
              "algumas pastas podem falhar.", file=sys.stderr)
    return profile


def cmd_discover(args: argparse.Namespace) -> int:
    system = discover_system()
    user = discover_user()

    if args.json:
        print(json.dumps({"system": system.to_dict(), "user": user.to_dict()},
                         indent=2, ensure_ascii=False))
        return 0

    print(BANNER)
    _print_rows("Sistema", [
        ("Hostname", system.hostname),
        ("Sistema", system.os_name),
        ("Família", system.os_family),
        ("Release", system.os_release),
        ("Build/Kernel", system.os_build),
        ("Arquitetura", system.architecture),
        ("Python", system.python_version),
        ("Coletado em", system.collected_at),
    ])
    _print_rows("Usuário", [
        ("Usuário", user.username),
        ("ID (UID/SID)", user.user_id),
        ("Grupo", user.group_id),
        ("Perfil", user.profile_path),
        ("Domínio/Host", user.domain),
        ("Em domínio", user.domain_joined),
        ("Shell", user.shell),
        ("Elevado", user.is_elevated),
    ])
    return 0


def cmd_profiles(args: argparse.Namespace) -> int:
    profiles = list_profiles()
    if args.json:
        print(json.dumps([p.to_dict() for p in profiles], indent=2, ensure_ascii=False))
        return 0
    print(f"[DISCOVERY] Perfis ({len(profiles)})\n")
    for p in profiles:
        mark = "*" if p.is_current else " "
        print(f" {mark} {p.username:<20} {p.path:<32} {p.sid}")
    print("\n* = usuário atual")
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    profile = _resolve_profile(args)
    print(f"Varrendo perfil {profile.username} ({profile.path}) ...", file=sys.stderr)
    folders = discover_folders(profile)
    appdata = {} if args.no_appdata else discover_appdata(profile)

    if args.json:
        print(json.dumps({
            "profile": profile.to_dict(),
            "folders": {k: v.to_dict() for k, v in folders.items()},
            "appdata": appdata,
        }, indent=2, ensure_ascii=False))
        return 0

    print(BANNER)
    print(f"[DISCOVERY] Pastas do perfil {profile.username}\n")
    total_files = total_bytes = 0
    for scan in folders.values():
        flags = []
        if not scan.exists:
            flags.append("NÃO EXISTE")
        if scan.redirected:
            flags.append("redirecionada")
        if scan.cloud_placeholders:
            flags.append(f"{scan.cloud_placeholders} só-na-nuvem")
        if scan.skipped_count:
            flags.append(f"{scan.skipped_count} links ignorados")
        if scan.error_count:
            flags.append(f"{scan.error_count} erros")
        total_files += scan.file_count
        total_bytes += scan.total_bytes
        print(f"{scan.key:<10} {scan.file_count:>8} arquivos {fmt_bytes(scan.total_bytes):>10}  "
              f"{scan.path}" + (f"  [{', '.join(flags)}]" if flags else ""))
    print(f"{'TOTAL':<10} {total_files:>8} arquivos {fmt_bytes(total_bytes):>10}\n")

    if appdata:
        print("[DISCOVERY] AppData (inventário; os 8 maiores por área)\n")
        for area, summary in appdata.items():
            entries = summary["entries"]
            area_bytes = sum(e["total_bytes"] for e in entries)
            state = "" if summary["exists"] else "  (não existe)"
            print(f"{area}: {len(entries)} pastas, {fmt_bytes(area_bytes)}{state}")
            for e in entries[:8]:
                print(f"    {e['name']:<36} {e['file_count']:>8} arq. {fmt_bytes(e['total_bytes']):>10}")
        print()
    return 0


def cmd_manifest(args: argparse.Namespace) -> int:
    profile = _resolve_profile(args)
    inventory = None
    if args.output:
        out = Path(args.output)
        inventory = out.with_name(f"{out.stem}.inventory.jsonl")
    print(f"Varrendo perfil {profile.username} ({profile.path}) ...", file=sys.stderr)
    manifest = build_manifest(
        profile=profile,
        include_appdata=not args.no_appdata,
        inventory_path=inventory,
    )
    if args.output:
        path = manifest.write(args.output)
        print(f"Manifesto {manifest.migration_id} gravado em {path}")
        if inventory:
            print(f"Inventário gravado em {inventory}")
    else:
        print(manifest.to_json())
    return 0


def _ask_yes_no(question: str) -> bool:
    try:
        return input(f"{question} [s/N] ").strip().lower() in ("s", "sim", "y", "yes")
    except EOFError:
        return False


def _choose_profile(profiles: list[dict]) -> str:
    print("\nPerfis disponíveis no TX:")
    for i, p in enumerate(profiles, 1):
        mark = "*" if p.get("is_current") else " "
        print(f"  {i}. {mark} {p['username']:<20} {p['path']}")
    default = next((p for p in profiles if p.get("is_current")), profiles[0])
    while True:
        try:
            raw = input(f"Perfil a migrar [1-{len(profiles)}, ENTER = {default['username']}]: ").strip()
        except EOFError:
            return default["username"]
        if not raw:
            return default["username"]
        if raw.isdigit() and 1 <= int(raw) <= len(profiles):
            return profiles[int(raw) - 1]["username"]
        print("Opção inválida.")


def _pause_before_pass(number: int) -> None:
    try:
        input(f"\nPronto para a passada {number}. Feche os programas no TX e pressione "
              "ENTER para continuar (Ctrl+C cancela)... ")
    except EOFError:
        pass


PASSWORD_ENV = "MPM_NEW_USER_PASSWORD"


def _read_new_password(name: str) -> str:
    """Senha do usuário novo: variável de ambiente ou prompt oculto (nunca na linha de comando)."""
    from mpm.core.accounts import AccountError

    env = os.environ.get(PASSWORD_ENV)
    if env:
        return env
    import getpass
    for _ in range(3):
        first = getpass.getpass(f"Defina a senha do usuário {name}: ")
        if first == getpass.getpass("Repita a senha: "):
            if not first:
                print("  (senha vazia: o Windows pode recusar, conforme a política da máquina)")
            return first
        print("  As senhas não conferem.")
    raise AccountError("senhas não conferiram 3 vezes")


def cmd_account_check(args: argparse.Namespace) -> int:
    """Diagnóstico SOMENTE LEITURA da criação de contas (nada é criado)."""
    from mpm.platforms import accounts

    print(BANNER)
    print(f"[account-check] Verificando o que seria necessário para criar o usuário {args.name!r}\n")
    rows = accounts().check(args.name)
    for label, value, ok in rows:
        print(f"  [{'OK' if ok else '!!'}] {label}: {value}")
    bad = [r for r in rows if not r[2]]
    print("\nTudo certo para criar a conta." if not bad else
          "\nHá pendências acima; resolva-as antes de usar 'rx --as-user'.")
    return 0 if not bad else 1


def _install_programs(report, mode: str, *, only: str | None, dest: Path, dry_run: bool,
                      pin_versions: bool, assume_yes: bool) -> int:
    """Instala nesta máquina os programas do relatório do TX. Devolve 0, ou 5 se algum falhou."""
    from mpm import platforms
    from mpm.net import apps_install
    if mode == "menu":
        mode = "express" if assume_yes else apps_install.ask_mode()
    entries = apps_install.plan(report)
    if mode in ("express", "custom"):
        entries = apps_install.mark_present(entries, *apps_install.scan_local(platforms.apps()))
        skipped = sum(bool(e.get("present")) for e in entries)
        if skipped:
            print(f"  {skipped} itens já estão instalados aqui e ficam desmarcados.")
    if mode == "express":
        entries = apps_install.express(entries)
        if only is not None:
            entries = apps_install.only(entries, only)
    elif mode == "custom":
        if only is not None:
            entries = apps_install.only(entries, only)
            if not any(e["selected"] for e in entries):
                print(f"Nenhum item casa com --only {only!r}.")
                return 0
        entries = apps_install.choose(entries)
    else:
        entries = None
    if entries is None:
        print("Nenhum programa foi instalado.")
        return 0
    results = apps_install.run_install(entries, platforms.apps(), outdir=dest / "mpm",
                                       dry_run=dry_run, pin_versions=pin_versions)
    return 5 if any(r["status"] == "falhou" for r in results) else 0


def _make_migration(args, client, workers, source_user):
    from mpm.net.migration import Migration, Options
    options = Options(
        dest=Path(args.dest),
        source_user=source_user,
        excludes=args.exclude or [],
        passes=max(1, args.passes),
        dry_run=args.dry_run,
        assume_yes=args.yes,
        retries=max(0, args.retries),
        retry_wait=max(0.0, args.retry_wait),
    )
    target = None
    if args.as_user is not None:
        from mpm.core.target import AccountTarget
        from mpm.platforms import accounts
        target = AccountTarget(
            accounts(), name=args.as_user or None, admin=args.admin, merge=args.merge,
            meta_dir=Path(args.dest) / "mpm", get_password=_read_new_password)
    return Migration(
        client, options, target=target,
        ask=_ask_yes_no,
        choose_profile=_choose_profile,
        pause=None if args.yes or getattr(args, "meu", False) else _pause_before_pass,
        workers=workers,
    )


def _run_full(args, client, workers) -> int:
    """Tudo de uma vez: perfil -> programas -> configurações e Wi-Fi, numa só conexão."""
    from mpm import platforms
    from mpm.net import apps_install
    from mpm.net.appdata_inventory import ProfileError, pick_source_profile
    from mpm.net.apps_inventory import run_apps_inventory
    from mpm.net.migration import MigrationError
    from mpm.net.rx import RemoteError
    from mpm.net.settings import Backends, SettingsError, run_settings

    personal = bool(getattr(args, "meu", False))
    backends = Backends(platforms.accounts(), platforms.appdata(), platforms.regtools(),
                        None if personal else platforms.devices())
    if not args.dry_run and backends.accounts.is_elevated() is False:
        print("Erro: abra o PowerShell como administrador (criar usuário e Wi-Fi exigem).", file=sys.stderr)
        return 2
    try:
        source = pick_source_profile(client, args.source_user, _choose_profile)
    except (ProfileError, RemoteError, OSError) as exc:
        print(f"\nErro: {exc}", file=sys.stderr)
        return 4
    print(f"Perfil de origem no TX: {source}")
    mode = args.full
    steps = 2 if personal else 3
    if mode == "menu":
        mode = "express" if args.yes else apps_install.ask_mode(
            title="MIGRAÇÃO COMPLETA (perfil + programas + configurações e Wi-Fi)",
            verb="faz", cancel="Cancelar")
    if mode is None:
        print("Cancelado. Nada foi feito.")
        return 0
    dest = Path(args.dest)

    print(f"\n=== ETAPA 1/{steps}: PERFIL E ARQUIVOS ===")
    try:
        summary = _make_migration(args, client, workers, source).run()
    except (MigrationError, RemoteError, OSError) as exc:
        print(f"\nErro: {exc}", file=sys.stderr)
        return 4
    except KeyboardInterrupt:
        print("\nInterrompido. Rode de novo para retomar.", file=sys.stderr)
        return 130
    code = 0
    if not summary.get("dry_run") and not summary.get("ok", True):
        print("\nA cópia do perfil terminou com pendências; as etapas seguintes foram canceladas. "
              f"Veja {dest / 'mpm' / 'transfer-report.json'} e rode de novo para retomar.", file=sys.stderr)
        return 1

    if not personal:
        print("\n=== ETAPA 2/3: PROGRAMAS ===")
        try:
            report = run_apps_inventory(client, dest / "mpm", show_all=False, extra_ignore=args.ignore_app)
            code = max(code, _install_programs(report, mode, only=None, dest=dest, dry_run=args.dry_run,
                                               pin_versions=args.pin_versions, assume_yes=args.yes))
        except (RemoteError, OSError) as exc:
            print(f"\nErro: {exc}", file=sys.stderr)
            return 4

    print("\n=== ETAPA 2/2: OUTLOOK ===" if personal else "\n=== ETAPA 3/3: CONFIGURAÇÕES E WI-FI ===")
    try:
        result = run_settings(
            client, workers, backends, outdir=dest / "mpm", mode=mode, only="^outlook$" if personal else None,
            to_user=args.as_user,
            source_user=source, choose_profile=_choose_profile, assume_yes=args.yes, dry_run=args.dry_run,
            retries=args.retries, retry_wait=args.retry_wait)
    except (SettingsError, RemoteError, OSError, RuntimeError) as exc:
        print(f"\nErro: {exc}", file=sys.stderr)
        return 4
    if not result.get("ok", True):
        code = max(code, 5)
    print("\nMigração completa " + ("simulada." if args.dry_run else
          ("concluída sem pendências." if code == 0 else "concluída COM pendências.")))
    return code


def cmd_rx(args: argparse.Namespace) -> int:
    from mpm.net.migration import MigrationError
    from mpm.net.netcheck import run_netcheck
    from mpm.net.rx import (
        RemoteError,
        RxClient,
        RxListener,
        close_workers,
        local_addresses,
        open_workers,
    )
    from mpm.net.security import AuthError, MissingDependency, short_fingerprint

    if args.meu:
        if args.full or args.settings or args.apps or args.appdata or args.install or args.netcheck \
                or args.only is not None or args.to_user:
            print("Erro: --meu já faz perfil + Outlook; não combina com "
                  "--full/--settings/--apps/--appdata/--install/--only/--to-user/--netcheck.", file=sys.stderr)
            return 2
        if not args.as_user:
            print("Erro: --meu exige --as-user NOME (o usuário que recebe tudo).", file=sys.stderr)
            return 2
        args.full = "express"
    as_user = args.as_user is not None
    if (args.admin or args.merge) and not as_user:
        print("Erro: --admin e --merge só fazem sentido com --as-user.", file=sys.stderr)
        return 2
    if (args.install or args.pin_versions) and not args.apps and not args.full:
        print("Erro: --install e --pin-versions só fazem sentido com --apps.", file=sys.stderr)
        return 2
    if args.full:
        if not args.as_user:
            print("Erro: --full exige --as-user NOME (o usuário novo que recebe tudo).", file=sys.stderr)
            return 2
        if args.settings or args.apps or args.appdata or args.install or args.netcheck or args.only is not None \
                or args.to_user:
            print("Erro: --full já faz perfil, programas e configurações; não combina com "
                  "--settings/--apps/--appdata/--install/--only/--to-user/--netcheck.", file=sys.stderr)
            return 2
        if args.full == "custom" and args.yes:
            print("Erro: o modo custom é interativo e não combina com -y (use --full express).", file=sys.stderr)
            return 2
    elif as_user and (args.settings or args.apps or args.appdata):
        hint = " Para as configurações em outro usuário, use --to-user NOME." if args.settings else ""
        print("Erro: --as-user (copiar o perfil) não se combina com --settings/--apps/--appdata; "
              "rode-os separadamente." + hint, file=sys.stderr)
        return 2
    if args.to_user and not args.settings:
        print("Erro: --to-user só faz sentido com --settings.", file=sys.stderr)
        return 2
    if args.settings == "custom" and args.yes:
        print("Erro: o modo custom é interativo e não combina com -y (use --settings express).", file=sys.stderr)
        return 2
    if args.only is not None:
        if not args.install and not args.settings:
            print("Erro: --only só faz sentido com --install ou --settings.", file=sys.stderr)
            return 2
        try:
            re.compile(args.only)
        except re.error as exc:
            print(f"Erro: expressão de --only inválida: {exc}", file=sys.stderr)
            return 2
    if args.install == "custom" and args.yes:
        print("Erro: o modo custom é interativo e não combina com -y (use --install express).", file=sys.stderr)
        return 2
    if not args.netcheck and not args.dest and not as_user and not args.apps and not args.appdata \
            and not args.settings:
        print("Erro: informe --dest PASTA, ou --as-user para criar o usuário na máquina nova "
              "(ou use --netcheck para só medir a rede).", file=sys.stderr)
        return 2
    if (as_user or args.apps or args.appdata or args.settings) and not args.dest:
        args.dest = str(Path(os.environ.get("PROGRAMDATA") or ".") / "MPM" / "migracao")
    if not 1 <= args.threads <= 32:
        print("Erro: --threads deve estar entre 1 e 32.", file=sys.stderr)
        return 2

    if (as_user or args.settings or args.full) and not args.dry_run:
        from mpm import platforms
        if platforms.accounts().is_elevated() is False:
            print("Erro: abra o programa como administrador (botão direito -> Executar como administrador, "
                  "ou PowerShell como administrador). Criar usuário, Wi-Fi e o perfil de outro usuário exigem.",
                  file=sys.stderr)
            return 2

    try:
        listener = RxListener(port=args.port)
    except MissingDependency as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"Erro: não foi possível escutar na porta {args.port}: {exc}", file=sys.stderr)
        return 2

    responder = None
    try:
        responder = DiscoveryResponder(listener.port).start()
    except OSError as exc:
        discovery_error = str(exc)
    else:
        discovery_error = ""

    print(BANNER)
    print("[RX] Aguardando o TX (máquina antiga)\n")
    print(f"  Código de pareamento : {listener.code}")
    print(f"  Impressão digital    : {short_fingerprint(listener.fingerprint)}")
    addresses = local_addresses() or ["<endereço-do-RX>"]
    suffix = "" if args.port == DEFAULT_PORT else f":{args.port}"
    print(f"  Endereços do RX      : {', '.join(addresses)} (porta {listener.port})")
    print("\n  No TX, execute (ele localiza este RX sozinho e pede o código):")
    print(f"    {COMMAND} tx")
    if discovery_error:
        print(f"\n  Aviso: descoberta automática indisponível ({discovery_error}).")
        print("  Use o endereço manualmente:")
        print(f"    {COMMAND} tx --rx {addresses[0]}{suffix} --code {listener.code}")
    print("\n  Se o Firewall do Windows perguntar, permita o acesso em redes privadas.\n")

    try:
        conn = listener.accept_authenticated()
    except AuthError as exc:
        print(f"Pareamento falhou: {exc}", file=sys.stderr)
        listener.close()
        return 3
    except KeyboardInterrupt:
        print("\nCancelado.")
        listener.close()
        return 130
    finally:
        if responder:
            responder.stop()      # pareado (ou cancelado): este RX deixa de aparecer na rede

    client = RxClient(conn)
    workers: list[RxClient] = []
    try:
        if args.threads > 1 and not args.dry_run and not args.apps and not args.appdata:
            try:
                workers = open_workers(client, listener, args.threads)
                print(f"{len(workers)} conexões de cópia abertas.")
            except (AuthError, OSError, RemoteError, TimeoutError) as exc:
                print(f"Aviso: não foi possível abrir conexões paralelas ({exc}); "
                      "seguindo com uma conexão.", file=sys.stderr)

        if args.full:
            return _run_full(args, client, workers)

        if args.settings:
            from mpm import platforms
            from mpm.net.settings import Backends, SettingsError, run_settings
            backends = Backends(platforms.accounts(), platforms.appdata(), platforms.regtools(), platforms.devices())
            if not args.dry_run and backends.accounts.is_elevated() is False:
                print("Erro: abra o PowerShell como administrador (Wi-Fi e o perfil de outro usuário "
                      "exigem).", file=sys.stderr)
                return 2
            try:
                summary = run_settings(
                    client, workers, backends, outdir=Path(args.dest) / "mpm", mode=args.settings,
                    only=args.only, to_user=args.to_user, source_user=args.source_user, choose_profile=_choose_profile,
                    assume_yes=args.yes, dry_run=args.dry_run,
                    retries=args.retries, retry_wait=args.retry_wait)
            except (SettingsError, RemoteError, OSError, RuntimeError) as exc:
                print(f"\nErro: {exc}", file=sys.stderr)
                return 4
            return 0 if summary.get("ok", True) else 5

        if args.appdata:
            from mpm.net.appdata_inventory import run_appdata_inventory
            try:
                from mpm.net.appdata_inventory import ProfileError, pick_source_profile
                source = pick_source_profile(client, args.source_user, _choose_profile)
                print(f"Perfil de origem no TX: {source}")
                run_appdata_inventory(client, Path(args.dest) / "mpm", user=source)
            except (ProfileError, RemoteError, OSError) as exc:
                print(f"\nErro: {exc}", file=sys.stderr)
                return 4
            if not args.apps:
                return 0

        if args.apps:
            from mpm.net.apps_inventory import run_apps_inventory
            try:
                report = run_apps_inventory(client, Path(args.dest) / "mpm", show_all=args.all,
                                            extra_ignore=args.ignore_app)
            except (RemoteError, OSError) as exc:
                print(f"\nErro: {exc}", file=sys.stderr)
                return 4
            if args.install:
                return _install_programs(report, args.install, only=args.only, dest=Path(args.dest),
                                         dry_run=args.dry_run, pin_versions=args.pin_versions, assume_yes=args.yes)
            return 0

        if args.netcheck:
            run_netcheck(client, workers)
            return 0

        migration = _make_migration(args, client, workers, args.source_user)
        try:
            summary = migration.run()
        except (MigrationError, RemoteError, OSError) as exc:
            print(f"\nErro: {exc}", file=sys.stderr)
            return 4
        except KeyboardInterrupt:
            print("\nInterrompido. O que já foi copiado e verificado permanece no destino; "
                  "arquivos em andamento continuam de onde pararam. Rode de novo para retomar.",
                  file=sys.stderr)
            return 130
    finally:
        close_workers(workers)
        client.close()
        listener.close()

    if summary.get("dry_run"):
        print(f"\nDry run concluído: {summary['to_copy']} arquivos "
              f"({fmt_bytes(summary['bytes_to_copy'])}) seriam copiados. Nada foi gravado.")
        return 0
    report = Path(args.dest) / "mpm" / "transfer-report.json"
    print(f"\n{'Concluído sem pendências' if summary['ok'] else 'Concluído COM pendências'}. "
          f"Relatório: {report}")
    return 0 if summary["ok"] else 1


def _choose_rx(found: list[RxAnnouncement]) -> RxAnnouncement | str | None:
    """Mostra os RX encontrados. Devolve o escolhido, 'r' (procurar de novo) ou None (sair)."""
    if len(found) == 1:
        rx = found[0]
        print(f"RX encontrado: {rx.hostname}  {rx.address}:{rx.port}  (MPM {rx.mpm_version})")
        return rx
    print(f"{len(found)} RX encontrados na rede:\n")
    for i, rx in enumerate(found, 1):
        print(f"  {i}. {rx.hostname:<24} {rx.address}:{rx.port}  (MPM {rx.mpm_version})")
    while True:
        try:
            raw = input(f"\nEscolha o RX [1-{len(found)}], r = procurar de novo, q = sair: ").strip().lower()
        except EOFError:
            return None
        if raw in ("q", "sair"):
            return None
        if raw == "r":
            return "r"
        if raw.isdigit() and 1 <= int(raw) <= len(found):
            return found[int(raw) - 1]
        print("Opção inválida.")


def _parse_hostport(text: str) -> tuple[str, int]:
    host, _, port_text = text.strip().partition(":")
    return host, (int(port_text) if port_text else DEFAULT_PORT)


def _find_rx() -> tuple[str, int] | None:
    """Descobre o RX por broadcast. Com vários, o usuário escolhe pelo nome do computador."""
    from mpm.net.discovery import discover_rx

    while True:
        print("Procurando o RX na rede local ...")
        found = discover_rx()
        if found:
            choice = _choose_rx(found)
            if isinstance(choice, RxAnnouncement):
                return choice.address, choice.port
            if choice is None:
                return None
            continue                      # 'r': procura de novo
        print("Nenhum RX respondeu. Confira se o 'mpm rx' está rodando na mesma rede/sub-rede "
              "e se o Firewall do RX permite o acesso.")
        try:
            raw = input("ENTER = procurar de novo, ou digite IP[:porta] do RX, q = sair: ").strip()
        except EOFError:
            return None
        if raw.lower() in ("q", "sair"):
            return None
        if raw:
            try:
                return _parse_hostport(raw)
            except ValueError:
                print("Endereço inválido.")


def _ask_code() -> str | None:
    try:
        return input("Código de pareamento (exibido no RX): ").strip() or None
    except EOFError:
        return None


def cmd_tx(args: argparse.Namespace) -> int:
    from mpm.net.security import AuthError, MissingDependency
    from mpm.net.tx import run_tx

    profile = None
    if args.profile_path:
        path = Path(args.profile_path)
        if not path.is_dir():
            print(f"Diretório inexistente: {path}", file=sys.stderr)
            return 2
        profile = ProfileInfo(username=path.name, sid="", path=str(path), is_current=True)

    print(BANNER)
    try:
        if args.rx:
            try:
                host, port = _parse_hostport(args.rx)
            except ValueError:
                print(f"Porta inválida em --rx: {args.rx!r}", file=sys.stderr)
                return 2
        else:
            target = _find_rx()
            if target is None:
                print("Nenhum RX selecionado. Para informar o endereço manualmente: "
                      f"{COMMAND} tx --rx IP --code CÓDIGO", file=sys.stderr)
                return 4
            host, port = target

        code = args.code
        attempts = 1 if code else 3        # o RX também encerra após 3 pareamentos falhos
        for attempt in range(1, attempts + 1):
            if not code:
                code = _ask_code()
                if not code:
                    print("Código não informado.", file=sys.stderr)
                    return 3
            try:
                run_tx(host, port, code, profile=profile)
                return 0
            except AuthError as exc:
                if attempt >= attempts:
                    raise
                print(f"Pareamento falhou: {exc}. Confira o código no RX e tente de novo "
                      f"({attempts - attempt} tentativa(s) restante(s)).", file=sys.stderr)
                code = None
    except MissingDependency as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 2
    except AuthError as exc:
        print(f"Pareamento falhou: {exc}", file=sys.stderr)
        return 3
    except (OSError, ConnectionError) as exc:
        print(f"Não foi possível comunicar com o RX: {exc}", file=sys.stderr)
        return 4
    except KeyboardInterrupt:
        print("\nEncerrado.")
        return 130
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    try:
        manifest = Manifest.read(args.path)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Manifesto inválido: {exc}", file=sys.stderr)
        return 1
    print(f"OK — {manifest.migration_id} (schema v{manifest.schema_version}, "
          f"origem {manifest.source_system.hostname}, perfil {manifest.source_profile.username})")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mpm", description="MPM — Munhos PC Migrator")
    parser.add_argument("--version", action="version", version=f"MPM {__version__}")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("discover", help="Discovery de sistema e usuário")
    p.add_argument("--json", action="store_true", help="Saída em JSON")
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("profiles", help="Lista os perfis de usuário da máquina")
    p.add_argument("--json", action="store_true", help="Saída em JSON")
    p.set_defaults(func=cmd_profiles)

    p = sub.add_parser("scan", help="Varre as pastas do perfil (e o AppData) sem gravar nada")
    _add_profile_args(p)
    p.add_argument("--no-appdata", action="store_true", help="Pula o resumo do AppData (mais rápido)")
    p.add_argument("--json", action="store_true", help="Saída em JSON")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("manifest", help="Gera o manifesto (JSON) e o inventário de arquivos")
    _add_profile_args(p)
    p.add_argument("--no-appdata", action="store_true", help="Pula o resumo do AppData")
    p.add_argument("-o", "--output", help="Arquivo do manifesto (ex.: output/manifest.json); "
                                          "o inventário vai ao lado, em <nome>.inventory.jsonl")
    p.set_defaults(func=cmd_manifest)

    p = sub.add_parser("rx", help="Máquina NOVA: escuta, controla e recebe a migração pela rede")
    p.add_argument("--dest", help="Pasta de destino dos arquivos migrados (obrigatório, exceto com "
                                  "--netcheck ou --as-user; com --as-user guarda só os metadados)")
    p.add_argument("--as-user", nargs="?", const="", metavar="NOME",
                   help="Cria o usuário local NOME na máquina nova (sem NOME: o mesmo nome do perfil "
                        "de origem) e copia direto para o perfil dele. Exige Administrador")
    p.add_argument("--admin", action="store_true", help="Com --as-user: o usuário novo será administrador")
    p.add_argument("--merge", action="store_true",
                   help="Com --as-user: aceita copiar para um usuário que JÁ existia (pode substituir arquivos)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Porta (padrão {DEFAULT_PORT})")
    p.add_argument("--source-user", help="Perfil do TX a migrar (nome ou SID); sem isto, pergunta. Vale para o perfil, --settings e --appdata")
    p.add_argument("--exclude", action="append", metavar="PADRÃO",
                   help="Exclui por padrão (ex.: node_modules, *.iso); pode repetir")
    p.add_argument("--passes", type=int, default=2,
                   help="Passadas: 1 = massa, 2 = massa + diferenças (padrão 2)")
    p.add_argument("--dry-run", action="store_true", help="Mostra o plano e não copia nada")
    p.add_argument("--threads", type=int, default=8, metavar="N",
                   help="Conexões de cópia em paralelo, como o /MT do Robocopy (1 a 32; padrão 8)")
    p.add_argument("--retries", type=int, default=2, metavar="N",
                   help="Tentativas extras por arquivo com falha transitória (padrão 2)")
    p.add_argument("--retry-wait", type=float, default=2.0, metavar="SEG",
                   help="Segundos de espera entre tentativas (padrão 2)")
    p.add_argument("--apps", action="store_true",
                   help="Só inventaria os aplicativos instalados no TX e gera o relatório (nada é copiado nem instalado)")
    p.add_argument("--appdata", action="store_true",
                   help="Só inventaria os dados e configurações dos programas do TX (Chrome, Firefox, PuTTY, WinSCP, "
                        "Outlook, SSH...) com tamanhos e avisos; nada é copiado. Pode vir junto de --apps")
    p.add_argument("--settings", nargs="?", const="menu", choices=("menu", "express", "custom"), metavar="MODO",
                   help="Copia as configurações dos programas do TX (Chrome, Firefox, PuTTY, WinSCP, Outlook, SSH...), "
                        "os Wi-Fi salvos para o RX. MODO: express = tudo sem perguntar; custom = você marca; sem MODO "
                        "pergunta. Instale os programas antes (--apps --install). Com --dry-run só simula")
    p.add_argument("--to-user", metavar="NOME",
                   help="Com --settings: usuário local (já com perfil) que recebe as configurações; sem isto, "
                        "o usuário atual. Para outro usuário, abra o PowerShell como administrador")
    p.add_argument("--all", action="store_true", help="Com --apps: lista também sistema e drivers no resumo")
    p.add_argument("--ignore-app", action="append", metavar="REGEX",
                   help="Com --apps: trata como ruído os programas cujo nome casar com a expressão (repetível)")
    p.add_argument("--install", nargs="?", const="menu", choices=("menu", "express", "custom"), metavar="MODO",
                   help="Com --apps: depois do relatório, instala nesta máquina os programas. MODO: express = tudo "
                        "sem perguntar; custom = você marca cada item; sem MODO pergunta (com -y = express). "
                        "Com --dry-run só simula")
    p.add_argument("--only", metavar="REGEX",
                   help="Com --install: marca só os itens cujo ID ou nome casar (ex.: --only notepad). "
                        "No custom abre a lista já com só eles marcados")
    p.add_argument("--pin-versions", action="store_true",
                   help="Com --install: pede a mesma versão do TX em vez da mais recente")
    p.add_argument("--full", nargs="?", const="menu", choices=("menu", "express", "custom"), metavar="MODO",
                   help="Tudo de uma vez, com --as-user NOME: copia o perfil, instala os programas e copia "
                        "configurações e Wi-Fi. MODO: express = sem perguntar; custom = você marca; sem MODO pergunta")
    p.add_argument("--meu", action="store_true",
                   help="MPM pessoal, com --as-user NOME: copia o perfil inteiro (Área de Trabalho, Documentos, "
                        "Downloads...) e as configurações do Outlook (contas sem senha, assinaturas, .pst). "
                        "Sem programas e sem Wi-Fi; não pergunta nada")
    p.add_argument("--netcheck", action="store_true",
                   help="Só mede a rede (latência e vazão com 1 e N conexões); não copia nada")
    p.add_argument("-y", "--yes", action="store_true",
                   help="Não pergunta nada (sem confirmações nem pausa entre passadas)")
    p.add_argument("--no-log", action="store_true", help="Não grava o arquivo de log da execução")
    p.set_defaults(func=cmd_rx)

    p = sub.add_parser("account-check", help="Verifica (sem criar nada) se esta máquina consegue criar o usuário")
    p.add_argument("name", help="Nome do usuário que seria criado")
    p.set_defaults(func=cmd_account_check)

    p = sub.add_parser("tx", help="Máquina ANTIGA: conecta ao RX e serve os arquivos (somente leitura)")
    p.add_argument("--rx", help="Endereço do RX (IP ou nome[:porta]); sem isto, procura na rede local")
    p.add_argument("--code", help="Código de pareamento exibido no RX; sem isto, pergunta")
    p.add_argument("--profile-path", help="Serve este diretório como perfil (testes/disco montado)")
    p.add_argument("--no-log", action="store_true", help="Não grava o arquivo de log da execução")
    p.set_defaults(func=cmd_tx)

    p = sub.add_parser("validate", help="Valida um manifesto existente")
    p.add_argument("path")
    p.set_defaults(func=cmd_validate)

    return parser


def _command_name() -> str:
    """Como o usuário chama o programa: o .exe empacotado ou `python -m mpm`."""
    return "mpm.exe" if getattr(sys, "frozen", False) else "python -m mpm"


COMMAND = _command_name()


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        args = parser.parse_args(["discover"])
    run = None
    if args.func in (cmd_rx, cmd_tx) and not getattr(args, "no_log", False):
        from mpm.core.runlog import RunLog
        run = RunLog.start("rx" if args.func is cmd_rx else "tx", list(argv if argv is not None else sys.argv[1:]))
        if run.path:
            print(f"Log desta execução: {run.path}")
    code: object = 1
    try:
        code = args.func(args)
        return code
    except ProfileNotFound as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        code = 2
        return 2
    except BaseException:
        if run:
            run.exception()
        raise
    finally:
        if run:
            if run.path:
                print(f"Log gravado em: {run.path}", file=sys.stderr)
            run.stop(code)
