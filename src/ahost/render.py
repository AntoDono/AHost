"""Render the files an app needs: per-process unit override, env file and run script, plus the nginx site.

Pure functions of (manifest, config, ports, facts). No I/O except reading templates, so the same code runs in
`plan` (unprivileged) and in the root helper.
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

import jinja2

from .config import Config
from .models import App, Process, Route
from .policy import resolve

SHELL_OPS = ("&&", "||", ";", "|", ">", "<", "`", "$(")
CACHE_PATHS = {
    "huggingface": ".cache/huggingface", "torch": ".cache/torch", "uv": ".cache/uv", "pip": ".cache/pip",
    "npm": ".npm", "playwright": ".cache/ms-playwright",
}
GPU_CONTROL_DEVICES = ["/dev/nvidiactl", "/dev/nvidia-uvm", "/dev/nvidia-uvm-tools", "/dev/nvidia-modeset"]


@dataclass(frozen=True)
class Artifact:
    path: str
    content: str
    mode: int = 0o644
    kind: str = "file"  # "unit" | "env" | "run" | "site" | "static"


@dataclass
class Facts:
    """Things about the host that rendering depends on, gathered by the caller."""

    certs_present: set[str] = field(default_factory=set)  # cert names that exist
    gpu_minors: dict[str, int] = field(default_factory=dict)  # uuid -> minor
    manifest_path: str = ""


def _env() -> jinja2.Environment:
    loader = jinja2.FunctionLoader(lambda n: resources.files("ahost.templates").joinpath(n).read_text())
    env = jinja2.Environment(loader=loader, keep_trailing_newline=True, trim_blocks=False,
                             undefined=jinja2.StrictUndefined, autoescape=False)
    env.globals["q"] = shlex.quote
    return env


def static_template(name: str) -> str:
    return resources.files("ahost.templates").joinpath(name).read_text()


# ---------------------------------------------------------------- systemd quoting
def systemd_env_line(key: str, value: str) -> str:
    """One Environment= value, quoted for systemd (specifiers and backslashes escaped)."""
    v = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return f'"{key}={v}"'


def env_file_line(key: str, value: str) -> str:
    v = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'{key}="{v}"'


# ---------------------------------------------------------------- per process
def exec_line(cmd: str, proc: Process, cfg: Config) -> str:
    if proc.runtime.uv:
        args = " ".join(shlex.quote(a) for a in proc.runtime.uv_args)
        cmd = f"{shlex.quote(cfg.uv_bin)} run {args + ' ' if args else ''}{cmd}"
    if any(op in cmd for op in SHELL_OPS):
        return f"/bin/bash -c {shlex.quote(cmd)}"
    return cmd


def runtime_exports(app: App, proc: Process, cfg: Config) -> list[tuple[str, str]]:
    rt = proc.runtime
    ex: list[tuple[str, str]] = []
    if rt.venv:
        venv = resolve(app, rt.venv)
        ex += [("VIRTUAL_ENV", shlex.quote(venv)), ("PATH", f'{shlex.quote(venv + "/bin")}:"$PATH"')]
    elif rt.conda:
        prefix = f"{cfg.conda_root}/envs/{rt.conda}"
        ex += [("CONDA_PREFIX", shlex.quote(prefix)), ("CONDA_DEFAULT_ENV", shlex.quote(rt.conda)),
               ("PATH", f'{shlex.quote(prefix + "/bin")}:"$PATH"')]
    elif rt.node:
        ver = rt.node.lstrip("v")
        ex += [("PATH", f'{shlex.quote(f"{cfg.nvm_dir}/versions/node/v{ver}/bin")}:"$PATH"')]
    elif rt.uv:
        ex += [("PATH", f'{shlex.quote(os.path.dirname(cfg.uv_bin))}:"$PATH"')]
    return ex


def toolchain_paths(app: App, proc: Process, cfg: Config) -> list[str]:
    """Read-only paths a sandboxed process needs for its runtime."""
    rt, home = proc.runtime, cfg.home
    paths: list[str] = []
    if rt.uv:
        paths += [cfg.uv_bin, f"{home}/.local/share/uv/python"]
    if rt.venv:
        py = os.path.realpath(os.path.join(resolve(app, rt.venv), "bin", "python"))
        if py.startswith(home + "/"):
            paths.append(str(Path(py).parent.parent))  # uv/pyenv-managed interpreter outside the venv
    if rt.node:
        paths.append(f"{cfg.nvm_dir}/versions/node/v{rt.node.lstrip('v')}")
    if rt.conda:
        paths.append(f"{cfg.conda_root}")
    return paths


def sandbox_lines(app: App, proc: Process, cfg: Config, facts: Facts) -> list[str]:
    lvl = app.sandbox.level
    if lvl == "none":
        return []
    home = cfg.home
    rw = [app.workdir] + [resolve(app, p) for p in app.sandbox.rw_paths]
    rw += [f"{home}/{CACHE_PATHS[c]}" for c in app.sandbox.caches]
    ro = toolchain_paths(app, proc, cfg) + [resolve(app, p) for p in app.sandbox.ro_paths]
    lines = [
        "ProtectSystem=strict",
        "PrivateTmp=yes",
        "NoNewPrivileges=yes",
        # Hide all homes behind small tmpfs mounts; writes to unbound home paths fail loudly instead of eating RAM.
        "TemporaryFileSystem=/home:mode=0755,size=64M",
        "TemporaryFileSystem=/root:mode=0700,size=16M",
    ]
    lines += [f"BindPaths=-{p}" for p in rw]
    lines += [f"ReadWritePaths=-{p}" for p in rw]
    lines += [f"BindReadOnlyPaths=-{p}" for p in ro]
    if lvl == "strict":
        lines += [
            "CapabilityBoundingSet=",
            "AmbientCapabilities=",
            "SystemCallFilter=@system-service",
            "SystemCallArchitectures=native",
            "RestrictSUIDSGID=yes",
            "LockPersonality=yes",
            "ProtectKernelTunables=yes",
            "ProtectKernelModules=yes",
            "ProtectControlGroups=yes",
            "RestrictNamespaces=yes",
        ]
        if proc.gpus:
            lines.append("DevicePolicy=closed")
            for uuid in proc.gpus:
                minor = facts.gpu_minors.get(uuid)
                if minor is None:
                    raise ValueError(f"GPU {uuid} not found on this host")
                lines.append(f"DeviceAllow=/dev/nvidia{minor} rw")
            lines += [f"DeviceAllow={d} rw" for d in GPU_CONTROL_DEVICES]
    return lines


def render_process(app: App, pid: str, port: int | None, cfg: Config, facts: Facts) -> list[Artifact]:
    proc = app.processes[pid]
    inst = app.instance(pid)
    etc = cfg.paths.etc_dir
    user = app.user or cfg.run.default_user
    workdir = resolve(app, proc.runtime.dir) if proc.runtime.dir else app.workdir

    # --- env file: only values AHost owns (PORT, identity, GPUs). Wins over everything else.
    env = {"AHOST_APP": app.name, "AHOST_PROCESS": pid}
    if port is not None:
        env["PORT"] = str(port)
    if proc.gpus:
        env["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
        env["CUDA_VISIBLE_DEVICES"] = ",".join(proc.gpus)
    env_text = "# Generated by AHost. Do not edit.\n" + "".join(env_file_line(k, v) + "\n" for k, v in env.items())

    # --- unit override
    after = list(app.after) + [app.unit(d) for d in proc.after]
    wants = list(app.wants) + [app.unit(d) for d in proc.after]
    environment = [systemd_env_line(k, v) for k, v in {**app.env, **proc.env}.items()]
    extra = []
    for k, v in proc.systemd.items():
        for item in (v if isinstance(v, list) else [v]):
            extra.append((k, item))
    unit_text = _env().get_template("override.conf.j2").render(
        manifest_path=facts.manifest_path, app=app, proc=proc, pid_label=pid if app.multi else "",
        after=after, wants=wants, part_of=f"ahost@{app.name}.target" if app.multi else "",
        user=user, workdir=workdir, env_files=[resolve(app, f) for f in app.env_file],
        ahost_env=f"{etc}/apps/{inst}.env", environment=environment, extra=extra,
        sandbox=sandbox_lines(app, proc, cfg, facts),
    )

    # --- run script
    run_text = _env().get_template("run.sh.j2").render(
        manifest_path=facts.manifest_path, user=user, unit=app.unit(pid), dir=workdir,
        exports=runtime_exports(app, proc, cfg), command=exec_line(proc.command, proc, cfg),
    )
    unit_dir = f"{cfg.paths.systemd_dir}/ahost@{inst}.service.d"
    return [
        Artifact(f"{unit_dir}/override.conf", unit_text, 0o644, "unit"),
        Artifact(f"{etc}/apps/{inst}.env", env_text, 0o600, "env"),
        Artifact(f"{etc}/apps/{inst}.sh", run_text, 0o755, "run"),
    ]


# ---------------------------------------------------------------- nginx
def _location(app: App, r: Route, ports: dict[str, int]) -> str:
    L: list[str] = []
    if r.static:
        d = resolve(app, r.static).rstrip("/")
        if r.path == "/" or r.spa_fallback or r.static_root:
            L.append(f"root {d};")
            if r.spa_fallback:
                L.append(f"try_files $uri $uri.html $uri/index.html {r.spa_fallback};")
        else:
            L.append(f"alias {d}/;" if r.path.endswith("/") else f"alias {d};")
        if r.cache:
            L += [f"expires {r.cache};", 'add_header Cache-Control "public";']
    elif r.status is not None:
        L.append(f"return {r.status};")
    else:
        port = ports[r.to]
        L.append(f"proxy_pass http://127.0.0.1:{port}{'/' if r.strip_prefix else ''};")
        L += ["proxy_http_version 1.1;",
              "proxy_set_header Host $host;",
              "proxy_set_header X-Real-IP $remote_addr;",
              "proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;",
              "proxy_set_header X-Forwarded-Proto $scheme;"]
        if r.websocket:
            L += ["proxy_set_header Upgrade $http_upgrade;",
                  "proxy_set_header Connection $ahost_connection_upgrade;"]
        if r.streaming:
            L += ["proxy_buffering off;", "proxy_cache off;"]
        timeout = r.timeout or ("24h" if r.streaming else None)
        if timeout:
            L += [f"proxy_read_timeout {timeout};", f"proxy_send_timeout {timeout};"]
        if r.proxy_redirect_off:
            L.append("proxy_redirect off;")
    if r.max_body:
        L.append(f"client_max_body_size {r.max_body};")
    for k, v in r.headers.items():
        L.append(f'add_header {k} "{v}" always;')
    if r.raw:
        L += [ln.rstrip() for ln in r.raw.strip().splitlines()]
    body = "\n".join("    " + ln for ln in L)
    return f"location {r.path} {{\n{body}\n}}"


def render_site(app: App, ports: dict[str, int], cfg: Config, facts: Facts) -> Artifact | None:
    if not app.domains:
        return None
    locations = "\n\n".join(_location(app, r, ports) for r in app.effective_routes())
    https = [{"cert": cert, "domains": doms} for cert, doms in app.cert_groups().items()
             if cert in facts.certs_present]
    text = _env().get_template("site.conf.j2").render(
        manifest_path=facts.manifest_path, app=app, locations=locations, https_servers=https,
        ipv6=cfg.nginx.listen_ipv6, log_dir=cfg.nginx.log_dir, webroot=cfg.certs.webroot,
        live_dir=cfg.certs.live_dir, options_file=cfg.certs.options_file, dhparam=cfg.certs.dhparam,
    )
    return Artifact(f"{cfg.nginx.sites_dir}/{app.name}.conf", text, 0o644, "site")


def missing_certs(app: App, facts: Facts) -> list[str]:
    return [c for c in app.cert_groups() if c not in facts.certs_present]


def render_app(app: App, ports: dict[str, int], cfg: Config, facts: Facts) -> list[Artifact]:
    out: list[Artifact] = []
    for pid in app.processes:
        out += render_process(app, pid, ports.get(pid), cfg, facts)
    site = render_site(app, ports, cfg, facts)
    if site:
        out.append(site)
    return out


def render_static(cfg: Config) -> list[Artifact]:
    """Files installed once (by A4 / `ahost install`), shared by all apps."""
    sd = cfg.paths.systemd_dir
    default = _env().get_template("00-default.conf.j2").render(ipv6=cfg.nginx.listen_ipv6)
    return [
        Artifact(f"{sd}/ahost@.service", static_template("ahost@.service"), 0o644, "static"),
        Artifact(f"{sd}/ahost@.target", static_template("ahost@.target"), 0o644, "static"),
        Artifact(f"{cfg.nginx.sites_dir}/00-common.conf", static_template("00-common.conf"), 0o644, "static"),
        Artifact(f"{cfg.nginx.sites_dir}/00-default.conf", default, 0o644, "static"),
    ]
