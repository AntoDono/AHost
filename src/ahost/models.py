"""Manifest model: one App per <apps_dir>/<name>.toml.

The same model validates manifests for the CLI, the API and the root helper. Anything security-relevant
(users, paths, env names, raw nginx) is checked here or in ``policy`` so the helper never trusts its caller.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
PROC_RE = re.compile(r"^[a-z][a-z0-9-]{0,19}$")
DOMAIN_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
DURATION_RE = re.compile(r"^\d+(ms|s|m|h|d|y)?$")
SIZE_RE = re.compile(r"^\d+[kKmMgG]?$")
UNIT_RE = re.compile(r"^[A-Za-z0-9@:._\\-]+\.(service|target|socket|mount|timer)$")

MAIN = "main"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=False)


class Runtime(Strict):
    """How the command's environment is prepared. At most one of venv/uv/conda/node."""

    venv: str | None = None  # path to a venv, relative to workdir (or absolute)
    uv: bool = False  # command is run as `uv run <command>`
    uv_args: list[str] = []  # e.g. ["--frozen"]
    conda: str | None = None  # conda env name
    node: str | None = None  # node version managed by nvm, e.g. "22.19.0"
    dir: str | None = None  # subdirectory of workdir to run in

    @model_validator(mode="after")
    def _one_kind(self) -> Runtime:
        kinds = [k for k in ("venv", "conda", "node") if getattr(self, k)] + (["uv"] if self.uv else [])
        if len(kinds) > 1:
            raise ValueError(f"runtime: pick one of venv/uv/conda/node, got {kinds}")
        if self.uv_args and not self.uv:
            raise ValueError("runtime.uv_args needs uv = true")
        return self

    @property
    def kind(self) -> str:
        if self.venv:
            return "venv"
        if self.uv:
            return "uv"
        if self.conda:
            return "conda"
        if self.node:
            return "node"
        return "plain"


# systemd directives a manifest may set verbatim (hardening/limits carried over from legacy units).
SYSTEMD_EXTRA_ALLOWED = {
    "PrivateTmp", "NoNewPrivileges", "ProtectSystem", "ProtectHome", "ReadWritePaths", "ReadOnlyPaths",
    "LimitNOFILE", "Nice", "UMask", "ProtectKernelTunables", "ProtectControlGroups", "RestrictSUIDSGID",
    "LockPersonality", "StandardOutput", "StandardError",
}


class Process(Strict):
    command: str
    runtime: Runtime = Runtime()
    port: int | Literal["auto"] | None = None
    gpus: list[str] = []  # GPU UUIDs
    health: str | None = None
    env: dict[str, str] = {}
    after: list[str] = []  # other process ids of the same app
    restart: Literal["always", "on-failure", "no"] = "always"
    restart_sec: int = 5
    stop_timeout: int | None = None  # seconds; None = systemd default (90)
    start_timeout: int | None = None
    type: Literal["simple", "exec", "notify", "forking"] = "simple"
    reload: str | None = None
    kill_mode: Literal["control-group", "mixed", "process"] = "control-group"
    memory_max: str | None = None
    cpu_quota: str | None = None
    systemd: dict[str, str | list[str]] = {}  # allow-listed extra [Service] directives

    @field_validator("command")
    @classmethod
    def _cmd(cls, v: str) -> str:
        v = v.strip()
        if not v or "\n" in v:
            raise ValueError("command must be a single non-empty line")
        return v

    @field_validator("port")
    @classmethod
    def _port(cls, v: Any) -> Any:
        if isinstance(v, int) and not (1 <= v <= 65535):
            raise ValueError("port out of range")
        return v

    @field_validator("systemd")
    @classmethod
    def _systemd(cls, v: dict) -> dict:
        bad = set(v) - SYSTEMD_EXTRA_ALLOWED
        if bad:
            raise ValueError(f"systemd: directives not allowed: {sorted(bad)}")
        return v

    @field_validator("health")
    @classmethod
    def _health(cls, v: str | None) -> str | None:
        if v is not None and not v.startswith("/"):
            raise ValueError("health must be a path starting with /")
        return v


class Route(Strict):
    path: str  # "/", "/api/", "= /exact", "~ ^/regex"
    to: str | None = None  # process id
    static: str | None = None  # directory, relative to workdir
    static_root: bool = False  # nginx `root` (dir + full URI) instead of `alias` (dir replaces the path prefix)
    status: int | None = None  # fixed response (e.g. 404 to block a path)
    spa_fallback: str | None = None
    strip_prefix: bool = False  # proxy_pass with a trailing slash
    websocket: bool = True
    streaming: bool = True  # proxy_buffering off
    timeout: str | None = None  # read/send timeout; None = nginx default (60s) unless streaming
    max_body: str | None = None
    cache: str | None = None
    headers: dict[str, str] = {}
    proxy_redirect_off: bool = False
    raw: str | None = None  # extra directives inside this location (checked by policy)

    @model_validator(mode="after")
    def _target(self) -> Route:
        n = sum(x is not None for x in (self.to, self.static, self.status))
        if n != 1:
            raise ValueError(f"route {self.path!r}: set exactly one of to/static/status")
        if self.static_root and not self.static:
            raise ValueError("static_root only applies to static routes")
        if self.spa_fallback and not self.static:
            raise ValueError("spa_fallback only applies to static routes")
        for f in ("timeout", "cache"):
            val = getattr(self, f)
            if val is not None and not DURATION_RE.match(val):
                raise ValueError(f"{f}: bad duration {val!r}")
        if self.max_body is not None and not SIZE_RE.match(self.max_body):
            raise ValueError(f"max_body: bad size {self.max_body!r}")
        return self

    @field_validator("path")
    @classmethod
    def _path(cls, v: str) -> str:
        if not v.startswith(("/", "= /", "~ ", "~* ")):
            raise ValueError(f"route path must start with '/', '= /', '~ ' or '~* ': {v!r}")
        if any(c in v for c in "{};\n"):
            raise ValueError(f"route path contains forbidden characters: {v!r}")
        return v


class Proxy(Strict):
    max_body: str | None = None
    gzip: bool = False
    http_redirect: bool = True  # port 80 redirects to https (plus ACME challenges)
    raw_server: str | None = None
    raw_http: str | None = None


class Sandbox(Strict):
    level: Literal["none", "standard", "strict"] = "none"
    rw_paths: list[str] = []
    ro_paths: list[str] = []
    caches: list[Literal["huggingface", "torch", "uv", "pip", "npm", "playwright"]] = []


class Legacy(Strict):
    """Where an adopted app came from. Used by adopt/rollback; dropped once finalized."""

    unit: str | None = None  # e.g. "myapp.service"
    site: str | None = None  # file name in sites-available, e.g. "cms-api"


class App(Strict):
    name: str
    description: str = ""
    workdir: str
    domains: list[str] = []
    user: str | None = None
    group: str | None = None
    env_file: list[str] = []
    env: dict[str, str] = {}
    after: list[str] = []
    wants: list[str] = []
    enabled: bool = True
    certs: dict[str, str] = {}  # domain -> existing certbot cert name
    processes: dict[str, Process] = {}
    routes: list[Route] = []
    proxy: Proxy = Proxy()
    sandbox: Sandbox = Sandbox()
    legacy: Legacy | None = None

    # ---- shorthand: top-level command/runtime/port/... define process "main" ----
    @model_validator(mode="before")
    @classmethod
    def _shorthand(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        data = dict(data)  # never mutate the caller's dict
        keys = set(Process.model_fields) & set(data)
        if "command" in data:
            if data.get("processes"):
                raise ValueError("use either top-level command or [processes.*], not both")
            proc = {k: data.pop(k) for k in list(keys)}
            if "env" in proc:  # top-level env stays app-wide
                data["env"] = proc.pop("env")
            if "after" in proc:  # top-level after = unit ordering, not process ordering
                data["after"] = proc.pop("after")
            data["processes"] = {MAIN: proc}
        if isinstance(data.get("env_file"), str):
            data["env_file"] = [data["env_file"]]
        return data

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not NAME_RE.match(v):
            raise ValueError(f"name must match {NAME_RE.pattern}")
        return v

    @field_validator("domains")
    @classmethod
    def _domains(cls, v: list[str]) -> list[str]:
        for d in v:
            if not DOMAIN_RE.match(d):
                raise ValueError(f"invalid domain {d!r}")
        if len(set(v)) != len(v):
            raise ValueError("duplicate domains")
        return v

    @field_validator("env")
    @classmethod
    def _env(cls, v: dict[str, str]) -> dict[str, str]:
        for k in v:
            if not ENV_NAME_RE.match(k):
                raise ValueError(f"invalid env name {k!r}")
        return v

    @field_validator("after", "wants")
    @classmethod
    def _units(cls, v: list[str]) -> list[str]:
        for u in v:
            if not UNIT_RE.match(u):
                raise ValueError(f"invalid unit name {u!r}")
        return v

    @model_validator(mode="after")
    def _consistency(self) -> App:
        for pid, p in self.processes.items():
            if not PROC_RE.match(pid):
                raise ValueError(f"process id {pid!r} must match {PROC_RE.pattern}")
            for dep in p.after:
                if dep not in self.processes or dep == pid:
                    raise ValueError(f"process {pid}: after={dep!r} is not another process of this app")
            for k in p.env:
                if not ENV_NAME_RE.match(k):
                    raise ValueError(f"process {pid}: invalid env name {k!r}")
        for r in self.routes:
            if r.to is not None:
                if r.to not in self.processes:
                    raise ValueError(f"route {r.path!r}: unknown process {r.to!r}")
                if self.processes[r.to].port is None:
                    raise ValueError(f"route {r.path!r}: process {r.to!r} has no port")
        for d in self.certs:
            if d not in self.domains:
                raise ValueError(f"certs: {d!r} is not one of this app's domains")
        if self.domains and not self.routes and not self._default_route_target():
            raise ValueError("app has domains but no routes and no process with a port")
        return self

    # ---- derived ----
    def _default_route_target(self) -> str | None:
        with_port = [pid for pid, p in self.processes.items() if p.port is not None]
        if MAIN in with_port:
            return MAIN
        return with_port[0] if len(with_port) == 1 else None

    def effective_routes(self) -> list[Route]:
        """Routes as written, or a single '/' route to the only process with a port."""
        if self.routes:
            return self.routes
        target = self._default_route_target()
        return [Route(path="/", to=target)] if (target and self.domains) else []

    def mount_routes(self) -> list[Route]:
        """Routes to serve under a router path (same as effective_routes, but no domain is needed)."""
        if self.routes:
            return self.routes
        target = self._default_route_target()
        return [Route(path="/", to=target)] if target else []

    def instance(self, pid: str) -> str:
        """systemd instance name: <app> for a single 'main' process, else <app>:<pid>."""
        if list(self.processes) == [MAIN]:
            return self.name
        return f"{self.name}:{pid}"

    def unit(self, pid: str) -> str:
        return f"ahost@{self.instance(pid)}.service"

    @property
    def multi(self) -> bool:
        return list(self.processes) != [MAIN] and len(self.processes) > 0

    def cert_groups(self) -> dict[str, list[str]]:
        """cert name -> domains it serves. Domains without an explicit cert share one cert named after the app."""
        groups: dict[str, list[str]] = {}
        for d in self.domains:
            groups.setdefault(self.certs.get(d, self.name), []).append(d)
        return groups


Port = Annotated[int, Field(ge=1, le=65535)]


# ---------------------------------------------------------------- routers
ENTRY_RE = re.compile(r"^(/[a-z0-9][a-z0-9._-]{0,62}){1,4}$")


class Entry(Strict):
    """One path on a router, e.g. /chat. With no app it is reserved: nginx answers 404 until it's assigned."""

    path: str
    app: str | None = None
    strip: bool = True  # the app sees / instead of /chat (redirects and cookies are rewritten to match)
    note: str = ""

    @field_validator("path")
    @classmethod
    def _path(cls, v: str) -> str:
        v = v.rstrip("/")
        if not ENTRY_RE.match(v) or ".." in v:
            raise ValueError(f"path {v!r}: use /name (lowercase letters, digits, . _ -), up to 4 levels")
        return v

    @field_validator("app")
    @classmethod
    def _app(cls, v: str | None) -> str | None:
        if v is not None and not NAME_RE.match(v):
            raise ValueError(f"invalid app name {v!r}")
        return v


class Router(Strict):
    """A shared domain whose paths lead to different apps: <domain>/<path> -> app. One per <apps_dir>/routers/*.toml."""

    name: str
    domain: str
    description: str = ""
    cert: str | None = None  # existing certbot cert name; default ahost-router.<name>
    index: str | None = None  # "/" redirects to this entry path; None = 404
    max_body: str | None = None
    entries: list[Entry] = []

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not NAME_RE.match(v):
            raise ValueError(f"name must match {NAME_RE.pattern}")
        return v

    @field_validator("domain")
    @classmethod
    def _domain(cls, v: str) -> str:
        if not DOMAIN_RE.match(v):
            raise ValueError(f"invalid domain {v!r}")
        return v

    @model_validator(mode="after")
    def _consistency(self) -> Router:
        paths = [e.path for e in self.entries]
        dup = sorted({p for p in paths if paths.count(p) > 1})
        if dup:
            raise ValueError(f"paths used twice: {dup}")
        if self.index is not None and self.index not in paths:
            raise ValueError(f"index {self.index!r} is not one of this router's paths")
        if self.max_body is not None and not SIZE_RE.match(self.max_body):
            raise ValueError(f"max_body: bad size {self.max_body!r}")
        if self.cert is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", self.cert):
            raise ValueError("invalid cert name")
        return self

    @property
    def cert_name(self) -> str:
        return self.cert or f"ahost-router.{self.name}"

    def entry(self, path: str) -> Entry | None:
        return next((e for e in self.entries if e.path == path.rstrip("/")), None)
