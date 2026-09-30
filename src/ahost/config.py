"""Server-wide configuration (/etc/ahost/ahost.toml)."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict

DEFAULT_CONFIG = Path(os.environ.get("AHOST_CONFIG", "/etc/ahost/ahost.toml"))


class _S(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Paths(_S):
    apps_dir: str = "/home/me/apps"
    allowed_roots: list[str] = ["/home/me"]
    deny_paths: list[str] = []
    etc_dir: str = "/etc/ahost"  # env files, run scripts, allow-lists
    state_dir: str = "/var/lib/ahost"
    systemd_dir: str = "/etc/systemd/system"


class Run(_S):
    default_user: str = "me"
    home: str | None = None  # home of default_user; defaults to /home/<user>
    uv_bin: str | None = None  # defaults to <home>/.local/bin/uv
    nvm_dir: str | None = None  # defaults to <home>/.nvm
    conda_root: str | None = None  # defaults to <home>/anaconda3


class Ports(_S):
    range: tuple[int, int] = (10000, 10999)
    reserved: list[int] = []


class Nginx(_S):
    sites_dir: str = "/etc/nginx/ahost.d"
    legacy_available: str = "/etc/nginx/sites-available"
    legacy_enabled: str = "/etc/nginx/sites-enabled"
    log_dir: str = "/var/log/nginx/ahost"
    catch_all: bool = True
    listen_ipv6: bool = False  # add [::]:80/443 listeners


class Certs(_S):
    email: str = ""
    webroot: str = "/var/www/ahost-acme"
    live_dir: str = "/etc/letsencrypt/live"
    options_file: str = "/etc/letsencrypt/options-ssl-nginx.conf"
    dhparam: str = "/etc/letsencrypt/ssl-dhparams.pem"


class Dns(_S):
    provider: str = "none"
    static_hosts: list[str] = []  # names ddclient keeps updating that aren't AHost apps (vpn, games, ...)
    ddclient_conf: str = "/etc/ddclient/ddclient.conf"


class Gpu(_S):
    enabled: bool = True


class Ui(_S):
    domain: str | None = None  # public name for the dashboard, e.g. host.example.com
    bind: str = "127.0.0.1:9900"
    session_hours: int = 12
    totp: str = "optional"


class Observe(_S):
    units: list[str] = []


class Config(_S):
    paths: Paths = Paths()
    run: Run = Run()
    ports: Ports = Ports()
    nginx: Nginx = Nginx()
    certs: Certs = Certs()
    dns: Dns = Dns()
    gpu: Gpu = Gpu()
    ui: Ui = Ui()
    observe: Observe = Observe()

    @property
    def home(self) -> str:
        return self.run.home or f"/home/{self.run.default_user}"

    @property
    def uv_bin(self) -> str:
        return self.run.uv_bin or f"{self.home}/.local/bin/uv"

    @property
    def nvm_dir(self) -> str:
        return self.run.nvm_dir or f"{self.home}/.nvm"

    @property
    def conda_root(self) -> str:
        return self.run.conda_root or f"{self.home}/anaconda3"


def load(path: Path | str | None = None) -> Config:
    p = Path(path) if path else DEFAULT_CONFIG
    if not p.exists():
        raise FileNotFoundError(f"AHost config not found: {p} (set AHOST_CONFIG or pass --config)")
    with p.open("rb") as f:
        return Config.model_validate(tomllib.load(f))
