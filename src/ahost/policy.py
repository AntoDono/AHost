"""Security policy checks that need server context (config, allow-lists, the filesystem).

These run in the unprivileged CLI (to report problems early) *and* in the root helper (to enforce them).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .models import App

DENIED_ENV = {
    "LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT", "NODE_OPTIONS", "PYTHONSTARTUP", "BASH_ENV", "ENV",
    "PROMPT_COMMAND", "PYTHONINSPECT", "PERL5OPT", "RUBYOPT",
}
DENIED_ENV_ROOT = DENIED_ENV | {"PATH", "PYTHONPATH", "PYTHONHOME", "NODE_PATH"}

# nginx directives never allowed in raw snippets (the helper sets logging/TLS itself; the rest can read/write
# arbitrary files or run code as the nginx master).
DENIED_NGINX = re.compile(
    r"(^|[;{}\s])(access_log|error_log|include|load_module|ssl_certificate\w*|ssl_trusted_certificate|"
    r"ssl_dhparam|lua\w*|perl\w*|js_\w+|njs\w*|root|alias|client_body_temp_path|proxy_temp_path|"
    r"fastcgi_temp_path|uwsgi_temp_path|scgi_temp_path|proxy_cache_path|pid|user|env|daemon|"
    r"master_process|working_directory|worker_\w+)\s",
    re.MULTILINE,
)
NGINX_NAME_DEF = re.compile(r"^\s*(upstream|map\s+\S+)\s+\$?([A-Za-z0-9_]+)", re.MULTILINE)


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def read_list(path: Path) -> set[str]:
    """One name per line, '#' comments allowed."""
    if not path.exists():
        return set()
    out = set()
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.add(line)
    return out


def _under(p: str, roots: list[str]) -> bool:
    rp = os.path.realpath(p)
    return any(rp == os.path.realpath(r) or rp.startswith(os.path.realpath(r).rstrip("/") + "/") for r in roots)


def resolve(app: App, rel: str) -> str:
    return rel if rel.startswith("/") else os.path.join(app.workdir, rel)


def check(app: App, cfg: Config, *, root_allow: set[str] | None = None, check_fs: bool = True) -> Report:
    r = Report()
    root_allow = root_allow if root_allow is not None else read_list(Path(cfg.paths.etc_dir) / "root.allow")

    user = app.user or cfg.run.default_user
    if user == "root" and app.name not in root_allow:
        r.errors.append(f"user=root is only allowed for apps listed in {cfg.paths.etc_dir}/root.allow")
    if user not in ("root", cfg.run.default_user):
        r.errors.append(f"user {user!r} is not allowed (only {cfg.run.default_user!r}, or root via root.allow)")

    # paths
    paths = [("workdir", app.workdir)]
    paths += [("env_file", resolve(app, f)) for f in app.env_file]
    paths += [(f"route {rt.path} static", resolve(app, rt.static)) for rt in app.routes if rt.static]
    paths += [("sandbox.rw_paths", resolve(app, p)) for p in app.sandbox.rw_paths]
    paths += [("sandbox.ro_paths", resolve(app, p)) for p in app.sandbox.ro_paths]
    for pid, p in app.processes.items():
        if p.runtime.venv:
            paths.append((f"{pid}.runtime.venv", resolve(app, p.runtime.venv)))
        if p.runtime.dir:
            paths.append((f"{pid}.runtime.dir", resolve(app, p.runtime.dir)))
    for what, p in paths:
        if not os.path.isabs(app.workdir):
            r.errors.append("workdir must be absolute")
            break
        if not _under(p, cfg.paths.allowed_roots):
            r.errors.append(f"{what}: {p} is outside allowed_roots {cfg.paths.allowed_roots}")
        elif cfg.paths.deny_paths and _under(p, cfg.paths.deny_paths):
            r.errors.append(f"{what}: {p} is inside a denied path")
        if check_fs and what in ("workdir", "env_file") and not os.path.exists(p):
            r.errors.append(f"{what}: {p} does not exist")

    # env
    denied = DENIED_ENV_ROOT if user == "root" else DENIED_ENV
    envs = [("env", app.env)] + [(f"{pid}.env", p.env) for pid, p in app.processes.items()]
    for where, env in envs:
        for k in env:
            if k in denied:
                r.errors.append(f"{where}: {k} is not allowed" + (" for root apps" if user == "root" else ""))
            if k == "PORT":
                r.errors.append(f"{where}: PORT is managed by AHost; set `port` instead")

    # raw nginx
    raws = [("proxy.raw_server", app.proxy.raw_server), ("proxy.raw_http", app.proxy.raw_http)]
    raws += [(f"route {rt.path} raw", rt.raw) for rt in app.routes]
    for where, raw in raws:
        if not raw:
            continue
        m = DENIED_NGINX.search(raw + " ")
        if m:
            r.errors.append(f"{where}: directive {m.group(2)!r} is not allowed in raw nginx")
        if raw.count("{") != raw.count("}"):
            r.errors.append(f"{where}: unbalanced braces")
    if app.proxy.raw_http:
        for kind, name in NGINX_NAME_DEF.findall(app.proxy.raw_http):
            if not name.startswith(f"{app.name.replace('-', '_')}_"):
                r.errors.append(f"proxy.raw_http: {kind.split()[0]} name {name!r} must start with "
                                f"'{app.name.replace('-', '_')}_'")

    # sandbox caveats
    if app.sandbox.level != "none" and user == "root":
        r.warnings.append("sandboxing a root app hides /root too; bind its caches explicitly")
    for pid, p in app.processes.items():
        if p.gpus and any("cuda:" in v for v in {**app.env, **p.env}.values()):
            r.warnings.append(f"{pid}: env mentions cuda:N; CUDA_VISIBLE_DEVICES renumbers cards from 0")
    return r


def check_unique_domains(apps: list[App], legacy_names: dict[str, str]) -> list[str]:
    """Domains must be unique across manifests and legacy enabled sites.

    ``legacy_names`` maps server_name -> legacy site file. A manifest may claim a legacy site's names only if it
    declares that site in ``legacy.site`` (that's how adoption swaps it).
    """
    errors: list[str] = []
    seen: dict[str, str] = {}
    for a in apps:
        for d in a.domains:
            if d in seen:
                errors.append(f"domain {d} is claimed by both {seen[d]} and {a.name}")
            seen[d] = a.name
            site = legacy_names.get(d)
            if site and not (a.legacy and a.legacy.site == site):
                errors.append(f"domain {d} ({a.name}) is still served by legacy site {site!r}")
    return errors


def reserved_domains(routers: dict, ui_domain: str | None) -> dict[str, str]:
    """Domains that belong to routers or the dashboard: domain -> owner label."""
    out = {r.domain: f"router {r.name}" for r in routers.values()}
    if ui_domain:
        out[ui_domain] = "the AHost dashboard"
    return out


def check_router(router, apps: list[App], routers: dict, legacy_names: dict[str, str],
                 ui_domain: str | None) -> list[str]:
    """A router's domain must not be anyone else's."""
    errors = []
    d = router.domain
    for a in apps:
        if d in a.domains:
            errors.append(f"domain {d} already belongs to app {a.name}")
    for other in routers.values():
        if other.name != router.name and other.domain == d:
            errors.append(f"domain {d} already belongs to router {other.name}")
    if d in legacy_names:
        errors.append(f"domain {d} is served by legacy site {legacy_names[d]!r}")
    if ui_domain and d == ui_domain:
        errors.append(f"domain {d} is the AHost dashboard's")
    return errors
