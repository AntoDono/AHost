"""Turn a hand-written systemd unit + nginx site into an AHost manifest, plus a report of everything that
didn't map 1:1. The goal is behavioral fidelity: same command, port, env, user, ordering and nginx behavior."""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Config
from ..models import SYSTEMD_EXTRA_ALLOWED, App
from .parse import Directive, Unit, parse_environment, parse_nginx, parse_unit, seconds, unquote

IGNORED_ORDER = {"network.target", "network-online.target", "multi-user.target", "default.target"}
BIND_RE = re.compile(r"(--bind|-b)(\s+|=)(?P<host>[\w.\-\[\]:]*?):(?P<port>\d{2,5})\b")
# proxy_set_header values that the generated config sets anyway
STD_HEADERS = {
    "host": "$host", "x-real-ip": "$remote_addr", "x-forwarded-for": "$proxy_add_x_forwarded_for",
    "x-forwarded-proto": "$scheme", "upgrade": "$http_upgrade",
}


@dataclass
class ImportReport:
    name: str
    notes: list[str] = field(default_factory=list)  # intended/normalized differences
    todo: list[str] = field(default_factory=list)  # must be reviewed by a human before adopting

    def md(self) -> str:
        out = [f"## {self.name}", ""]
        out += [f"- ⚠ {t}" for t in self.todo] or ["- nothing needs manual review"]
        out += [f"- {n}" for n in self.notes]
        return "\n".join(out) + "\n"


# ---------------------------------------------------------------- unit -> process
def _rel(path: str, workdir: str) -> str:
    wd = workdir.rstrip("/") + "/"
    return path.removeprefix(wd)


def _split_exec(execstart: str) -> list[str]:
    s = execstart.lstrip("-@:+!")
    return shlex.split(s)


def _venv_of(exe: str) -> str | None:
    p = Path(exe)
    if p.parent.name == "bin" and (p.parent.parent / "pyvenv.cfg").exists():
        return str(p.parent.parent)
    return None


def map_command(argv: list[str], workdir: str, cfg: Config, rep: ImportReport) -> tuple[str, dict, int | None]:
    """ExecStart argv -> (command, runtime, port found in a --bind)."""
    runtime: dict = {}
    exe = argv[0]
    rest = argv[1:]
    venv = _venv_of(exe)
    nvm = re.match(re.escape(cfg.nvm_dir) + r"/versions/node/v([\d.]+)/bin/(\w+)$", exe)
    if venv:
        runtime["venv"] = _rel(venv, workdir)
        argv = [os.path.basename(exe), *rest]
    elif os.path.basename(exe) == "uv" and rest[:1] == ["run"]:
        runtime["uv"] = True
        rest = rest[1:]
        uv_args = []
        while rest and rest[0].startswith("-"):
            uv_args.append(rest.pop(0))
        if uv_args:
            runtime["uv_args"] = uv_args
        argv = rest
        if exe != cfg.uv_bin:
            rep.notes.append(f"uv binary {exe} → {cfg.uv_bin}")
    elif nvm:
        runtime["node"] = nvm.group(1)
        argv = [nvm.group(2), *rest]
    cmd = shlex.join(argv)
    port = None
    m = BIND_RE.search(cmd)
    if m:
        port = int(m.group("port"))
        cmd = cmd[: m.start("port")] + "$PORT" + cmd[m.end("port"):]
        rep.notes.append(f"hard-coded bind port {port} → $PORT (port pinned to {port}, same behavior)")
    return cmd, runtime, port


def read_env_keys(path: str) -> dict[str, str]:
    """KEY=value pairs of a dotenv file (values only used for PORT detection; never written anywhere)."""
    out: dict[str, str] = {}
    try:
        text = Path(path).read_text()
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.removeprefix("export ").split("=", 1)
        out[k.strip()] = v.strip().strip("'\"")
    return out


def unit_to_manifest(unit: Unit, name: str, cfg: Config, rep: ImportReport, *, live_port: int | None) -> dict:
    S = "Service"
    workdir = (unit.get(S, "WorkingDirectory") or "").rstrip("/")
    if not workdir:
        rep.todo.append("no WorkingDirectory in the legacy unit; set workdir by hand")
        workdir = cfg.home
    m: dict = {"name": name, "description": unit.get("Unit", "Description") or "", "workdir": workdir}

    user = unit.get(S, "User") or "root"
    if user != cfg.run.default_user:
        m["user"] = user
    if user == "root":
        rep.todo.append("runs as root today (no User= or User=root); add it to /etc/ahost/root.allow to keep that, "
                        "or move it to the app user as a separate step")
    if unit.get(S, "Group"):
        m["group"] = unit.get(S, "Group")

    env_files = [f.lstrip("-") for f in unit.all(S, "EnvironmentFile") if f]
    if env_files:
        m["env_file"] = [_rel(f, workdir) for f in env_files]
    env = parse_environment(unit.all(S, "Environment"))
    env_port = env.pop("PORT", None)
    if env:
        m["env"] = env

    for key in ("After", "Wants", "Requires"):
        units = [u for v in unit.all("Unit", key) for u in v.split() if u not in IGNORED_ORDER]
        if units:
            target = "wants" if key in ("Wants", "Requires") else "after"
            m.setdefault(target, [])
            m[target] += [u for u in units if u not in m[target]]
            if key == "Requires":
                rep.notes.append(f"Requires={' '.join(units)} → wants (weaker: a dependency failing won't stop the app)")

    execs = unit.all(S, "ExecStart")
    if len([e for e in execs if e]) != 1:
        rep.todo.append(f"expected one ExecStart, found {len(execs)}")
    argv = _split_exec([e for e in execs if e][-1]) if execs else ["true"]
    command, runtime, bind_port = map_command(argv, workdir, cfg, rep)
    proc: dict = {"command": command}
    if runtime:
        proc["runtime"] = runtime

    # ---- port: explicit bind > Environment PORT > .env PORT > live listener
    dotenv_port = None
    for f in env_files:
        v = read_env_keys(f).get("PORT")
        if v and v.isdigit():
            dotenv_port = int(v)
    candidates = [p for p in (bind_port, int(env_port) if env_port and env_port.isdigit() else None,
                              dotenv_port, live_port) if p]
    if candidates:
        proc["port"] = candidates[0]
        if len(set(candidates)) > 1:
            rep.todo.append(f"port sources disagree: bind={bind_port} env={env_port} .env={dotenv_port} "
                            f"live={live_port}; manifest uses {candidates[0]}")
        if dotenv_port and not bind_port:
            rep.notes.append(f"port {dotenv_port} comes from the app's .env PORT; AHost sets the same PORT")
        if live_port and live_port != candidates[0]:
            rep.todo.append(f"the unit currently listens on {live_port}, not {candidates[0]}")
    if bind_port is None and "$PORT" not in command and proc.get("port") and dotenv_port is None and not env_port:
        rep.notes.append("the command doesn't take $PORT; the port is only used for routing and health checks")

    restart = unit.get(S, "Restart") or "no"
    proc["restart"] = restart if restart in ("always", "on-failure", "no") else "always"
    if restart not in ("always", "on-failure", "no"):
        rep.notes.append(f"Restart={restart} → always")
    rs = seconds(unit.get(S, "RestartSec"))
    proc["restart_sec"] = rs if rs is not None else 0  # systemd default is 100ms
    if unit.get(S, "Type"):
        proc["type"] = unit.get(S, "Type")
    km = unit.get(S, "KillMode")
    proc["kill_mode"] = km or "control-group"
    for key, field_ in (("TimeoutStopSec", "stop_timeout"), ("TimeoutStartSec", "start_timeout")):
        if unit.get(S, key):
            proc[field_] = seconds(unit.get(S, key))
    if unit.get(S, "ExecReload"):
        proc["reload"] = unit.get(S, "ExecReload")

    extra: dict = {}
    for key in sorted(unit.keys(S)):
        if key in SYSTEMD_EXTRA_ALLOWED:
            vals = unit.all(S, key)
            if key in ("StandardOutput", "StandardError") and vals[-1] in ("journal", "syslog", "inherit"):
                rep.notes.append(f"{key}={vals[-1]} dropped (the journal is the default; syslog is forwarded from it)")
                continue
            extra[key] = vals if len(vals) > 1 else vals[0]
    if extra:
        proc["systemd"] = extra
    handled = {"User", "Group", "WorkingDirectory", "EnvironmentFile", "Environment", "ExecStart", "Restart",
               "RestartSec", "Type", "KillMode", "TimeoutStopSec", "TimeoutStartSec", "ExecReload",
               "SyslogIdentifier", *SYSTEMD_EXTRA_ALLOWED}
    for key in sorted(unit.keys(S) - handled):
        rep.todo.append(f"[Service] {key}={unit.get(S, key)} has no manifest equivalent")
    if unit.get(S, "SyslogIdentifier"):
        rep.notes.append(f"SyslogIdentifier={unit.get(S, 'SyslogIdentifier')} → ahost-{name}")
    if user != "root" and "PATH" in env:
        rep.notes.append("PATH is set by the legacy unit; kept as-is (the runtime prepends its bin dir)")
    m["processes"] = {"main": proc}
    return m


# ---------------------------------------------------------------- site -> routes
def _servers(tree: list[Directive]) -> list[Directive]:
    return [d for d in tree if d.name == "server"]


def _listens(srv: Directive) -> set[str]:
    return {" ".join(d.args) for d in srv.find("listen")}


def _is_https(srv: Directive) -> bool:
    return any("443" in lst for lst in _listens(srv))


def _proxy_port(target: str) -> int | None:
    m = re.match(r"https?://(?:127\.0\.0\.1|localhost|\[::1\]):(\d+)/?$", target)
    return int(m.group(1)) if m else None


def _loc_route(loc: Directive, workdir: str, port: int | None, rep: ImportReport,
               seen_ports: list[int] | None = None) -> dict:
    path = " ".join(loc.args)
    r: dict = {"path": path}
    raw: list[str] = []
    hdrs = {}
    timeouts: list[str] = []
    for d in loc.block or []:
        n, a = d.name, d.args
        if n == "proxy_pass":
            p = _proxy_port(a[0])
            if p is None:
                rep.todo.append(f"location {path}: proxy_pass {a[0]} isn't a local port; kept raw")
                raw.append(f"proxy_pass {a[0]};")
                continue
            if seen_ports is not None:
                seen_ports.append(p)
            if port is not None and p != port:
                rep.todo.append(f"location {path}: proxies to :{p} but the app's port is {port}")
            r["to"] = "main"
            if a[0].endswith("/") and path != "/":
                r["strip_prefix"] = True
        elif n == "alias" or n == "root":
            r["static"] = _rel(a[0].rstrip("/"), workdir)
            if n == "root":
                rep.notes.append(f"location {path}: root → static route")
        elif n == "return" and len(a) == 1 and a[0].isdigit():
            r["status"] = int(a[0])
        elif n == "proxy_http_version":
            continue
        elif n == "proxy_set_header":
            hdrs[a[0].lower()] = unquote(" ".join(a[1:]))
        elif n == "proxy_cache_bypass":
            rep.notes.append(f"location {path}: proxy_cache_bypass dropped (no proxy cache is configured)")
        elif n == "proxy_buffering" and a == ["off"]:
            r["streaming"] = True
        elif n in ("proxy_read_timeout", "proxy_send_timeout", "send_timeout"):
            timeouts.append(a[0])
        elif n == "proxy_redirect" and a == ["off"]:
            r["proxy_redirect_off"] = True
        elif n == "client_max_body_size":
            r["max_body"] = a[0]
        elif n == "expires":
            r["cache"] = a[0]
        else:
            from .parse import render_directive
            raw.append(render_directive(d))
    if "to" in r:
        r["websocket"] = "upgrade" in hdrs
        r.setdefault("streaming", False)
        extra_h = {k: v for k, v in hdrs.items() if k not in STD_HEADERS and k != "connection"}
        for k, v in extra_h.items():
            raw.append(f"proxy_set_header {k} {v};")
        missing = [k for k in ("host", "x-real-ip") if k not in hdrs]
        if missing:
            rep.notes.append(f"location {path}: now also sends {', '.join(missing)} headers (standard set)")
        if "x-forwarded-proto" not in hdrs:
            rep.notes.append(f"location {path}: now also sends X-Forwarded-For/-Proto")
        if hdrs.get("connection") in ("upgrade", "'upgrade'") or hdrs.get("connection") == "upgrade":
            rep.notes.append(f"location {path}: Connection 'upgrade' (always) → map (upgrade only when requested)")
        if timeouts:
            r["timeout"] = max(timeouts, key=lambda t: _dur_s(t))
        elif r["streaming"]:
            r["timeout"] = "60s"  # keep nginx's default rather than the 24h streaming default
    if raw:
        r["raw"] = "\n".join(raw)
    if not any(k in r for k in ("to", "static", "status")):
        rep.todo.append(f"location {path}: no proxy_pass/alias/root/return; needs manual mapping")
        r["status"] = 404
    return r


def _dur_s(t: str) -> float:
    m = re.match(r"(\d+)(ms|s|m|h|d)?$", t)
    if not m:
        return 0
    return int(m.group(1)) * {"ms": 0.001, "s": 1, None: 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


def site_to_manifest(site_text: str, site_name: str, workdir: str, port: int | None, rep: ImportReport) -> dict:
    tree = parse_nginx(site_text)
    https = [s for s in _servers(tree) if _is_https(s)]
    http = [s for s in _servers(tree) if not _is_https(s)]
    out: dict = {"domains": [], "routes": [], "certs": {}, "proxy": {}, "proxy_ports": []}
    named: list[str] = []
    if not https:
        rep.todo.append(f"site {site_name}: no HTTPS server block")
    for srv in https:
        names = [n for d in srv.find("server_name") for n in d.args if n != "_"]
        cert = None
        for d in srv.find("ssl_certificate"):
            m = re.search(r"/live/([^/]+)/", d.args[0])
            cert = m.group(1) if m else None
        for n in names:
            if n not in out["domains"]:
                out["domains"].append(n)
            if cert:
                out["certs"][n] = cert
        if cert and len(names) > 1:
            rep.todo.append(f"cert {cert} is used for {names}; check it actually covers every name "
                            "(`certbot certificates`), otherwise give each name its own cert")
        for d in srv.block or []:
            if d.name == "location" and d.args and d.args[0].startswith("@"):
                from .parse import render_directive
                named.append(render_directive(d))
                rep.notes.append(f"named location {d.args[0]} → raw_server (verbatim)")
            elif d.name == "location":
                out["routes"].append(_loc_route(d, workdir, port, rep, out["proxy_ports"]))
            elif d.name == "client_max_body_size":
                out["proxy"]["max_body"] = d.args[0]
            elif d.name == "gzip" and d.args == ["on"]:
                out["proxy"]["gzip"] = True
            elif d.name.startswith("gzip") or d.name in ("server_name", "listen", "ssl_certificate", "ssl_certificate_key", "ssl_dhparam",
                            "include", "access_log", "error_log"):
                continue
            elif d.name in ("send_timeout", "proxy_read_timeout", "proxy_send_timeout"):
                rep.todo.append(f"server-level {d.name} {' '.join(d.args)}; move into routes if needed")
            else:
                rep.todo.append(f"server-level `{d.name} {' '.join(d.args)}` has no manifest equivalent")
    # port 80: certbot-style redirect blocks → http_redirect (default). Anything else needs review.
    redirected = set()
    for srv in http:
        names = [n for d in srv.find("server_name") for n in d.args]
        text = str([x.name for x in srv.block or []])
        locs = srv.find("location")
        loc_redirects = bool(locs) and all(
            any(x.name == "return" and x.args[:1] == ["301"] for x in (loc.block or [])) for loc in locs)
        if "if" in text or loc_redirects or any(d.name == "return" and d.args[:1] == ["301"]
                                                for d in srv.block or []):
            redirected.update(names)
        elif srv.find("location"):
            rep.todo.append(f"port-80 block for {names} serves content; generated config redirects to HTTPS")
    not_redirected = [d for d in out["domains"] if d not in redirected]
    if not_redirected:
        rep.notes.append(f"HTTP→HTTPS redirect added for {not_redirected} (today port 80 falls through to the "
                         "default site for these names)")
    # unique certs: when every domain uses a cert named after itself, keep; else note
    if named:
        out["proxy"]["raw_server"] = "\n".join(named)
    if not out["certs"]:
        del out["certs"]
    if not out["proxy"]:
        del out["proxy"]
    return out


def import_app(name: str, unit_text: str, unit_name: str, site_text: str | None, site_name: str | None,
               cfg: Config, *, live_port: int | None = None) -> tuple[dict, ImportReport]:
    rep = ImportReport(name)
    unit = parse_unit(unit_text, unit_name)
    m = unit_to_manifest(unit, name, cfg, rep, live_port=live_port)
    port = m["processes"]["main"].get("port")
    if site_text is not None:
        s = site_to_manifest(site_text, site_name or "", m["workdir"], port, rep)
        if port is None and len(set(s["proxy_ports"])) == 1:
            port = s["proxy_ports"][0]
            m["processes"]["main"]["port"] = port
            rep.notes.append(f"port {port} taken from the nginx site's proxy_pass (the app sets its own port; "
                             "AHost exports the same PORT)")
        elif port is None and s["proxy_ports"]:
            rep.todo.append(f"the site proxies to several ports {sorted(set(s['proxy_ports']))}; set ports by hand")
        m["domains"] = s["domains"]
        if s.get("certs"):
            m["certs"] = s["certs"]
        if s["routes"]:
            m["routes"] = s["routes"]
        if s.get("proxy"):
            m["proxy"] = s["proxy"]
    m["legacy"] = {"unit": unit_name, **({"site": site_name} if site_name else {})}
    App.model_validate(m)  # raises if the import produced something invalid
    return m, rep
