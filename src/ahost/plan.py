"""Compute what `apply` would do for an app, without changing anything."""

from __future__ import annotations

import difflib
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from . import host, policy, render
from .config import Config
from .gpu import minor_map
from .models import App
from .ports import PortError, Registry, listening_ports


@dataclass
class FileChange:
    path: str
    kind: str
    status: str  # "create" | "update" | "same" | "unreadable"
    diff: str = ""


@dataclass
class Plan:
    app: str
    ports: dict[str, int] = field(default_factory=dict)
    files: list[FileChange] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return any(f.status != "same" for f in self.files)


def open_registry(cfg: Config) -> Registry:
    """The real registry if readable, else an in-memory copy (plan must work without root)."""
    db = Path(cfg.paths.state_dir) / "ahost.db"
    try:
        readable = db.exists() and os.access(db, os.R_OK)
    except PermissionError:  # state dir not traversable (not in the ahost group, or a new login is needed)
        readable = False
    if readable:
        reg = Registry(":memory:", cfg)
        src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        src.backup(reg.db)
        src.close()
        return reg
    return Registry(":memory:", cfg)


def facts_for(app: App, cfg: Config, manifest_path: str) -> render.Facts:
    return render.Facts(
        certs_present=host.certs_present(cfg),
        gpu_minors=minor_map() if any(p.gpus for p in app.processes.values()) else {},
        manifest_path=manifest_path,
    )


def _compare(a: render.Artifact) -> FileChange:
    p = Path(a.path)
    if not p.exists():
        return FileChange(a.path, a.kind, "create", a.content)
    try:
        old = p.read_text()
    except PermissionError:
        return FileChange(a.path, a.kind, "unreadable")
    if old == a.content:
        return FileChange(a.path, a.kind, "same")
    diff = "".join(difflib.unified_diff(old.splitlines(True), a.content.splitlines(True), a.path, a.path + " (new)"))
    return FileChange(a.path, a.kind, "update", diff)


def make_plan(app: App, cfg: Config, all_apps: dict[str, App], manifest_path: str) -> Plan:
    pl = Plan(app.name)
    rep = policy.check(app, cfg)
    pl.errors += rep.errors
    pl.warnings += rep.warnings
    legacy_names = host.legacy_server_names(cfg)
    others = {**all_apps, app.name: app}
    pl.errors += [e for e in policy.check_unique_domains(list(others.values()), legacy_names)
                  if any(f"domain {d} " in e for d in app.domains)]
    reg = open_registry(cfg)
    listening = listening_ports()
    try:
        pl.ports = reg.resolve(app, listening=listening)
    except PortError as e:
        pl.errors.append(str(e))
        return pl
    for pid, port in pl.ports.items():
        cur = reg.get(app.name, pid)
        if cur is None:
            where = f" (in use now by {listening[port] or 'another process'})" if port in listening else ""
            pl.steps.append(f"reserve port {port} for {pid}{where}")
        elif cur != port:
            pl.steps.append(f"move {pid} from port {cur} to {port}")
    facts = facts_for(app, cfg, manifest_path)
    try:
        arts = render.render_app(app, pl.ports, cfg, facts)
    except ValueError as e:
        pl.errors.append(str(e))
        return pl
    pl.files = [_compare(a) for a in arts]
    unit_changed = any(f.status != "same" and f.kind in ("unit", "env", "run") for f in pl.files)
    site_changed = any(f.status != "same" and f.kind == "site" for f in pl.files)
    if unit_changed:
        pl.steps.append("write unit override(s), env file(s), run script(s); systemctl daemon-reload")
        verb = "enable --now" if app.enabled else "stop + disable"
        pl.steps.append(f"{verb} " + " ".join(app.unit(p) for p in app.processes)
                        + (" (restart if running)" if app.enabled else ""))
    for cert in render.missing_certs(app, facts):
        doms = app.cert_groups()[cert]
        pl.steps.append(f"issue certificate {cert!r} for {', '.join(doms)} (certbot webroot; DNS must point here)")
    legacy_sites = sorted({legacy_names[d] for d in app.domains if d in legacy_names})
    if site_changed:
        if legacy_sites:
            pl.steps.append(f"swap nginx: disable legacy {legacy_sites} + enable ahost.d/{app.name}.conf "
                            "(one nginx -t + reload)")
        else:
            pl.steps.append(f"write ahost.d/{app.name}.conf; nginx -t; reload")
    return pl


def format_plan(pl: Plan, *, show_diffs: bool = True) -> str:
    out = [f"== {pl.app}"]
    for e in pl.errors:
        out.append(f"  ERROR   {e}")
    for w in pl.warnings:
        out.append(f"  warning {w}")
    if pl.ports:
        out.append("  ports:  " + ", ".join(f"{p}={n}" for p, n in pl.ports.items()))
    for f in pl.files:
        out.append(f"  {f.status:<10} {f.kind:<5} {f.path}")
    if pl.steps:
        out.append("  steps:")
        out += [f"    {i}. {s}" for i, s in enumerate(pl.steps, 1)]
    elif not pl.errors:
        out.append("  nothing to do")
    if show_diffs:
        for f in pl.files:
            if f.status == "update" and f.diff:
                out.append("")
                out.append(f.diff.rstrip())
            elif f.status == "create" and f.kind != "env":
                out.append("")
                out.append(f"--- new file {f.path}")
                out.append(f.diff.rstrip())
    return "\n".join(out)
