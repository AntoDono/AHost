"""Read-only facts about the live host. Nothing here needs root; anything unreadable is reported as unknown."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .config import Config
from .importer.parse import parse_nginx


def run(*args: str, timeout: int = 15) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), capture_output=True, text=True, timeout=timeout, check=False)


def unit_file_text(unit: str, cfg: Config) -> str:
    """The unit's main file (as systemd sees it, drop-ins excluded)."""
    path = run("systemctl", "show", "-P", "FragmentPath", unit).stdout.strip()
    if not path:
        path = f"{cfg.paths.systemd_dir}/{unit}"
    return Path(path).read_text()


def unit_props(unit: str, *props: str) -> dict[str, str]:
    out = run("systemctl", "show", unit, *[f"-p{p}" for p in props]).stdout
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


def unit_pids(unit: str) -> list[int]:
    cg = unit_props(unit, "ControlGroup").get("ControlGroup", "")
    if not cg:
        return []
    try:
        return [int(x) for x in Path(f"/sys/fs/cgroup{cg}/cgroup.procs").read_text().split()]
    except OSError:
        return []


def unit_ports(unit: str) -> list[int]:
    """TCP ports a unit listens on. Only works for processes visible to us (not root-owned ones)."""
    pids = set(unit_pids(unit))
    ports = set()
    for line in run("ss", "-ltnpH").stdout.splitlines():
        cols = line.split()
        if len(cols) < 6:
            continue
        found = {int(p) for p in re.findall(r"pid=(\d+)", cols[5])}
        if found & pids:
            try:
                ports.add(int(cols[3].rsplit(":", 1)[1]))
            except ValueError:
                pass
    return sorted(ports)


def certs_present(cfg: Config) -> set[str]:
    """Cert names that exist. /etc/letsencrypt/live is root-only, so use the (readable) renewal configs."""
    renewal = Path(cfg.certs.live_dir).parent / "renewal"
    try:
        return {p.stem for p in renewal.glob("*.conf")}
    except OSError:
        return set()


def cert_domains(cfg: Config, name: str) -> set[str] | None:
    """Domains a certbot cert covers, from its renewal config. None when unknown (only webroot certs list them)."""
    try:
        text = (Path(cfg.certs.live_dir).parent / "renewal" / f"{name}.conf").read_text()
    except OSError:
        return None
    _, sep, rest = text.partition("[[webroot_map]]")
    if not sep:
        return None
    return {m.group(1) for m in re.finditer(r"^\s*([A-Za-z0-9.*-]+)\s*=", rest, re.MULTILINE)}


def usable_certs(cfg: Config, wanted: dict[str, list[str]]) -> set[str]:
    """Cert names from `wanted` (cert -> domains it must serve) that exist and cover those domains. A cert whose
    domains are unknown counts as usable; one known to lack a domain doesn't (it gets re-issued for the new set)."""
    present = certs_present(cfg)
    out = set()
    for name, domains in wanted.items():
        if name not in present:
            continue
        have = cert_domains(cfg, name)
        if have is None or set(domains) <= have:
            out.add(name)
    return out


def legacy_server_names(cfg: Config) -> dict[str, str]:
    """server_name -> legacy site file name, for every enabled legacy site."""
    out: dict[str, str] = {}
    enabled = Path(cfg.nginx.legacy_enabled)
    for f in sorted(enabled.iterdir()) if enabled.exists() else []:
        try:
            tree = parse_nginx(f.read_text())
        except (OSError, ValueError):
            continue
        for srv in (d for d in tree if d.name == "server"):
            for d in srv.find("server_name"):
                for n in d.args:
                    if n != "_":
                        out.setdefault(n, f.name)
    return out


def site_text(site: str, cfg: Config) -> str:
    return (Path(cfg.nginx.legacy_available) / site).read_text()
