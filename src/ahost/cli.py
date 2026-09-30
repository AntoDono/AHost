"""`ahost` command line. Unprivileged; anything that needs root goes through `sudo ahost-helper`."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Annotated

import typer

from . import host
from .config import Config, load
from .importer.legacy import import_app
from .manifest_io import dumps, load_all, load_app
from .models import App
from .plan import format_plan, make_plan, open_registry
from .ports import listening_ports

app = typer.Typer(no_args_is_help=True, add_completion=False, help="AHost: app hosting from one manifest per app.")
HELPER = os.environ.get("AHOST_HELPER", "/usr/local/sbin/ahost-helper")
STATE = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "ahost"

ConfigOpt = Annotated[Path | None, typer.Option("--config", "-c", help="config file (default $AHOST_CONFIG or /etc/ahost/ahost.toml)")]


def _cfg(path: Path | None) -> Config:
    try:
        return load(path)
    except FileNotFoundError as e:
        typer.secho(str(e), fg="red", err=True)
        raise typer.Exit(2)


def _apps(cfg: Config) -> dict[str, App]:
    apps, errors = load_all(Path(cfg.paths.apps_dir))
    for name, err in errors.items():
        typer.secho(f"{name}: invalid manifest: {err}", fg="red", err=True)
    return apps


def _one(cfg: Config, name: str) -> App:
    path = Path(cfg.paths.apps_dir) / f"{name}.toml"
    if not path.exists():
        typer.secho(f"no manifest {path}", fg="red", err=True)
        raise typer.Exit(1)
    return load_app(path)


def helper(*args: str) -> dict:
    """Run a helper verb via sudo. Raises typer.Exit on failure."""
    typer.secho(f"  $ sudo ahost-helper {' '.join(args)}", dim=True)
    r = subprocess.run(["sudo", HELPER, *args], capture_output=True, text=True, check=False)
    try:
        out = json.loads(r.stdout.strip().splitlines()[-1]) if r.stdout.strip() else {}
    except json.JSONDecodeError:
        out = {}
    if r.returncode != 0 or not out.get("ok"):
        msg = out.get("error") or r.stderr.strip() or r.stdout.strip()
        raise RuntimeError(f"ahost-helper {' '.join(args)} failed: {msg}")
    return out


# ---------------------------------------------------------------- checks
def _http(url: str, host_header: str | None = None, resolve_to: str | None = None, timeout: float = 8):
    """GET url -> (status, sha256 of body, content-type). Uses curl so --resolve works without DNS tricks."""
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


def wait_healthy(a: App, cfg: Config, ports: dict[str, int], timeout: int = 90) -> tuple[bool, str]:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        ok = True
        for pid, port in ports.items():
            proc = a.processes[pid]
            if port not in listening_ports():
                ok, last = False, f"{pid}: port {port} not listening yet"
                break
            path = proc.health or "/"
            code, _, _ = _http(f"http://127.0.0.1:{port}{path}", host_header=a.domains[0] if a.domains else None)
            if code == 0 or code >= 500 and proc.health:
                ok, last = False, f"{pid}: GET {path} -> {code}"
                break
        states = [host.unit_props(a.unit(p), "ActiveState").get("ActiveState") for p in a.processes]
        if any(s != "active" for s in states):
            ok, last = False, f"units: {states}"
        if ok:
            return True, "healthy"
        time.sleep(2)
    return False, last


def baseline_urls(a: App) -> list[tuple[str, str]]:
    """(label, url) pairs worth comparing: every domain's root, plus each simple route path and health path."""
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
        code, digest, ctype = _http(url, resolve_to="127.0.0.1")
        res[label] = {"status": code, "sha": digest, "type": ctype}
    return res


# ---------------------------------------------------------------- commands
@app.command()
def validate(names: Annotated[list[str] | None, typer.Argument()] = None, config: ConfigOpt = None):
    """Check manifests (schema + policy) without touching anything."""
    from . import policy
    cfg = _cfg(config)
    apps = _apps(cfg)
    bad = 0
    for name, a in sorted(apps.items()):
        if names and name not in names:
            continue
        rep = policy.check(a, cfg)
        for e in rep.errors:
            typer.secho(f"{name}: {e}", fg="red")
        for w in rep.warnings:
            typer.secho(f"{name}: {w}", fg="yellow")
        bad += not rep.ok
        if rep.ok:
            typer.secho(f"{name}: ok", fg="green")
    for e in policy.check_unique_domains(list(apps.values()), host.legacy_server_names(cfg)):
        typer.secho(f"note: {e}", fg="yellow")
    raise typer.Exit(1 if bad else 0)


@app.command()
def plan(names: Annotated[list[str] | None, typer.Argument()] = None, config: ConfigOpt = None,
         no_diff: Annotated[bool, typer.Option("--no-diff")] = False):
    """Show what `apply` would change. Read-only."""
    cfg = _cfg(config)
    apps = _apps(cfg)
    for name in names or sorted(apps):
        a = apps.get(name) or _one(cfg, name)
        pl = make_plan(a, cfg, apps, str(Path(cfg.paths.apps_dir) / f"{name}.toml"))
        typer.echo(format_plan(pl, show_diffs=not no_diff))
        typer.echo()


@app.command("import")
def import_(unit: Annotated[str, typer.Option(help="legacy unit, e.g. myapp.service")],
            name: Annotated[str, typer.Option(help="AHost app name")],
            site: Annotated[str | None, typer.Option(help="legacy nginx site file in sites-available")] = None,
            write: Annotated[bool, typer.Option("--write", help="write the manifest (default: print it)")] = False,
            force: Annotated[bool, typer.Option("--force", help="overwrite an existing manifest")] = False,
            config: ConfigOpt = None):
    """Generate a manifest from a legacy systemd unit (+ nginx site). Changes nothing on the system."""
    cfg = _cfg(config)
    ports = host.unit_ports(unit)
    m, rep = import_app(name, host.unit_file_text(unit, cfg), unit,
                        host.site_text(site, cfg) if site else None, site, cfg,
                        live_port=ports[0] if len(ports) == 1 else None)
    if len(ports) > 1:
        rep.notes.append(f"the unit listens on several ports: {ports}")
    header = (f"Imported from {unit}" + (f" + nginx site {site}" if site else "") +
              f" on {time.strftime('%Y-%m-%d')}.\nReview the import report before adopting.")
    text = dumps(m, header)
    if not write:
        typer.echo(text)
        typer.echo(rep.md())
        return
    out = Path(cfg.paths.apps_dir) / f"{name}.toml"
    if out.exists() and not force:
        typer.secho(f"{out} exists (use --force)", fg="red", err=True)
        raise typer.Exit(1)
    out.write_text(text)
    rpt = Path(cfg.paths.apps_dir) / "_import-report.md"
    old = rpt.read_text() if rpt.exists() else "# Import report\n\n"
    import re as _re
    old = _re.sub(rf"(?ms)^## {_re.escape(name)}\n.*?(?=^## |\Z)", "", old)
    rpt.write_text(old.rstrip() + "\n\n" + rep.md())
    typer.secho(f"wrote {out}; report in {rpt}", fg="green")
    typer.echo(rep.md())


@app.command()
def baseline(name: str, compare: Annotated[bool, typer.Option("--compare", help="compare with the last baseline")] = False,
             config: ConfigOpt = None):
    """Record (or compare) how the app responds today, through local nginx."""
    cfg = _cfg(config)
    a = _one(cfg, name)
    STATE.mkdir(parents=True, exist_ok=True)
    now = take_baseline(a)
    prev_files = sorted(STATE.glob(f"baseline-{name}-*.json"))
    if compare:
        if not prev_files:
            typer.secho("no previous baseline", fg="red")
            raise typer.Exit(1)
        prev = json.loads(prev_files[-1].read_text())
        diffs = compare_baselines(prev, now)
        for line in diffs:
            typer.secho(line, fg="yellow")
        typer.secho("baseline matches" if not diffs else f"{len(diffs)} difference(s) vs {prev_files[-1].name}",
                    fg="green" if not diffs else "yellow")
        raise typer.Exit(0 if not diffs else 1)
    f = STATE / f"baseline-{name}-{time.strftime('%Y%m%d-%H%M%S')}.json"
    f.write_text(json.dumps(now, indent=2))
    for k, v in now.items():
        typer.echo(f"  {v['status']}  {v['type']:<24} {k}")
    typer.secho(f"saved {f}", fg="green")


def compare_baselines(prev: dict, now: dict) -> list[str]:
    out = []
    for k, v in prev.items():
        n = now.get(k)
        if n is None:
            out.append(f"{k}: missing now")
        elif n["status"] != v["status"]:
            out.append(f"{k}: status {v['status']} -> {n['status']}")
        elif v["type"].startswith(("text/css", "application/javascript", "image/", "font/")) and n["sha"] != v["sha"]:
            out.append(f"{k}: static content changed")
    return out


@app.command()
def apply(name: str, config: ConfigOpt = None):
    """Make the system match the manifest (for apps that are not being adopted from a legacy unit)."""
    cfg = _cfg(config)
    a = _one(cfg, name)
    try:
        out = helper("apply-unit", name)
        typer.echo(f"  ports: {out['ports']}  changed: {len(out['changed'])} file(s)")
        if a.enabled:
            helper("unit", "enable", name)
            helper("unit", "restart" if out["changed"] else "start", name)
            ok, why = wait_healthy(a, cfg, {p: n for p, n in out["ports"].items()})
            if not ok:
                raise RuntimeError(f"not healthy: {why}")
        else:
            helper("unit", "stop", name)
            helper("unit", "disable", name)
        if a.domains:
            helper("apply-site", name)  # http-only until the cert exists
            if helper("cert", name)["issued"]:
                helper("apply-site", name)
    except RuntimeError as e:
        typer.secho(str(e), fg="red")
        raise typer.Exit(1)
    typer.secho(f"{name}: applied", fg="green")


@app.command()
def adopt(name: str, config: ConfigOpt = None,
          yes: Annotated[bool, typer.Option("--yes", help="don't ask for confirmation")] = False):
    """Cut an app over from its legacy unit + site to AHost. Rolls back automatically on failure."""
    cfg = _cfg(config)
    a = _one(cfg, name)
    if not a.legacy or not a.legacy.unit:
        typer.secho("manifest has no [legacy] unit; use `ahost apply`", fg="red")
        raise typer.Exit(1)
    apps = _apps(cfg)
    pl = make_plan(a, cfg, apps, str(Path(cfg.paths.apps_dir) / f"{name}.toml"))
    typer.echo(format_plan(pl, show_diffs=False))
    if pl.errors:
        raise typer.Exit(1)
    snap = take_baseline(a)
    STATE.mkdir(parents=True, exist_ok=True)
    (STATE / f"baseline-{name}-{time.strftime('%Y%m%d-%H%M%S')}-pre-adopt.json").write_text(json.dumps(snap, indent=2))
    typer.echo(f"  baseline recorded ({len(snap)} checks)")
    if not yes and not typer.confirm(f"Stop {a.legacy.unit} and switch {name} to AHost now?"):
        raise typer.Exit(1)
    done: list[str] = []
    try:
        helper("legacy-unit", "stop", name); done.append("legacy-stopped")
        helper("legacy-unit", "disable", name); done.append("legacy-disabled")
        out = helper("apply-unit", name); done.append("unit-written")
        helper("unit", "enable", name)
        helper("unit", "start", name); done.append("started")
        ok, why = wait_healthy(a, cfg, out["ports"],
                               timeout=max([p.start_timeout or 90 for p in a.processes.values()]))
        if not ok:
            raise RuntimeError(f"new unit not healthy: {why}")
        typer.secho("  service healthy", fg="green")
        if a.domains:
            helper("swap-site" if a.legacy.site else "apply-site", name); done.append("site-swapped")
        time.sleep(1)
        diffs = compare_baselines(snap, take_baseline(a))
        if diffs:
            raise RuntimeError("baseline differs after cutover:\n    " + "\n    ".join(diffs))
    except RuntimeError as e:
        typer.secho(f"FAILED: {e}", fg="red")
        typer.secho("rolling back...", fg="yellow")
        _rollback(name, a, done)
        raise typer.Exit(1)
    typer.secho(f"{name}: adopted. Legacy {a.legacy.unit} is stopped and disabled (not deleted). "
                f"Soak it, then run the manual checks. `ahost rollback {name}` undoes it.", fg="green")


def _rollback(name: str, a: App, done: list[str] | None = None) -> None:
    done = done if done is not None else ["legacy-stopped", "legacy-disabled", "unit-written", "started", "site-swapped"]
    errors = []
    steps = []
    if "site-swapped" in done and a.legacy and a.legacy.site:
        steps.append(("unswap-site", name))
    if "started" in done:
        steps += [("unit", "stop", name), ("unit", "disable", name)]
    if "legacy-disabled" in done:
        steps.append(("legacy-unit", "enable", name))
    if "legacy-stopped" in done:
        steps.append(("legacy-unit", "start", name))
    for s in steps:
        try:
            helper(*s)
        except RuntimeError as e:
            errors.append(str(e))
    if errors:
        typer.secho("rollback had errors (see MIGRATION manual rollback):\n  " + "\n  ".join(errors), fg="red")
    else:
        typer.secho("rolled back: legacy unit and site are active again", fg="yellow")


@app.command()
def rollback(name: str, config: ConfigOpt = None):
    """Undo `adopt`: generated site out, legacy site back, AHost unit stopped, legacy unit started."""
    cfg = _cfg(config)
    _rollback(name, _one(cfg, name))


@app.command()
def status(config: ConfigOpt = None):
    """All apps: systemd state, ports, domains."""
    cfg = _cfg(config)
    apps = _apps(cfg)
    reg = open_registry(cfg)
    for name, a in sorted(apps.items()):
        for pid in a.processes:
            unit = a.unit(pid)
            st = host.unit_props(unit, "ActiveState", "UnitFileState")
            port = reg.get(name, pid) or a.processes[pid].port
            legacy = ""
            if a.legacy and a.legacy.unit:
                ls = host.unit_props(a.legacy.unit, "ActiveState").get("ActiveState")
                legacy = f"  legacy {a.legacy.unit}: {ls}"
            typer.echo(f"{unit:<40} {st.get('ActiveState', '?'):<9} {st.get('UnitFileState', '?'):<9} "
                       f":{port or '-':<6} {' '.join(a.domains)}{legacy}")


@app.command()
def logs(name: str, follow: Annotated[bool, typer.Option("-f", "--follow")] = False,
         lines: Annotated[int, typer.Option("-n")] = 100, config: ConfigOpt = None):
    """journalctl for all of an app's processes."""
    cfg = _cfg(config)
    a = _one(cfg, name)
    args = ["journalctl", "--no-pager", "-n", str(lines)]
    for p in a.processes:
        args += ["-u", a.unit(p)]
    if follow:
        args.append("-f")
    os.execvp(args[0], args)


def helper_main() -> None:  # entry point used by /usr/local/sbin/ahost-helper
    from .helper import main
    sys.exit(main())
