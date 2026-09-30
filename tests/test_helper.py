"""Helper verbs against a fake filesystem (no root; systemctl/nginx/chown mocked)."""

import os
from pathlib import Path

import pytest

from ahost import helper
from ahost.config import Config

MANIFEST = """name = "blog"
workdir = "{wd}"
domains = ["blog.example.com"]
command = "gunicorn app --bind 127.0.0.1:$PORT"
runtime = {{ venv = "venv" }}
port = 5100
[legacy]
unit = "Blog.service"
site = "blog"
"""

LEGACY_SITE = "server { listen 443 ssl; server_name blog.example.com; location / { proxy_pass http://127.0.0.1:5100; } }\n"


@pytest.fixture
def env(tmp_path, monkeypatch):
    wd = tmp_path / "home/proj"
    (wd / "venv/bin").mkdir(parents=True)
    apps = tmp_path / "home/apps"
    apps.mkdir()
    (apps / "blog.toml").write_text(MANIFEST.format(wd=wd))
    etc = tmp_path / "etc"
    avail, enabled = tmp_path / "nginx/sites-available", tmp_path / "nginx/sites-enabled"
    avail.mkdir(parents=True); enabled.mkdir()
    (avail / "blog").write_text(LEGACY_SITE)
    os.symlink("../sites-available/blog", enabled / "blog")
    (etc).mkdir()
    (etc / "legacy.allow").write_text("unit:Blog.service\nsite:blog\n")
    renewal = tmp_path / "le/renewal"; renewal.mkdir(parents=True)
    (renewal / "blog.conf").write_text("")
    cfg = Config.model_validate({
        "paths": {"apps_dir": str(apps), "allowed_roots": [str(tmp_path / "home")], "etc_dir": str(etc),
                  "state_dir": str(tmp_path / "state"), "systemd_dir": str(tmp_path / "systemd")},
        "run": {"default_user": "me", "home": str(tmp_path / "home")},
        "nginx": {"sites_dir": str(tmp_path / "nginx/ahost.d"), "legacy_available": str(avail),
                  "legacy_enabled": str(enabled), "log_dir": str(tmp_path / "log")},
        "certs": {"live_dir": str(tmp_path / "le/live"), "webroot": str(tmp_path / "acme")},
    })
    calls = []
    state = {"nginx_ok": True}

    def fake_sh(*args, check=True):
        calls.append(args)
        class R:  # noqa: N801
            returncode = 0 if (args[:2] != ("nginx", "-t") or state["nginx_ok"]) else 1
            stdout = ""
            stderr = "" if returncode == 0 else "nginx: [emerg] test failure"
        if check and R.returncode:
            raise helper.HelperError("failed")
        return R()

    monkeypatch.setattr(helper, "sh", fake_sh)
    monkeypatch.setattr(helper.os, "chown", lambda *a: None)
    monkeypatch.setattr(helper.syslog, "syslog", lambda *a: None)
    monkeypatch.setattr("ahost.ports.listening_ports", lambda: {})
    return cfg, calls, state, enabled


def test_apply_unit_writes_files(env):
    cfg, calls, _, _ = env
    out = helper.v_apply_unit(cfg, "blog")
    assert out["ports"] == {"main": 5100}
    etc = Path(cfg.paths.etc_dir) / "apps"
    assert (etc / "blog.env").stat().st_mode & 0o777 == 0o600
    assert 'PORT="5100"' in (etc / "blog.env").read_text()
    assert (etc / "blog.sh").stat().st_mode & 0o777 == 0o755
    assert ("systemctl", "daemon-reload") in calls
    calls.clear()
    assert helper.v_apply_unit(cfg, "blog")["changed"] == []  # idempotent
    assert calls == []


def test_apply_site_refuses_while_legacy_enabled(env):
    cfg, _, _, _ = env
    helper.v_apply_unit(cfg, "blog")
    with pytest.raises(helper.HelperError, match="swap-site"):
        helper.v_apply_site(cfg, "blog")


def test_swap_and_unswap(env):
    cfg, calls, _, enabled = env
    helper.v_apply_unit(cfg, "blog")
    helper.v_apply_site(cfg, "blog", swap_legacy=True)
    assert not (enabled / "blog").exists()
    site = Path(cfg.nginx.sites_dir) / "blog.conf"
    assert "listen 443 ssl;" in site.read_text()
    assert sum(1 for c in calls if c == ("systemctl", "reload", "nginx")) == 1  # one reload
    helper.v_unswap_site(cfg, "blog")
    assert (enabled / "blog").is_symlink() and not site.exists()


def test_swap_restores_on_nginx_failure(env):
    cfg, _, state, enabled = env
    helper.v_apply_unit(cfg, "blog")
    state["nginx_ok"] = False
    with pytest.raises(helper.HelperError, match="restored"):
        helper.v_apply_site(cfg, "blog", swap_legacy=True)
    assert (enabled / "blog").is_symlink()
    assert not (Path(cfg.nginx.sites_dir) / "blog.conf").exists()


def test_legacy_allow_list_enforced(env):
    cfg, _, _, _ = env
    (Path(cfg.paths.etc_dir) / "legacy.allow").write_text("")
    with pytest.raises(helper.HelperError, match="legacy.allow"):
        helper.v_legacy_unit(cfg, "stop", "blog")


def test_rejects_bad_names_and_symlinked_manifests(env, tmp_path):
    cfg, _, _, _ = env
    with pytest.raises(helper.HelperError):
        helper._app(cfg, "../etc/passwd")
    link = Path(cfg.paths.apps_dir) / "evil.toml"
    os.symlink("/etc/hostname", link)
    with pytest.raises(helper.HelperError, match="regular file"):
        helper._app(cfg, "evil")


def test_allowed_path(env):
    cfg, _, _, _ = env
    helper._allowed_path(cfg, f"{cfg.paths.etc_dir}/apps/blog.env")
    for bad in ("/etc/shadow", f"{cfg.paths.etc_dir}/apps/../../x", f"{cfg.paths.systemd_dir}/sshd.service"):
        with pytest.raises(helper.HelperError):
            helper._allowed_path(cfg, bad)
