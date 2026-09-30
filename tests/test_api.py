import pytest
from fastapi.testclient import TestClient

from ahost.api import auth
from ahost.api import app as api_app
from ahost.config import Config

H = {"X-AHost": "1"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    (tmp_path / "apps").mkdir()
    (tmp_path / "home" / "proj").mkdir(parents=True)
    (tmp_path / "home" / ".ssh").mkdir()
    dist = tmp_path / "dist"; dist.mkdir()
    (dist / "index.html").write_text('<html><script>window.__NUXT__={}</script><script src="/_nuxt/a.js"></script></html>')
    monkeypatch.setattr(api_app, "UI_DIST", dist)
    cfg = Config.model_validate({"paths": {"apps_dir": str(tmp_path / "apps"), "state_dir": str(tmp_path / "state"),
                                           "allowed_roots": [str(tmp_path / "home")],
                                           "deny_paths": [str(tmp_path / "home/.ssh")]},
                                 "run": {"default_user": "me"}})
    store = auth.Store(cfg)
    store.add_user("anto", "correct horse battery")
    return TestClient(api_app.create_app(cfg, store), base_url="https://testserver"), tmp_path


def login(c, pw="correct horse battery"):
    return c.post("/api/auth/login", json={"username": "anto", "password": pw}, headers=H)


def test_requires_login_and_csrf(client):
    c, _ = client
    assert c.get("/api/overview").status_code == 401
    assert c.post("/api/auth/login", json={"username": "anto", "password": "x"}).status_code == 403  # no header
    assert login(c, "wrong password!!").status_code == 401
    r = login(c)
    assert r.status_code == 200
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=strict" in cookie
    assert c.get("/api/auth/me").json() == {"user": "anto"}
    assert c.post("/api/apps/x/restart").status_code == 403  # signed in but no CSRF header
    assert c.post("/api/auth/logout", headers=H).status_code == 200
    assert c.get("/api/ports").status_code == 401


def test_rate_limit(client):
    c, _ = client
    for _ in range(5):
        assert login(c, "nope nope nope").status_code == 401
    assert login(c).status_code == 429  # even the right password is blocked for a while


def test_security_headers_and_spa(client):
    c, _ = client
    r = c.get("/some/deep/link")
    assert r.status_code == 200 and "__NUXT__" in r.text
    csp = r.headers["content-security-policy"]
    assert "'sha256-" in csp and "unsafe-inline" not in csp.split("script-src")[1].split(";")[0]
    assert r.headers["x-frame-options"] == "DENY"


def test_fs_confined(client):
    c, tmp = client
    login(c)
    assert c.get("/api/fs", params={"path": str(tmp / "home")}).json()["dirs"] == ["proj"]
    assert c.get("/api/fs", params={"path": "/etc"}).status_code == 403
    assert c.get("/api/fs", params={"path": str(tmp / "home/.ssh")}).status_code == 403
    assert c.get("/api/fs", params={"path": str(tmp / "home/proj/../../..")}).status_code == 403


def test_create_validates(client):
    c, tmp = client
    login(c)
    bad = {"name": "x", "workdir": "/etc", "command": "run"}
    assert c.post("/api/apps", json=bad, headers=H).status_code == 422
    good = {"name": "blog", "workdir": str(tmp / "home/proj"), "command": "python app.py", "port": "auto"}
    assert c.post("/api/apps", json=good, headers=H).status_code == 200
    assert (tmp / "apps/blog.toml").exists()
    assert c.post("/api/apps", json=good, headers=H).status_code == 409
    assert "blog" in c.get("/api/apps/blog/manifest").json()["toml"]
