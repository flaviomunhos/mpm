"""Contas locais do Windows via API nativa (ctypes): sem PowerShell, sem dependências.

ATENÇÃO: este módulo só pode ser exercitado de verdade no Windows. Rode
`python -m mpm account-check NOME` (somente leitura) antes de criar qualquer coisa.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
from ctypes import wintypes
from pathlib import Path

from mpm.core.accounts import AccountError, validate_username
from mpm.platforms.windows.users import _is_elevated

SID_USERS = "S-1-5-32-545"
SID_ADMINISTRATORS = "S-1-5-32-544"
USER_PRIV_USER = 1
UF_SCRIPT = 0x0001
UF_NORMAL_ACCOUNT = 0x0200
SID_TYPE_USER = 1
ERROR_NONE_MAPPED = 1332
ERROR_MEMBER_IN_ALIAS = 1378
HR_ALREADY_EXISTS = 0x800700B7
_PROFILE_LIST = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList"

_NET_ERRORS = {
    5: "acesso negado (execute como Administrador)",
    2202: "nome de usuário inválido",
    2224: "o usuário já existe",
    2243: "senha inválida",
    2245: "a senha não atende à política (tamanho/complexidade) desta máquina",
}


class _UserInfo1(ctypes.Structure):
    _fields_ = [
        ("usri1_name", wintypes.LPWSTR), ("usri1_password", wintypes.LPWSTR),
        ("usri1_password_age", wintypes.DWORD), ("usri1_priv", wintypes.DWORD),
        ("usri1_home_dir", wintypes.LPWSTR), ("usri1_comment", wintypes.LPWSTR),
        ("usri1_flags", wintypes.DWORD), ("usri1_script_path", wintypes.LPWSTR),
    ]


class _MemberInfo3(ctypes.Structure):
    _fields_ = [("lgrmi3_domainandname", wintypes.LPWSTR)]


class _Api:
    """DLLs do Windows com assinaturas declaradas (carregadas só quando usadas)."""

    def __init__(self) -> None:
        d, p, v = wintypes.DWORD, ctypes.POINTER, ctypes.c_void_p
        self.net = ctypes.WinDLL("netapi32", use_last_error=True)  # type: ignore[attr-defined]
        self.adv = ctypes.WinDLL("advapi32", use_last_error=True)  # type: ignore[attr-defined]
        self.env = ctypes.WinDLL("userenv", use_last_error=True)   # type: ignore[attr-defined]
        self.k32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]

        self.net.NetUserAdd.argtypes = [wintypes.LPCWSTR, d, v, p(d)]
        self.net.NetUserAdd.restype = d
        self.net.NetUserDel.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        self.net.NetUserDel.restype = d
        self.net.NetLocalGroupAddMembers.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, d, v, d]
        self.net.NetLocalGroupAddMembers.restype = d

        self.adv.LookupAccountNameW.argtypes = [
            wintypes.LPCWSTR, wintypes.LPCWSTR, v, p(d), wintypes.LPWSTR, p(d), p(d)]
        self.adv.LookupAccountNameW.restype = wintypes.BOOL
        self.adv.LookupAccountSidW.argtypes = [
            wintypes.LPCWSTR, v, wintypes.LPWSTR, p(d), wintypes.LPWSTR, p(d), p(d)]
        self.adv.LookupAccountSidW.restype = wintypes.BOOL
        self.adv.ConvertSidToStringSidW.argtypes = [v, p(wintypes.LPWSTR)]
        self.adv.ConvertSidToStringSidW.restype = wintypes.BOOL
        self.adv.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, p(v)]
        self.adv.ConvertStringSidToSidW.restype = wintypes.BOOL

        self.k32.LocalFree.argtypes = [v]
        self.k32.LocalFree.restype = v
        self.env.CreateProfile.argtypes = [
            wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPWSTR, d]
        self.env.CreateProfile.restype = ctypes.c_long       # HRESULT


_api_cache: _Api | None = None


def _api() -> _Api:
    global _api_cache
    if _api_cache is None:
        _api_cache = _Api()
    return _api_cache


def _computer() -> str:
    return os.environ.get("COMPUTERNAME") or os.uname().nodename  # type: ignore[attr-defined]


def is_elevated() -> bool | None:
    return _is_elevated()


def _sid_to_string(sid_ptr) -> str:
    api = _api()
    out = wintypes.LPWSTR()
    if not api.adv.ConvertSidToStringSidW(sid_ptr, ctypes.byref(out)):
        raise AccountError(f"ConvertSidToStringSid falhou (erro {ctypes.get_last_error()})")
    try:
        return out.value or ""
    finally:
        api.k32.LocalFree(ctypes.cast(out, ctypes.c_void_p))


def lookup_user(name: str) -> str | None:
    """SID do usuário LOCAL `name`, ou None se não existir."""
    api = _api()
    sid = ctypes.create_string_buffer(512)
    cb_sid = wintypes.DWORD(len(sid))
    domain = ctypes.create_unicode_buffer(512)
    cch = wintypes.DWORD(len(domain))
    use = wintypes.DWORD()
    ok = api.adv.LookupAccountNameW(None, f"{_computer()}\\{name}", sid, ctypes.byref(cb_sid),
                                    domain, ctypes.byref(cch), ctypes.byref(use))
    if not ok:
        err = ctypes.get_last_error()
        if err == ERROR_NONE_MAPPED:
            return None
        raise AccountError(f"LookupAccountName falhou (erro {err})")
    if use.value != SID_TYPE_USER:
        raise AccountError(f"{name!r} existe, mas não é um usuário (é grupo ou outro tipo de conta)")
    return _sid_to_string(sid)


def group_name(sid_text: str) -> str:
    """Nome local do grupo conhecido (o Windows é localizado: 'Users', 'Usuários', ...)."""
    api = _api()
    psid = ctypes.c_void_p()
    if not api.adv.ConvertStringSidToSidW(sid_text, ctypes.byref(psid)):
        raise AccountError(f"SID inválido {sid_text} (erro {ctypes.get_last_error()})")
    try:
        name = ctypes.create_unicode_buffer(256)
        domain = ctypes.create_unicode_buffer(256)
        cn, cd, use = wintypes.DWORD(256), wintypes.DWORD(256), wintypes.DWORD()
        if not api.adv.LookupAccountSidW(None, psid, name, ctypes.byref(cn),
                                         domain, ctypes.byref(cd), ctypes.byref(use)):
            raise AccountError(f"não consegui resolver o grupo {sid_text} "
                               f"(erro {ctypes.get_last_error()})")
        return name.value
    finally:
        api.k32.LocalFree(psid)


def _add_to_group(sid_text: str, name: str) -> None:
    api = _api()
    group = group_name(sid_text)
    member = _MemberInfo3(f"{_computer()}\\{name}")
    rc = api.net.NetLocalGroupAddMembers(None, group, 3, ctypes.byref(member), 1)
    if rc not in (0, ERROR_MEMBER_IN_ALIAS):
        raise AccountError(f"não consegui adicionar {name!r} ao grupo {group!r} (código {rc})")


def create_user(name: str, password: str, admin: bool = False) -> str:
    name = validate_username(name)
    api = _api()
    info = _UserInfo1(name, password, 0, USER_PRIV_USER, None, None,
                      UF_SCRIPT | UF_NORMAL_ACCOUNT, None)
    parm = wintypes.DWORD(0)
    rc = api.net.NetUserAdd(None, 1, ctypes.byref(info), ctypes.byref(parm))
    if rc != 0:
        raise AccountError(f"NetUserAdd falhou: {_NET_ERRORS.get(rc, 'erro desconhecido')} (código {rc})")
    try:
        _add_to_group(SID_USERS, name)
        if admin:
            _add_to_group(SID_ADMINISTRATORS, name)
        sid = lookup_user(name)
        if not sid:
            raise AccountError("a conta foi criada mas não consegui obter o SID")
        return sid
    except AccountError:
        api.net.NetUserDel(None, name)        # não deixa conta pela metade
        raise


def _registry_profile_path(sid: str) -> Path | None:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _PROFILE_LIST + "\\" + sid) as key:
            raw, _ = winreg.QueryValueEx(key, "ProfileImagePath")
    except OSError:
        return None
    return Path(os.path.expandvars(raw))


def ensure_profile(sid: str, name: str) -> Path:
    buf = ctypes.create_unicode_buffer(260)
    hr = _api().env.CreateProfile(sid, name, buf, 260) & 0xFFFFFFFF
    if hr == 0:
        return Path(buf.value)
    if hr == HR_ALREADY_EXISTS:
        path = _registry_profile_path(sid)
        if path:
            return path
        raise AccountError("o perfil já existe, mas não encontrei o caminho dele no registro")
    raise AccountError(f"CreateProfile falhou (HRESULT 0x{hr:08X})")


def existing_profile_path(name: str) -> Path | None:
    """Pasta do perfil de um usuário local que já tem perfil (None se o usuário ou o perfil não existirem)."""
    sid = lookup_user(name)
    return _registry_profile_path(sid) if sid else None


def predict_profile_path(name: str) -> Path:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _PROFILE_LIST) as key:
            base, _ = winreg.QueryValueEx(key, "ProfilesDirectory")
    except OSError:
        base = r"%SystemDrive%\Users"
    return Path(os.path.expandvars(base)) / name


def set_owner(path: Path, name: str) -> str | None:
    """Passa a propriedade de `path` (recursivo) para o usuário. Melhor esforço."""
    try:
        run = subprocess.run(
            ["icacls", str(path), "/setowner", f"{_computer()}\\{name}", "/T", "/C", "/Q"],
            capture_output=True, text=True, timeout=3600)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"{type(exc).__name__}: {exc}"
    if run.returncode != 0:
        return (run.stderr or run.stdout or f"icacls retornou {run.returncode}").strip()[:300]
    return None


def check(name: str) -> list[tuple[str, str, bool]]:
    """Diagnóstico somente leitura: nada é criado nem alterado."""
    rows: list[tuple[str, str, bool]] = []
    elevated = is_elevated()
    rows.append(("Elevação (Administrador)", {True: "sim", False: "NÃO (necessário para criar)",
                                              None: "indeterminado"}[elevated], elevated is True))
    rows.append(("Computador", _computer(), True))
    try:
        validate_username(name)
        rows.append(("Nome de usuário", f"{name!r} válido", True))
    except AccountError as exc:
        rows.append(("Nome de usuário", str(exc), False))
        return rows
    try:
        _api()
        rows.append(("APIs do Windows (netapi32/advapi32/userenv)", "carregadas", True))
    except (OSError, AttributeError) as exc:
        rows.append(("APIs do Windows", f"falha: {exc}", False))
        return rows
    for label, sid_text in (("Grupo de usuários", SID_USERS), ("Grupo de administradores", SID_ADMINISTRATORS)):
        try:
            rows.append((label, f"{group_name(sid_text)}  ({sid_text})", True))
        except AccountError as exc:
            rows.append((label, str(exc), False))
    try:
        sid = lookup_user(name)
        rows.append(("Usuário já existe?", f"SIM, SID {sid}" if sid else "não (seria criado)", True))
    except AccountError as exc:
        rows.append(("Usuário já existe?", str(exc), False))
    rows.append(("Perfil previsto", str(predict_profile_path(name)), True))
    return rows
