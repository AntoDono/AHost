import json

from typer.testing import CliRunner

from ahost.cli import app

run = CliRunner().invoke


def test_router_cli(tmp_path, monkeypatch):
    (tmp_path / "apps").mkdir()
    (tmp_path / "proj").mkdir()
    (tmp_path / "ahost.toml").write_text(
        f'[paths]\napps_dir = "{tmp_path}/apps"\nallowed_roots = ["{tmp_path}"]\nstate_dir = "{tmp_path}/state"\n'
        f'etc_dir = "{tmp_path}/etc"\n[run]\ndefault_user = "me"\n[ui]\ndomain = "host.example.com"\n')
    (tmp_path / "apps/notes.toml").write_text(f'name = "notes"\nworkdir = "{tmp_path}/proj"\ncommand = "run"\nport = "auto"\n')
    monkeypatch.setenv("AHOST_CONFIG", str(tmp_path / "ahost.toml"))
    applied = []
    monkeypatch.setattr("ahost.ops.apply_router", lambda name, **k: applied.append(name))
    assert run(app, ["router", "create", "apps", "apps.example.com"]).exit_code == 0
    assert run(app, ["router", "create", "x", "host.example.com"]).exit_code == 1  # the dashboard's domain
    assert run(app, ["router", "add", "apps", "/notes", "--app", "notes"]).exit_code == 0
    assert run(app, ["router", "add", "apps", "/later"]).exit_code == 0
    assert run(app, ["router", "add", "apps", "/later"]).exit_code == 1
    assert run(app, ["router", "add", "apps", "/x", "--app", "ghost"]).exit_code == 1
    assert run(app, ["router", "assign", "apps", "/later", "notes", "--no-strip"]).exit_code == 0
    assert run(app, ["router", "drop", "apps", "/notes"]).exit_code == 0
    assert applied == ["apps"] * 5
    r = json.loads(run(app, ["router", "list", "--json"]).stdout)
    assert r[0]["entries"] == [{"path": "/later", "app": "notes", "strip": False, "note": ""}]
    st = json.loads(run(app, ["status", "--json"]).stdout)
    assert st[0]["port"] is None and st[0]["router_paths"] == ["apps.example.com/later/"]
    pl = json.loads(run(app, ["plan", "notes", "--json"]).stdout)
    assert pl[0]["app"] == "notes" and pl[0]["ok"]
    assert "AHost for AI agents" in run(app, ["guide"]).stdout
