import pytest
from pydantic import ValidationError

from ahost import policy, render
from ahost.config import Config
from ahost.manifest_io import dump_router, load_router, routers_dir
from ahost.models import App, Router


@pytest.fixture
def cfg(tmp_path):
    return Config.model_validate({
        "paths": {"apps_dir": str(tmp_path / "apps"), "allowed_roots": [str(tmp_path)], "etc_dir": str(tmp_path)},
        "run": {"default_user": "me", "home": str(tmp_path)},
        "ui": {"domain": "host.example.com"},
    })


@pytest.fixture
def apps(tmp_path):
    (tmp_path / "chat/dist").mkdir(parents=True)
    return {
        "chat": App.model_validate({
            "name": "chat", "workdir": str(tmp_path / "chat"), "command": "run", "port": "auto",
            "routes": [{"path": "/", "to": "main"}, {"path": "/assets/", "static": "dist", "cache": "7d"},
                       {"path": "~ ^/x", "status": 404}]}),
        "wiki": App.model_validate({"name": "wiki", "workdir": str(tmp_path), "command": "run", "port": 5000,
                                    "domains": ["wiki.example.com"]}),
        "docs": App.model_validate({"name": "docs", "workdir": str(tmp_path / "chat"),
                                    "routes": [{"path": "/", "static": "dist", "spa_fallback": "/index.html"}]}),
    }


def mk(**kw):
    base = {"name": "apps", "domain": "apps.example.com", "entries": [
        {"path": "/chat", "app": "chat"}, {"path": "/wiki/", "app": "wiki", "strip": False},
        {"path": "/docs", "app": "docs"}, {"path": "/later"}]}
    base.update(kw)
    return Router.model_validate(base)


def test_router_model():
    r = mk(index="/chat")
    assert r.entry("/wiki").path == "/wiki"  # trailing slash normalised
    assert r.entry("/later").app is None
    assert r.cert_name == "ahost-router.apps"
    for bad in [{"entries": [{"path": "/a"}, {"path": "/a/"}]}, {"index": "/nope"},
                {"entries": [{"path": "/"}]}, {"entries": [{"path": "/A"}]}, {"entries": [{"path": "/a/../b"}]},
                {"domain": "not a domain"}]:
        with pytest.raises(ValidationError):
            mk(**bad)


def test_router_render(cfg, apps):
    ports = {"chat": {"main": 10003}, "wiki": {"main": 5000}}
    site, warnings = render.render_router(mk(index="/chat"), apps, ports, cfg, render.Facts())
    t = site.content
    assert site.path.endswith("/router.apps.conf")
    assert "server_name apps.example.com;" in t and "listen 443" not in t  # HTTP until the cert exists
    assert "location = / {\n        return 302 /chat/;" in t
    assert "location / {\n        return 404;" in t
    # strip mode: prefix removed, redirects and cookies put back under it
    assert "location = /chat {\n        return 301 /chat/$is_args$args;" in t
    assert "location /chat/ {\n        proxy_pass http://127.0.0.1:10003/;" in t
    assert "proxy_set_header X-Forwarded-Prefix /chat;" in t
    assert r'proxy_redirect "~^(https?://apps\.example\.com)?/(?!chat(?:/|$))(.*)$" "$1/chat/$2";' in t
    assert "proxy_cookie_path / /chat/;" in t
    # static routes become aliases under the prefix
    assert f"location /chat/assets/ {{\n        alias {apps['chat'].workdir}/dist/;" in t
    assert f"location /docs/ {{\n        alias {apps['docs'].workdir}/dist/;\n        try_files $uri $uri.html $uri/index.html /docs/index.html;" in t
    # pass-through mode: URI unchanged, no rewriting
    assert "location /wiki/ {\n        proxy_pass http://127.0.0.1:5000;" in t
    assert t.count("X-Forwarded-Prefix") == 1
    assert "/later" not in t  # reserved paths fall through to 404
    assert any("regex" in w for w in warnings)


def test_router_render_unapplied_and_missing(cfg, apps):
    r = mk(entries=[{"path": "/chat", "app": "chat"}, {"path": "/gone", "app": "gone"}])
    site, warnings = render.render_router(r, apps, {}, cfg, render.Facts())
    assert "location /chat/ {\n        return 503;" in site.content
    assert "/gone" not in site.content
    assert len(warnings) == 2


def test_router_render_location_clash(cfg, apps):
    r = mk(entries=[{"path": "/chat", "app": "chat"}, {"path": "/chat/assets", "app": "docs"}])
    with pytest.raises(ValueError, match="both need location"):
        render.render_router(r, apps, {"chat": {"main": 10003}}, cfg, render.Facts())


def test_router_https_when_cert_present(cfg, apps):
    site, _ = render.render_router(mk(), apps, {"chat": {"main": 1}, "wiki": {"main": 2}}, cfg,
                                   render.Facts(certs_present={"ahost-router.apps"}))
    assert "live/ahost-router.apps/fullchain.pem" in site.content
    assert "return 301 https://$host$request_uri;" in site.content


def test_router_domain_policy(apps):
    r = mk()
    assert policy.check_router(r, list(apps.values()), {}, {}, "host.example.com") == []
    assert policy.check_router(mk(domain="wiki.example.com"), list(apps.values()), {}, {}, None)
    assert policy.check_router(r, [], {}, {"apps.example.com": "old-site"}, None)
    assert policy.check_router(mk(domain="host.example.com"), [], {}, {}, "host.example.com")
    other = mk(name="other")
    assert policy.check_router(r, [], {"other": other}, {}, None)
    assert policy.reserved_domains({"apps": r}, "host.example.com") == {
        "apps.example.com": "router apps", "host.example.com": "the AHost dashboard"}


def test_router_file_roundtrip(tmp_path):
    r = mk(index="/chat", description="Shared")
    d = routers_dir(tmp_path)
    d.mkdir()
    (d / "apps.toml").write_text(dump_router(r))
    text = (d / "apps.toml").read_text()
    assert "[[entries]]\npath = \"/chat\"\napp = \"chat\"" in text
    assert load_router(d / "apps.toml") == r


def test_mount_routes_without_domain(apps):
    worker = App.model_validate({"name": "w", "workdir": "/tmp", "command": "run", "port": "auto"})
    assert worker.effective_routes() == []
    assert [r.path for r in worker.mount_routes()] == ["/"]


def test_cert_must_cover_domains(tmp_path):
    from ahost import host
    cfg = Config.model_validate({"certs": {"live_dir": str(tmp_path / "le/live")}})
    ren = tmp_path / "le/renewal"
    ren.mkdir(parents=True)
    (ren / "ui.conf").write_text("[renewalparams]\nauthenticator = webroot\n[[webroot_map]]\nold.example.com = /w\n")
    (ren / "legacy.conf").write_text("[renewalparams]\nauthenticator = nginx\n")
    assert host.cert_domains(cfg, "ui") == {"old.example.com"}
    assert host.cert_domains(cfg, "legacy") is None
    assert host.usable_certs(cfg, {"ui": ["old.example.com"]}) == {"ui"}
    assert host.usable_certs(cfg, {"ui": ["new.example.com"]}) == set()  # re-issued for the new name
    assert host.usable_certs(cfg, {"legacy": ["x.example.com"]}) == {"legacy"}  # unknown: trusted as before
    assert host.usable_certs(cfg, {"nope": ["x.example.com"]}) == set()
