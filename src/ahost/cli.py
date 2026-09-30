"""`ahost` command line. Unprivileged; anything that needs root goes through `sudo ahost-helper`."""

from __future__ import annotations

import getpass
import json
import os
import re
import time
from pathlib import Path
from typing import Annotated

import typer

from . import host, ops
from .config import Config, load
from .importer.legacy import import_app
from .manifest_io import dumps, load_all, load_app
from .models import App
from .plan import format_plan, make_plan, open_registry

app = typer.Typer(no_args_is_help=True, add_completion=False, help="AHost: app hosting from one manifest per app.")
user_app = typer.Typer(no_args_is_help=True, help="Dashboard users.")
app.add_typer(user_app, name="user")
router_app = typer.Typer(no_args_is_help=True, help="Routers: one shared domain, a path per app (domain/<path>).")
app.add_typer(router_app, name="router")
STATE = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "ahost"

ConfigOpt = Annotated[Path | None, typer.Option("--config", "-c", help="config file (default $AHOST_CONFIG or /etc/ahost/ahost.toml)")]


def _say(msg: str) -> None:
    typer.secho(f"  {msg}", dim=msg.startswith("$"))


def _cfg(path: Path | None) -> Config:
    try:
        return load(path)
    except FileNotFoundError as e:
        typer.secho(str(e), fg="red", err=True)
        raise typer.Exit(2) from None


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


def _fail(e: Exception) -> None:
    typer.secho(str(e), fg="red")
    raise typer.Exit(1)


# ---------------------------------------------------------------- read-only
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


JsonOpt = Annotated[bool, typer.Option("--json", help="machine-readable output")]


@app.command()
def plan(names: Annotated[list[str] | None, typer.Argument()] = None, config: ConfigOpt = None,
         no_diff: Annotated[bool, typer.Option("--no-diff")] = False, as_json: JsonOpt = False):
    """Show what `apply` would change. Read-only. Exit code 1 if any plan has errors."""
    cfg = _cfg(config)
    apps = _apps(cfg)
    out, bad = [], False
    for name in names or sorted(apps):
        a = apps.get(name) or _one(cfg, name)
        pl = make_plan(a, cfg, apps, str(Path(cfg.paths.apps_dir) / f"{name}.toml"))
        bad |= bool(pl.errors)
        if as_json:
            out.append({"app": name, "ok": not pl.errors, "errors": pl.errors, "warnings": pl.warnings,
                        "ports": pl.ports, "steps": pl.steps, "changed": pl.changed,
                        "files": [{"path": f.path, "kind": f.kind, "status": f.status} for f in pl.files]})
        else:
            typer.echo(format_plan(pl, show_diffs=not no_diff))
            typer.echo()
    if as_json:
        typer.echo(json.dumps(out, indent=2))
    raise typer.Exit(1 if bad else 0)


@app.command()
def status(config: ConfigOpt = None, as_json: JsonOpt = False):
    """All apps: systemd state, ports, domains, router paths."""
    cfg = _cfg(config)
    apps = _apps(cfg)
    reg = open_registry(cfg)
    mounts: dict[str, list[str]] = {}
    for r in _routers(cfg).values():
        for e in r.entries:
            if e.app:
                mounts.setdefault(e.app, []).append(f"{r.domain}{e.path}/")
    rows = []
    for name, a in sorted(apps.items()):
        for pid in a.processes:
            unit = a.unit(pid)
            st = host.unit_props(unit, "ActiveState", "UnitFileState")
            pinned = a.processes[pid].port
            port = reg.get(name, pid) or (pinned if isinstance(pinned, int) else None)
            if as_json:
                rows.append({"app": name, "process": pid, "unit": unit, "active": st.get("ActiveState"),
                             "enabled": st.get("UnitFileState"), "port": port, "domains": a.domains,
                             "router_paths": mounts.get(name, [])})
                continue
            legacy = ""
            if a.legacy and a.legacy.unit:
                ls = host.unit_props(a.legacy.unit, "ActiveState").get("ActiveState")
                legacy = f"  legacy {a.legacy.unit}: {ls}"
            typer.echo(f"{unit:<40} {st.get('ActiveState', '?'):<9} {st.get('UnitFileState', '?'):<9} "
                       f":{port or '-':<6} {' '.join(a.domains + mounts.get(name, []))}{legacy}")
    if as_json:
        typer.echo(json.dumps(rows, indent=2))


@app.command()
def logs(name: str, follow: Annotated[bool, typer.Option("-f", "--follow")] = False,
         lines: Annotated[int, typer.Option("-n")] = 100, config: ConfigOpt = None):
    """journalctl for all of an app's processes."""
    a = _one(_cfg(config), name)
    args = ["journalctl", "--no-pager", "-n", str(lines)]
    for p in a.processes:
        args += ["-u", a.unit(p)]
    if follow:
        args.append("-f")
    os.execvp(args[0], args)


# ---------------------------------------------------------------- import / baseline
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
    old = re.sub(rf"(?ms)^## {re.escape(name)}\n.*?(?=^## |\Z)", "", old)
    rpt.write_text(old.rstrip() + "\n\n" + rep.md())
    typer.secho(f"wrote {out}; report in {rpt}", fg="green")
    typer.echo(rep.md())


@app.command()
def baseline(name: str, compare: Annotated[bool, typer.Option("--compare", help="compare with the last baseline")] = False,
             config: ConfigOpt = None):
    """Record (or compare) how the app responds today, through local nginx."""
    a = _one(_cfg(config), name)
    STATE.mkdir(parents=True, exist_ok=True)
    now = ops.take_baseline(a)
    prev_files = sorted(STATE.glob(f"baseline-{name}-*.json"))
    if compare:
        if not prev_files:
            typer.secho("no previous baseline", fg="red")
            raise typer.Exit(1)
        diffs = ops.compare_baselines(json.loads(prev_files[-1].read_text()), now)
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


# ---------------------------------------------------------------- changes
@app.command()
def apply(name: str, config: ConfigOpt = None):
    """Make the system match the manifest (for apps not being adopted from a legacy unit)."""
    cfg = _cfg(config)
    try:
        ops.apply(name, cfg, say=_say)
    except ops.OpError as e:
        _fail(e)
    typer.secho(f"{name}: applied", fg="green")


@app.command()
def start(name: str, config: ConfigOpt = None):
    """Enable + start (comes back after reboots)."""
    _cfg(config)
    try:
        ops.lifecycle(name, "start", say=_say)
    except ops.OpError as e:
        _fail(e)


@app.command()
def stop(name: str, config: ConfigOpt = None):
    """Stop + disable (stays stopped across reboots)."""
    _cfg(config)
    try:
        ops.lifecycle(name, "stop", say=_say)
    except ops.OpError as e:
        _fail(e)


@app.command()
def restart(name: str, config: ConfigOpt = None):
    """Restart all of an app's processes."""
    _cfg(config)
    try:
        ops.lifecycle(name, "restart", say=_say)
    except ops.OpError as e:
        _fail(e)


@app.command()
def remove(name: str, config: ConfigOpt = None,
           yes: Annotated[bool, typer.Option("--yes")] = False):
    """Remove from hosting. Keeps the project folder, its data and the certificate."""
    cfg = _cfg(config)
    _one(cfg, name)
    if not yes and typer.prompt(f"Type the app name to remove {name} from hosting") != name:
        raise typer.Exit(1)
    try:
        dst = ops.remove(name, cfg, say=_say)
    except ops.OpError as e:
        _fail(e)
    typer.secho(f"{name} removed from hosting; manifest kept at {dst}", fg="green")


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
    snap = ops.take_baseline(a)
    STATE.mkdir(parents=True, exist_ok=True)
    (STATE / f"baseline-{name}-{time.strftime('%Y%m%d-%H%M%S')}-pre-adopt.json").write_text(json.dumps(snap, indent=2))
    typer.echo(f"  baseline recorded ({len(snap)} checks)")
    if not yes and not typer.confirm(f"Stop {a.legacy.unit} and switch {name} to AHost now?"):
        raise typer.Exit(1)
    done: list[str] = []
    h = lambda *args: ops.helper(*args, say=_say)
    try:
        h("legacy-unit", "stop", name); done.append("legacy-stopped")
        h("legacy-unit", "disable", name); done.append("legacy-disabled")
        out = h("apply-unit", name); done.append("unit-written")
        h("unit", "enable", name)
        h("unit", "start", name); done.append("started")
        ok, why = ops.wait_healthy(a, out["ports"], timeout=max([p.start_timeout or 90 for p in a.processes.values()]))
        if not ok:
            raise ops.OpError(f"new unit not healthy: {why}")
        typer.secho("  service healthy", fg="green")
        if a.domains:
            h("swap-site" if a.legacy.site else "apply-site", name); done.append("site-swapped")
        time.sleep(1)
        diffs = ops.compare_baselines(snap, ops.take_baseline(a))
        if diffs:
            raise ops.OpError("baseline differs after cutover:\n    " + "\n    ".join(diffs))
    except ops.OpError as e:
        typer.secho(f"FAILED: {e}", fg="red")
        typer.secho("rolling back...", fg="yellow")
        _rollback(name, a, done)
        raise typer.Exit(1) from None
    typer.secho(f"{name}: adopted. Legacy {a.legacy.unit} is stopped and disabled (not deleted). "
                f"`ahost rollback {name}` undoes it.", fg="green")


def _rollback(name: str, a: App, done: list[str] | None = None) -> None:
    done = done if done is not None else ["legacy-stopped", "legacy-disabled", "unit-written", "started", "site-swapped"]
    steps = []
    if "site-swapped" in done and a.legacy and a.legacy.site:
        steps.append(("unswap-site", name))
    if "started" in done:
        steps += [("unit", "stop", name), ("unit", "disable", name)]
    if "legacy-disabled" in done:
        steps.append(("legacy-unit", "enable", name))
    if "legacy-stopped" in done:
        steps.append(("legacy-unit", "start", name))
    errors = []
    for s in steps:
        try:
            ops.helper(*s, say=_say)
        except ops.OpError as e:
            errors.append(str(e))
    if errors:
        typer.secho("rollback had errors (see the manual rollback in the docs):\n  " + "\n  ".join(errors), fg="red")
    else:
        typer.secho("rolled back: legacy unit and site are active again", fg="yellow")


@app.command()
def rollback(name: str, config: ConfigOpt = None):
    """Undo `adopt`: generated site out, legacy site back, AHost unit stopped, legacy unit started."""
    _rollback(name, _one(_cfg(config), name))


# ---------------------------------------------------------------- DNS / UI
@app.command()
def ddclient(apply_: Annotated[bool, typer.Option("--apply", help="write the new host list and restart ddclient")] = False,
             config: ConfigOpt = None):
    """Sync ddclient's host list with the manifests (+ dns.static_hosts + ui.domain). Credentials are untouched."""
    _cfg(config)
    try:
        out = ops.helper("ddclient", "apply" if apply_ else "plan", say=_say)
    except ops.OpError as e:
        _fail(e)
    for zone in sorted(set(out["added"]) | set(out["removed"])):
        for h_ in out["added"].get(zone, []):
            typer.secho(f"  + {h_}", fg="green")
        for h_ in out["removed"].get(zone, []):
            typer.secho(f"  - {h_}", fg="red")
    for h_ in out["unplaceable"]:
        typer.secho(f"  ! {h_}: no ddclient block (credentials) for its domain; add one by hand", fg="yellow")
    if not out["changed"]:
        typer.secho("ddclient host list already in sync", fg="green")
    elif apply_:
        typer.secho(f"applied; previous config saved to {out.get('backup')}", fg="green")
    else:
        typer.echo("dry run. Re-run with --apply.")


@app.command("ui-site")
def ui_site(config: ConfigOpt = None):
    """Create/refresh the dashboard's nginx site and certificate (ui.domain)."""
    _cfg(config)
    try:
        out = ops.helper("ui-site", say=_say)
    except ops.OpError as e:
        _fail(e)
    typer.secho(f"https://{out['domain']} ready" + (" (new certificate)" if out["cert_issued"] else ""), fg="green")


@app.command()
def guide():
    """Print the guide for AI agents and scripts: how to host an app with this CLI."""
    from importlib import resources
    try:
        text = resources.files("ahost").joinpath("guide.md").read_text()
    except (FileNotFoundError, OSError):  # running from a source checkout
        text = (Path(__file__).resolve().parents[2] / "docs/agents.md").read_text()
    typer.echo(text)


@app.command()
def serve(config: ConfigOpt = None):
    """Run the web dashboard (normally started by ahost.service)."""
    import uvicorn

    from .api.app import create_app
    cfg = _cfg(config)
    bind_host, _, port = cfg.ui.bind.rpartition(":")
    uvicorn.run(create_app(cfg), host=bind_host, port=int(port), proxy_headers=True,
                forwarded_allow_ips="127.0.0.1", log_level="info", access_log=False)


@user_app.command("add")
def user_add(username: str, config: ConfigOpt = None):
    """Create a dashboard user (run as the ahost user: `sudo -u ahost ahost user add NAME`)."""
    from .api import auth
    cfg = _cfg(config)
    pw = getpass.getpass("Password (min 12 chars): ")
    if pw != getpass.getpass("Repeat: "):
        _fail(ValueError("passwords don't match"))
    try:
        auth.Store(cfg).add_user(username, pw)
    except ValueError as e:
        _fail(e)
    typer.secho(f"user {username} created", fg="green")


@user_app.command("passwd")
def user_passwd(username: str, config: ConfigOpt = None):
    """Change a dashboard user's password (also signs them out everywhere)."""
    from .api import auth
    cfg = _cfg(config)
    pw = getpass.getpass("New password (min 12 chars): ")
    if pw != getpass.getpass("Repeat: "):
        _fail(ValueError("passwords don't match"))
    try:
        auth.Store(cfg).set_password(username, pw)
    except ValueError as e:
        _fail(e)
    typer.secho("password changed; existing sessions signed out", fg="green")


@user_app.command("list")
def user_list(config: ConfigOpt = None):
    """List dashboard users."""
    from .api import auth
    for u in auth.Store(_cfg(config)).users():
        typer.echo(f"{u['name']:<20} created {u['created']}")


@user_app.command("delete")
def user_delete(username: str, config: ConfigOpt = None):
    """Delete a dashboard user and their sessions."""
    from .api import auth
    auth.Store(_cfg(config)).delete_user(username)
    typer.secho(f"user {username} deleted", fg="green")


# ---------------------------------------------------------------- routers
def _routers(cfg: Config):
    from .manifest_io import load_routers
    routers, errors = load_routers(Path(cfg.paths.apps_dir))
    for name, err in errors.items():
        typer.secho(f"router {name}: invalid: {err}", fg="red", err=True)
    return routers


@router_app.command("list")
def router_list(config: ConfigOpt = None, as_json: JsonOpt = False):
    """Routers and their paths."""
    cfg = _cfg(config)
    if as_json:
        typer.echo(json.dumps([r.model_dump() for r in _routers(cfg).values()], indent=2))
        return
    for r in _routers(cfg).values():
        typer.secho(f"{r.name}  https://{r.domain}" + (f"  (/ -> {r.index}/)" if r.index else ""), bold=True)
        for e in r.entries:
            to = e.app or typer.style("reserved", dim=True)
            typer.echo(f"  {e.path + '/':<24} {to}" + ("" if e.strip else "  (prefix passed through)"))


@router_app.command("plan")
def router_plan(name: str, config: ConfigOpt = None):
    """Show the nginx site a router would get (nothing changes)."""
    from .manifest_io import routers_dir
    from .plan import plan_router
    cfg = _cfg(config)
    routers = _routers(cfg)
    if name not in routers:
        _fail(ValueError(f"no router {name!r} in {routers_dir(Path(cfg.paths.apps_dir))}"))
    rp = plan_router(routers[name], cfg, _apps(cfg), routers, str(routers_dir(Path(cfg.paths.apps_dir)) / f"{name}.toml"))
    for e in rp.errors:
        typer.secho(f"  ERROR   {e}", fg="red")
    for w in rp.warnings:
        typer.secho(f"  warning {w}", fg="yellow")
    if rp.file:
        typer.echo(f"  {rp.file.status:<10} site  {rp.file.path}")
        if rp.file.status != "same":
            typer.echo("\n" + (rp.file.diff if rp.file.status == "update" else rp.content).rstrip())
    raise typer.Exit(1 if rp.errors else 0)


@router_app.command("apply")
def router_apply(name: str, config: ConfigOpt = None):
    """Write the router's nginx site (and get its certificate the first time)."""
    _cfg(config)
    try:
        ops.apply_router(name, say=_say)
    except ops.OpError as e:
        _fail(e)
    typer.secho(f"router {name} applied", fg="green")


NoApplyOpt = Annotated[bool, typer.Option("--no-apply", help="only edit the router file; apply later")]


def _edit_router(cfg: Config, name: str, edit, no_apply: bool) -> None:
    """Change a router file, validate it, then apply (a failed apply puts the old file back)."""
    from .manifest_io import write_router
    from .models import Router
    routers = _routers(cfg)
    if name not in routers:
        _fail(ValueError(f"no router {name!r} (see `ahost router list`)"))
    old = routers[name]
    new = old.model_copy(deep=True)
    try:
        edit(new)
        new = Router.model_validate(new.model_dump())
    except ValueError as e:
        _fail(e)
    apps = _apps(cfg)
    missing = [e.app for e in new.entries if e.app and e.app not in apps]
    if missing:
        _fail(ValueError(f"no app named {missing[0]!r}"))
    write_router(Path(cfg.paths.apps_dir), new)
    if no_apply:
        typer.secho(f"router {name} saved; run `ahost router apply {name}`", fg="green")
        return
    try:
        ops.apply_router(name, say=_say)
    except ops.OpError as e:
        write_router(Path(cfg.paths.apps_dir), old)
        _fail(e)
    typer.secho(f"router {name} applied", fg="green")


@router_app.command("create")
def router_create(name: str, domain: str, description: Annotated[str, typer.Option()] = "",
                  config: ConfigOpt = None, no_apply: NoApplyOpt = False):
    """New router for DOMAIN (its DNS must point here). Applying it gets the certificate."""
    from . import policy
    from .manifest_io import routers_dir, write_router
    from .models import Router
    cfg = _cfg(config)
    try:
        r = Router(name=name, domain=domain.lower(), description=description)
    except ValueError as e:
        _fail(e)
    if (routers_dir(Path(cfg.paths.apps_dir)) / f"{name}.toml").exists():
        _fail(ValueError(f"router {name} already exists"))
    errs = policy.check_router(r, list(_apps(cfg).values()), _routers(cfg), host.legacy_server_names(cfg),
                               cfg.ui.domain)
    if errs:
        _fail(ValueError("; ".join(errs)))
    write_router(Path(cfg.paths.apps_dir), r)
    if no_apply:
        typer.secho(f"router {name} saved; run `ahost router apply {name}`", fg="green")
        return
    try:
        ops.apply_router(name, say=_say)
    except ops.OpError as e:
        _fail(e)
    typer.secho(f"https://{r.domain} ready. Add paths with `ahost router add {name} /path --app APP`.", fg="green")


@router_app.command("add")
def router_add(name: str, path: str, app_name: Annotated[str | None, typer.Option("--app", help="app to point it at; omit to reserve")] = None,
               no_strip: Annotated[bool, typer.Option("--no-strip", help="pass the prefix through (app is configured for it)")] = False,
               config: ConfigOpt = None, no_apply: NoApplyOpt = False):
    """Add PATH to a router, pointed at --app or reserved."""
    from .models import Entry

    def edit(r):
        e = Entry(path=path, app=app_name, strip=not no_strip)
        if r.entry(e.path):
            raise ValueError(f"{e.path} is already on router {r.name} (use `ahost router assign`)")
        r.entries.append(e)
    _edit_router(_cfg(config), name, edit, no_apply)


@router_app.command("assign")
def router_assign(name: str, path: str, app_name: Annotated[str | None, typer.Argument(metavar="APP", help="omit to make the path reserved")] = None,
                  strip: Annotated[bool | None, typer.Option("--strip/--no-strip", help="change prefix stripping")] = None,
                  config: ConfigOpt = None, no_apply: NoApplyOpt = False):
    """Point an existing PATH at APP (or reserve it again when APP is omitted)."""
    def edit(r):
        e = r.entry(path)
        if e is None:
            raise ValueError(f"{path} is not on router {r.name} (use `ahost router add`)")
        e.app = app_name
        if strip is not None:
            e.strip = strip
    _edit_router(_cfg(config), name, edit, no_apply)


@router_app.command("drop")
def router_drop(name: str, path: str, config: ConfigOpt = None, no_apply: NoApplyOpt = False):
    """Remove PATH from a router."""
    def edit(r):
        e = r.entry(path)
        if e is None:
            raise ValueError(f"{path} is not on router {r.name}")
        r.entries.remove(e)
        if r.index == e.path:
            r.index = None
    _edit_router(_cfg(config), name, edit, no_apply)


@router_app.command("remove")
def router_remove(name: str, config: ConfigOpt = None):
    """Take a router's site down. Its file and certificate are kept; apps are not touched."""
    _cfg(config)
    try:
        ops.helper("remove-router", name, say=_say)
    except ops.OpError as e:
        _fail(e)
    typer.secho(f"router {name} removed from nginx (its file under routers/ is kept)", fg="green")


if __name__ == "__main__":
    app()
