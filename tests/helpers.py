from pathlib import Path

from mpm.core.models import ProfileInfo


def make_profile(root: Path, name: str = "tester") -> ProfileInfo:
    """Perfil falso em um diretório temporário: 3 pastas no escopo + 2 fora (Music, Downloads)."""
    home = root / name
    for folder in ("Desktop", "Documents", "Pictures", "Downloads", "Music"):
        (home / folder).mkdir(parents=True)
    (home / "Desktop" / "a.txt").write_bytes(b"x" * 10)
    (home / "Documents" / "sub").mkdir()
    (home / "Documents" / "sub" / "b.bin").write_bytes(b"y" * 100)
    (home / "Documents" / "c.txt").write_bytes(b"z" * 1)
    (home / "Pictures" / "d.jpg").write_bytes(b"p" * 1000)
    (home / "Music" / "fora.mp3").write_bytes(b"m" * 5000)   # fora do escopo
    (home / "Downloads" / "baixado.zip").write_bytes(b"d" * 9000)   # fora do escopo
    return ProfileInfo(username=name, sid="S-1-5-21-0-0-0-9999", path=str(home), is_current=False)
