import pytest
from pydantic import ValidationError

from ahost import policy, render
from ahost.config import Config
from ahost.importer.legacy import import_app
from ahost.importer.parse import parse_environment, parse_nginx, parse_unit, seconds
from ahost.models import App
from ahost.ports import PortError, Registry


@pytest.fixture
def cfg(tmp_path):
    return Config.model_validate({
        "paths": {"apps_dir": str(tmp_path / "apps"), "allowed_roots": [str(tmp_path)],
                  "deny_paths": [str(tmp_path / ".ssh")], "etc_dir": str(tmp_path / "etc")},
        "run": {"default_user": "me", "home": str(tmp_path)},
        "ports": {"range": [10000, 10010], "reserved": [10000]},
    })


@pytest.fixture
def workdir(tmp_path):
    d = tmp_path / "proj"
    (d / "venv/bin").mkdir(parents=True)
    (d / "venv/pyvenv.cfg").write_text("home = /usr/bin\n")
    return d


def mk(wd, **kw):
    base = {"name": "blog", "workdir": str(wd), "command": "uvicorn main:app --port $PORT",
            "runtime": {"venv": "venv"}, "port": "auto", "domains": ["blog.example.com"]}
    base.update(kw)
    return App.model_validate(base)


# ---------------------------------------------------------------- model
def test_shorthand_becomes_main_process(workdir):
    a = mk(workdir, env={"A": "1"}, after=["postgresql.service"])
    assert list(a.processes) == ["main"]
    assert a.env == {"A": "1"} and a.after == ["postgresql.service"]
    assert a.unit("main") == "ahost@blog.service"
    assert [r.path for r in a.effective_routes()] == ["/"]


def test_shorthand_does_not_mutate_input(workdir):
    data = {"name": "x", "workdir": str(workdir), "command": "run", "port": 1}
    App.model_validate(data)
    assert "command" in data


def test_multi_process_instance_names(workdir):
    a = App.model_validate({"name": "asst", "workdir": str(workdir), "domains": ["a.example.com"],
                            "processes": {"web": {"command": "w", "port": "auto"}, "worker": {"command": "q"}},
                            "routes": [{"path": "/", "to": "web"}]})
    assert a.unit("web") == "ahost@asst:web.service" and a.multi


@pytest.mark.parametrize("bad", [
    {"name": "Bad_Name"},
    {"domains": ["not a domain"]},
    {"routes": [{"path": "/", "to": "nope"}]},
    {"routes": [{"path": "/", "to": "main", "static": "x"}]},
    {"routes": [{"path": "/x { }", "status": 404}]},
    {"processes": {"main": {"command": "a\nb"}}, "command": None},
])
def test_invalid_manifests(workdir, bad):
    data = {"name": "blog", "workdir": str(workdir), "command": "run", "port": "auto",
            "domains": ["blog.example.com"], **bad}
    if data.get("command") is None:
        data.pop("command"); data.pop("port")
    with pytest.raises(ValidationError):
        App.model_validate(data)


def test_systemd_extra_allow_list(workdir):
    with pytest.raises(ValidationError):
        mk(workdir, systemd={"ExecStartPre": "/bin/sh -c evil"})


# ---------------------------------------------------------------- policy
def test_policy_root_needs_allow_list(workdir, cfg):
    a = mk(workdir, user="root")
    assert not policy.check(a, cfg, root_allow=set()).ok
    assert policy.check(a, cfg, root_allow={"blog"}).ok


def test_policy_paths_and_env(workdir, cfg, tmp_path):
    assert not policy.check(mk(workdir, workdir="/etc"), cfg, check_fs=False).ok
    assert not policy.check(mk(workdir, env={"LD_PRELOAD": "x"}), cfg).ok
    assert not policy.check(mk(workdir, env={"PORT": "1"}), cfg).ok
    (tmp_path / ".ssh").mkdir()
    assert not policy.check(mk(workdir, sandbox={"rw_paths": [str(tmp_path / ".ssh")]}), cfg).ok


@pytest.mark.parametrize("raw", ["access_log /etc/cron.d/x;", "include /etc/shadow;", "alias /;",
                                 "  root /etc;", "load_module x.so;"])
def test_policy_raw_nginx_denied(workdir, cfg, raw):
    assert not policy.check(mk(workdir, proxy={"raw_server": raw}), cfg).ok


def test_policy_raw_http_names_prefixed(workdir, cfg):
    assert not policy.check(mk(workdir, proxy={"raw_http": "upstream django { server 127.0.0.1:1; }"}), cfg).ok
    assert policy.check(mk(workdir, proxy={"raw_http": "upstream blog_django { server 127.0.0.1:1; }"}), cfg).ok


def test_unique_domains(workdir):
    a, b = mk(workdir), mk(workdir, name="other")
    assert policy.check_unique_domains([a, b], {})
    assert policy.check_unique_domains([a], {"blog.example.com": "legacy-site"})
    adopted = mk(workdir, legacy={"unit": "Blog.service", "site": "legacy-site"})
    assert not policy.check_unique_domains([adopted], {"blog.example.com": "legacy-site"})


# ---------------------------------------------------------------- ports
def test_ports(workdir, cfg):
    reg = Registry(":memory:", cfg)
    a = mk(workdir)
    assert reg.resolve(a, listening={10001: "x"}, commit=True) == {"main": 10002}  # 10000 reserved, 10001 busy
    assert reg.resolve(a, listening={}, commit=True) == {"main": 10002}  # sticky
    with pytest.raises(PortError):
        reg.resolve(mk(workdir, name="other", port=10002), listening={})
    reg.release("blog")
    assert reg.all() == []


# ---------------------------------------------------------------- render
def test_render_process(workdir, cfg):
    a = mk(workdir, env={"Q": 'a "b" 50%'}, command="gunicorn app && echo done")
    arts = {x.kind: x for x in render.render_process(a, "main", 10003, cfg, render.Facts())}
    assert 'PORT="10003"' in arts["env"].content and arts["env"].mode == 0o600
    assert 'Environment="Q=a \\"b\\" 50%%"' in arts["unit"].content
    assert "exec /bin/bash -c" in arts["run"].content  # shell operators -> bash -c
    assert f"export VIRTUAL_ENV={workdir}/venv" in arts["run"].content


def test_render_site_http_only_until_cert(workdir, cfg):
    a = mk(workdir)
    site = render.render_site(a, {"main": 10003}, cfg, render.Facts())
    assert "listen 443" not in site.content and "proxy_pass http://127.0.0.1:10003;" in site.content
    site = render.render_site(a, {"main": 10003}, cfg, render.Facts(certs_present={"blog"}))
    assert "listen 443 ssl;" in site.content and "return 301 https://" in site.content
    assert "proxy_buffering off;" in site.content  # streaming default for new apps


def test_render_sandbox_strict_gpu(workdir, cfg):
    a = mk(workdir, gpus=["GPU-a"], sandbox={"level": "strict", "caches": ["huggingface"]})
    unit = render.render_process(a, "main", 1, cfg, render.Facts(gpu_minors={"GPU-a": 3}))[0].content
    assert "DeviceAllow=/dev/nvidia3 rw" in unit and "DevicePolicy=closed" in unit
    assert "/.cache/huggingface" in unit and "TemporaryFileSystem=/home" in unit
    with pytest.raises(ValueError):
        render.render_process(a, "main", 1, cfg, render.Facts(gpu_minors={}))


# ---------------------------------------------------------------- importer
UNIT = """[Unit]
Description=Shop API
After=network.target postgresql.service
Wants=postgresql.service

[Service]
User=me
Group=www-data
WorkingDirectory={wd}
ExecStart={wd}/venv/bin/gunicorn \\
\t--chdir {wd} \\
\tshop.wsgi:application --bind 0.0.0.0:5100 --workers 3
Restart=always
Environment="DJANGO_MODE=production"
Environment=PATH={wd}/venv/bin
PrivateTmp=true
TimeoutStopSec=20

[Install]
WantedBy=multi-user.target
"""

SITE = """server {
    server_name api.shop.example shop-api.example.com;
    client_max_body_size 100M;
    location /static/ {
        alias {wd}/static/;
    }
    location / {
        proxy_pass http://127.0.0.1:5100;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host $host;
        proxy_cache_bypass $http_upgrade;
        proxy_read_timeout 300s;
    }
    listen 443 ssl; # managed by Certbot
    ssl_certificate /etc/letsencrypt/live/shop-api.example.com/fullchain.pem; # managed by Certbot
    include /etc/letsencrypt/options-ssl-nginx.conf; # managed by Certbot
}
server {
    if ($host = api.shop.example) {
        return 301 https://$host$request_uri;
    } # managed by Certbot
    server_name api.shop.example;
    listen 80;
    return 404; # managed by Certbot
}
"""


def test_import_roundtrip(workdir, cfg):
    wd = str(workdir)
    m, rep = import_app("shop", UNIT.replace("{wd}", wd), "Shop.service", SITE.replace("{wd}", wd), "shop", cfg)
    a = App.model_validate(m)
    p = a.processes["main"]
    assert p.command == f"gunicorn --chdir {wd} shop.wsgi:application --bind 0.0.0.0:$PORT --workers 3"
    assert p.port == 5100 and p.runtime.venv == "venv" and p.restart == "always"
    assert p.stop_timeout == 20 and p.systemd == {"PrivateTmp": "true"}
    assert a.group == "www-data" and a.env["DJANGO_MODE"] == "production"
    assert a.after == ["postgresql.service"] and a.wants == ["postgresql.service"]
    assert a.domains == ["api.shop.example", "shop-api.example.com"]
    assert a.certs == {"api.shop.example": "shop-api.example.com", "shop-api.example.com": "shop-api.example.com"}
    assert a.proxy.max_body == "100M"
    static, root = a.routes
    assert static.static == "static" and root.to == "main" and root.timeout == "300s"
    assert root.websocket and not root.streaming  # legacy buffered: keep buffering
    assert any("check it actually covers every name" in t for t in rep.todo)
    assert any("redirect added" in n for n in rep.notes)


def test_parsers():
    u = parse_unit("[Service]\nExecStart=/a \\\n  b\nEnvironment=\"A=1 2\" B=3\n")
    assert u.get("Service", "ExecStart") == "/a b"
    assert parse_environment(u.all("Service", "Environment")) == {"A": "1 2", "B": "3"}
    assert seconds("5s") == 5 and seconds("2min") == 120 and seconds("1h 30s") == 3630
    tree = parse_nginx("server { location / { return 200 'a;b'; } } # c")
    assert tree[0].first("location").first("return").args == ["200", "'a;b'"]
