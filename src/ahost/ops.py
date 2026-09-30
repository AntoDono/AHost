"""Operations shared by the CLI and the web API: calling the root helper, health checks, apply, lifecycle."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from . import host
from .config import Config
from .manifest_io import load_app, load_routers, write_router
from .models import App
from .ports import listening_ports

HELPER = os.environ.get("AHOST_HELPER", "/usr/local/sbin/ahost-helper")
Say = Callable[[str], None]


def _quiet(_: str) -> None:
    pass


class OpError(RuntimeError):
    pass


def helper(*args: str, say: Say = _quiet, interactive: bool = True) -> dict:
    """Run a helper verb via sudo. `interactive=False` never prompts for a password (used by the web API)."""
    say(f"$ sudo ahost-helper {' '.join(args)}")
    cmd = ["sudo"] + ([] if interactive else ["-n"]) + [HELPER, *args]
    r = subprocess.run(cmd, capture_output=True, text=True, check=False)
    try:
        out = json.loads(r.stdout.strip().splitlines()[-1]) if r.stdout.strip() else {}
    except json.JSONDecodeError:
        out = {}
    if r.returncode != 0 or not out.get("ok"):
        msg = out.get("error") or r.stderr.strip() or r.stdout.strip() or f"exit {r.returncode}"
        raise OpError(f"ahost-helper {' '.join(args)}: {msg}")
    return out


# ---------------------------------------------------------------- HTTP checks
def http(url: str, host_header: str | None = None, resolve_to: str | None = None, timeout: float = 8):
    """GET url -> (status, sha256[:16] of body, content-type). curl, so --resolve works without DNS tricks."""
    cmd = ["curl", "-sk", "-m", str(int(timeout)), "-o", "-", "-w", "\n%{http_code} %{content_type}", url]
    if resolve_to:
        u = url.split("://", 1)[1].split("/", 1)[0]
        port = "443" if url.startswith("https") else "80"
        cmd += ["--resolve", f"{u}:{port}:{resolve_to}"]
    if host_header:
        cmd += ["-H", f"Host: {host_header}"]
    r = subprocess.run(cmd, capture_output=True, check=False)
    body, _, tail = r.stdout.rpartition(b"\n")
    code, _, ctype = tail.decode(errors="replace").partition(" ")
    return int(code) if code.isdigit() else 0, hashlib.sha256(body).hexdigest()[:16], ctype.split(";")[0]


def probe(a: App, pid: str, port: int) -> dict:
    """Health of one process: listening? HTTP status of its health path (or /)."""
    proc = a.processes[pid]
    if port not in listening_ports():
        return {"ok": False, "detail": f"port {port} not listening"}
    path = proc.health or "/"
    t = time.monotonic()
    code, _, _ = http(f"http://127.0.0.1:{port}{path}", host_header=a.domains[0] if a.domains else None, timeout=5)
    ms = int((time.monotonic() - t) * 1000)
    ok = code != 0 and (code < 500 or not proc.health)
    return {"ok": ok, "status": code, "ms": ms, "path": path}


def wait_healthy(a: App, ports: dict[str, int], timeout: int = 90) -> tuple[bool, str]:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        ok = True
        for pid, port in ports.items():
            res = probe(a, pid, port)
            if not res["ok"]:
                ok, last = False, f"{pid}: {res.get('detail') or 'GET ' + res['path'] + ' -> ' + str(res['status'])}"
                break
        states = [host.unit_props(a.unit(p), "ActiveState").get("ActiveState") for p in a.processes]
        if any(s != "active" for s in states):
            ok, last = False, f"units: {states}"
        if ok:
            return True, "healthy"
        time.sleep(2)
    return False, last


# ---------------------------------------------------------------- baselines
def baseline_urls(a: App) -> list[tuple[str, str]]:
    urls = []
    for d in a.domains:
        urls.append((f"https {d} /", f"https://{d}/"))
        urls.append((f"http {d} /", f"http://{d}/"))
    if a.domains:
        d = a.domains[0]
        for r in a.effective_routes():
            if r.path.startswith("/") and r.path != "/":
                urls.append((f"https {d} {r.path}", f"https://{d}{r.path}"))
        for p in a.processes.values():
            if p.health:
                urls.append((f"https {d} {p.health}", f"https://{d}{p.health}"))
    return urls


def take_baseline(a: App) -> dict:
    res = {}
    for label, url in baseline_urls(a):
        code, digest, ctype = http(url, resolve_to="127.0.0.1")
        res[label] = {"status": code, "sha": digest, "type": ctype}
    return res


def compare_baselines(prev: dict, now: dict) -> list[str]:
    out = []
    for k, v in prev.items():
        n = now.get(k)
        if n is None:
            out.append(f"{k}: missing now")
        elif n["status"] != v["status"]:
            if k.startswith("http ") and n["status"] == 301 and v["status"] != 301:
                continue  # AHost adds an HTTP->HTTPS redirect where the legacy site fell through (intended)
            out.append(f"{k}: status {v['status']} -> {n['status']}")
        elif v["type"].startswith(("text/css", "application/javascript", "image/", "font/")) and n["sha"] != v["sha"]:
            out.append(f"{k}: static content changed")
    return out


# ---------------------------------------------------------------- lifecycle
def apply(name: str, cfg: Config, say: Say = _quiet, interactive: bool = True) -> dict:
    """Make the system match the manifest for a (non-legacy) app: unit, start, health, site, cert."""
    a = load_app(Path(cfg.paths.apps_dir) / f"{name}.toml")
    out = helper("apply-unit", name, say=say, interactive=interactive)
    say(f"ports: {out['ports']}; {len(out['changed'])} file(s) changed")
    if a.enabled:
        helper("unit", "enable", name, say=say, interactive=interactive)
        helper("unit", "restart" if out["changed"] else "start", name, say=say, interactive=interactive)
        ok, why = wait_healthy(a, out["ports"], timeout=max([p.start_timeout or 90 for p in a.processes.values()]))
        if not ok:
            raise OpError(f"not healthy: {why}")
        say("service healthy")
    else:
        helper("unit", "stop", name, say=say, interactive=interactive)
        helper("unit", "disable", name, say=say, interactive=interactive)
    if a.domains:
        helper("apply-site", name, say=say, interactive=interactive)
        if helper("cert", name, say=say, interactive=interactive)["issued"]:
            say("certificate issued")
            helper("apply-site", name, say=say, interactive=interactive)
    for r in routers_of(name, cfg):  # its port may have changed: refresh every router path that leads to it
        apply_router(r, say=say, interactive=interactive)
    return {"ports": out["ports"]}


def routers_of(app: str, cfg: Config) -> list[str]:
    routers, _ = load_routers(Path(cfg.paths.apps_dir))
    return [r.name for r in routers.values() if any(e.app == app for e in r.entries)]


def apply_router(name: str, say: Say = _quiet, interactive: bool = True) -> dict:
    out = helper("apply-router", name, say=say, interactive=interactive)
    if out.get("cert_issued"):
        say(f"certificate issued for router {name}")
    for w in out.get("warnings", []):
        say(f"router {name}: {w}")
    return out


def lifecycle(name: str, action: str, say: Say = _quiet, interactive: bool = True) -> None:
    """start = enable + start, stop = stop + disable (stays stopped across reboots), restart."""
    steps = {"start": ["enable", "start"], "stop": ["stop", "disable"], "restart": ["restart"]}[action]
    for s in steps:
        helper("unit", s, name, say=say, interactive=interactive)


def remove(name: str, cfg: Config, say: Say = _quiet, interactive: bool = True) -> str:
    """Remove from hosting: generated files, units, site, port. Keeps the project folder and certificate.
    The manifest moves to <apps_dir>/.deleted/."""
    helper("remove", name, say=say, interactive=interactive)
    # its router paths stay reserved (unassigned), so the address can be reused
    routers, _ = load_routers(Path(cfg.paths.apps_dir))
    for r in routers.values():
        if any(e.app == name for e in r.entries):
            for e in r.entries:
                if e.app == name:
                    e.app = None
            write_router(Path(cfg.paths.apps_dir), r)
            apply_router(r.name, say=say, interactive=interactive)
    src = Path(cfg.paths.apps_dir) / f"{name}.toml"
    dst_dir = Path(cfg.paths.apps_dir) / ".deleted"
    dst_dir.mkdir(exist_ok=True)
    dst = dst_dir / f"{name}.{time.strftime('%Y%m%d-%H%M%S')}.toml"
    shutil.move(src, dst)
    return str(dst)
