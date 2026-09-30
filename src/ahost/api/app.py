"""AHost web API + static dashboard. Runs as the `ahost` user on 127.0.0.1 behind nginx (ahost.service)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import socket
import subprocess
import tomllib
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, ValidationError
from starlette.concurrency import run_in_threadpool

from .. import ops, policy
from ..config import Config
from ..manifest_io import dumps, load_all, load_app
from ..models import NAME_RE, App
from ..plan import format_plan, make_plan
from . import auth, status

COOKIE = "ahost_session"
CSRF_HEADER = "x-ahost"
UI_DIST = Path(os.environ.get("AHOST_UI_DIST", "/opt/ahost/ui"))


class Login(BaseModel):
    username: str
    password: str


class ManifestText(BaseModel):
    toml: str


def _inline_script_hashes(index_html: str) -> list[str]:
    out = []
    for m in re.finditer(r"<script(?![^>]*\bsrc=)(?![^>]*type=\"application/json\")[^>]*>(.*?)</script>", index_html, re.DOTALL):
        digest = base64.b64encode(hashlib.sha256(m.group(1).encode()).digest()).decode()
        out.append(f"'sha256-{digest}'")
    return out


def create_app(cfg: Config, store: auth.Store | None = None) -> FastAPI:
    app = FastAPI(title="AHost", docs_url=None, redoc_url=None, openapi_url="/api/openapi.json")
    store = store or auth.Store(cfg)
    limiter = auth.RateLimiter()
    apps_dir = Path(cfg.paths.apps_dir)
    index = UI_DIST / "index.html"
    _csp_cache: dict[str, object] = {"mtime": None, "value": ""}

    def csp() -> str:
        """CSP allowing exactly the dashboard's inline scripts. Recomputed whenever index.html changes (a UI
        reinstall must never leave a running server with stale hashes)."""
        try:
            mtime = index.stat().st_mtime_ns
        except OSError:
            mtime = None
        if mtime != _csp_cache["mtime"]:
            hashes = _inline_script_hashes(index.read_text()) if mtime else []
            _csp_cache["value"] = (
                "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                f"script-src 'self' {' '.join(hashes)}; connect-src 'self'; font-src 'self' data:; "
                "frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'")
            _csp_cache["mtime"] = mtime
        return str(_csp_cache["value"])

    def client_ip(req: Request) -> str:
        return req.headers.get("x-real-ip") or (req.client.host if req.client else "?")

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        if path.startswith("/api/"):
            if request.method not in ("GET", "HEAD") and request.headers.get(CSRF_HEADER) != "1":
                return JSONResponse({"detail": "missing X-AHost header"}, status_code=403)
            if path not in ("/api/auth/login", "/api/auth/me"):
                user = store.session_user(request.cookies.get(COOKIE))
                if not user:
                    return JSONResponse({"detail": "not signed in"}, status_code=401)
                request.state.user = user
        resp = await call_next(request)
        resp.headers["Content-Security-Policy"] = csp()
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Strict-Transport-Security"] = "max-age=31536000"
        if path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    # ------------------------------------------------------------ auth
    @app.post("/api/auth/login")
    async def login(body: Login, request: Request, response: Response):
        ip = client_ip(request)
        keys = [f"ip:{ip}", f"user:{body.username}"]
        if any(limiter.blocked(k) for k in keys):
            raise HTTPException(429, "Too many failed sign-ins. Try again in 15 minutes.")
        ok = await run_in_threadpool(store.verify, body.username, body.password)
        if not ok:
            for k in keys:
                limiter.fail(k)
            raise HTTPException(401, "Wrong username or password.")
        for k in keys:
            limiter.reset(k)
        token = store.create_session(body.username, ip)
        response.set_cookie(COOKIE, token, max_age=cfg.ui.session_hours * 3600, httponly=True, secure=True,
                            samesite="strict", path="/")
        return {"user": body.username}

    @app.post("/api/auth/logout")
    async def logout(request: Request, response: Response):
        store.end_session(request.cookies.get(COOKIE))
        response.delete_cookie(COOKIE, path="/")
        return {"ok": True}

    @app.get("/api/auth/me")
    async def me(request: Request):
        return {"user": store.session_user(request.cookies.get(COOKIE))}

    # ------------------------------------------------------------ read
    @app.get("/api/overview")
    async def overview():
        return await run_in_threadpool(status.overview, cfg)

    @app.get("/api/ports")
    async def ports():
        return await run_in_threadpool(status.ports_view, cfg)

    def _load(name: str) -> App:
        if not NAME_RE.match(name):
            raise HTTPException(404, "no such app")
        path = apps_dir / f"{name}.toml"
        if not path.exists():
            raise HTTPException(404, "no such app")
        try:
            return load_app(path)
        except (ValueError, ValidationError) as e:
            raise HTTPException(422, f"manifest is invalid: {e}") from e

    @app.get("/api/apps/{name}/manifest")
    async def manifest(name: str):
        _load(name)
        return {"toml": (apps_dir / f"{name}.toml").read_text()}

    @app.get("/api/apps/{name}/plan")
    async def plan(name: str):
        a = _load(name)
        apps, _ = load_all(apps_dir)

        def run():
            pl = make_plan(a, cfg, apps, str(apps_dir / f"{name}.toml"))
            return {"text": format_plan(pl), "errors": pl.errors, "warnings": pl.warnings, "steps": pl.steps}
        return await run_in_threadpool(run)

    # ------------------------------------------------------------ logs
    def _units(a: App, process: str | None) -> list[str]:
        if process:
            if process not in a.processes:
                raise HTTPException(404, "no such process")
            return [a.unit(process)]
        return [a.unit(p) for p in a.processes]

    def _journal_line(raw: str) -> dict | None:
        try:
            j = json.loads(raw)
        except json.JSONDecodeError:
            return None
        msg = j.get("MESSAGE", "")
        if isinstance(msg, list):  # binary messages come as byte arrays
            msg = bytes(msg).decode(errors="replace")
        return {"ts": int(j.get("__REALTIME_TIMESTAMP", 0)) // 1000, "unit": j.get("_SYSTEMD_UNIT", ""),
                "pri": int(j.get("PRIORITY", 6)), "msg": msg, "cursor": j.get("__CURSOR")}

    @app.get("/api/apps/{name}/logs")
    async def logs(name: str, source: str = "app", lines: int = 300, process: str | None = None,
                   since: str | None = None):
        a = _load(name)
        lines = max(1, min(lines, 5000))
        if source in ("access", "error"):
            out = await run_in_threadpool(ops.helper, "nginx-log", name, source, str(lines), interactive=False)
            return {"lines": [{"msg": ln} for ln in out["lines"]]}
        args = ["journalctl", "--no-pager", "-o", "json", "-n", str(lines)]
        if since == "boot":
            args += ["-b"]
        elif since == "restart":
            p = status.units_show(_units(a, process))
            ts = min((v.get("ActiveEnterTimestamp") for v in p.values() if v.get("ActiveEnterTimestamp")), default=None)
            if ts:
                args += ["--since", ts]
        elif since and re.fullmatch(r"-?\d+[smhd]|today|yesterday", since):
            args += ["--since", since if not since[0].isdigit() else f"-{since}"]
        for u in _units(a, process):
            args += ["-u", u]
        r = await run_in_threadpool(subprocess.run, args, capture_output=True, text=True, check=False)
        return {"lines": [x for x in map(_journal_line, r.stdout.splitlines()) if x]}

    @app.get("/api/apps/{name}/logs/stream")
    async def logs_stream(name: str, request: Request, process: str | None = None):
        a = _load(name)
        args = ["journalctl", "--no-pager", "-o", "json", "-n", "0", "-f"]
        for u in _units(a, process):
            args += ["-u", u]

        async def gen():
            proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                        stderr=asyncio.subprocess.DEVNULL)
            try:
                yield ": connected\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        raw = await asyncio.wait_for(proc.stdout.readline(), timeout=15)
                    except TimeoutError:
                        yield ": keep-alive\n\n"
                        continue
                    if not raw:
                        break
                    line = _journal_line(raw.decode(errors="replace"))
                    if line:
                        yield f"data: {json.dumps(line)}\n\n"
            finally:
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"X-Accel-Buffering": "no", "Cache-Control": "no-store"})

    # ------------------------------------------------------------ actions
    @app.post("/api/apps/{name}/{action}")
    async def action(name: str, action: str):
        _load(name)
        if action not in ("start", "stop", "restart", "apply"):
            raise HTTPException(404, "unknown action")
        log: list[str] = []
        try:
            if action == "apply":
                await run_in_threadpool(ops.apply, name, cfg, log.append, False)
            else:
                await run_in_threadpool(ops.lifecycle, name, action, log.append, False)
        except ops.OpError as e:
            return JSONResponse({"ok": False, "error": str(e), "log": log}, status_code=500)
        return {"ok": True, "log": log}

    @app.delete("/api/apps/{name}")
    async def delete(name: str, confirm: str = ""):
        _load(name)
        if confirm != name:
            raise HTTPException(400, "type the app name to confirm")
        try:
            dst = await run_in_threadpool(ops.remove, name, cfg, lambda _m: None, False)
        except ops.OpError as e:
            raise HTTPException(500, str(e)) from e
        return {"ok": True, "manifest_kept_at": dst}

    def _validate_text(name: str, text: str) -> App:
        try:
            data = tomllib.loads(text)
            a = App.model_validate(data)
        except (tomllib.TOMLDecodeError, ValidationError, ValueError) as e:
            raise HTTPException(422, str(e)) from e
        if a.name != name:
            raise HTTPException(422, f"name must stay {name!r}")
        rep = policy.check(a, cfg)
        if not rep.ok:
            raise HTTPException(422, "; ".join(rep.errors))
        return a

    @app.put("/api/apps/{name}/manifest")
    async def save_manifest(name: str, body: ManifestText):
        _load(name)
        _validate_text(name, body.toml)
        (apps_dir / f"{name}.toml").write_text(body.toml)
        return {"ok": True}

    @app.post("/api/apps")
    async def create(body: dict[str, Any]):
        name = str(body.get("name", ""))
        if not NAME_RE.match(name):
            raise HTTPException(422, "name: lowercase letters, digits and dashes, starting with a letter")
        if (apps_dir / f"{name}.toml").exists():
            raise HTTPException(409, f"an app named {name} already exists")
        text = dumps(body, header="Created in the AHost dashboard.")
        a = _validate_text(name, text)
        apps, _ = load_all(apps_dir)
        from .. import host
        dup = [e for e in policy.check_unique_domains([*apps.values(), a], host.legacy_server_names(cfg))
               if any(f"domain {d} " in e for d in a.domains)]
        if dup:
            raise HTTPException(409, "; ".join(dup))
        (apps_dir / f"{name}.toml").write_text(text)
        return {"ok": True, "name": name}

    @app.post("/api/preview")
    async def preview(body: dict[str, Any]):
        """Validate a would-be manifest and return what apply would do (nothing is written)."""
        name = str(body.get("name", "")) or "new-app"
        text = dumps(body)
        try:
            a = App.model_validate(tomllib.loads(text))
        except (ValidationError, ValueError) as e:
            return {"ok": False, "errors": [str(e)], "toml": text}
        apps, _ = load_all(apps_dir)

        def run():
            pl = make_plan(a, cfg, apps, str(apps_dir / f"{name}.toml"))
            return {"ok": not pl.errors, "errors": pl.errors, "warnings": pl.warnings, "steps": pl.steps,
                    "ports": pl.ports, "text": format_plan(pl), "toml": text}
        return await run_in_threadpool(run)

    # ------------------------------------------------------------ create-form helpers
    def _allowed(p: str) -> Path:
        rp = os.path.realpath(p)
        if not any(rp == r or rp.startswith(r.rstrip("/") + "/") for r in map(os.path.realpath, cfg.paths.allowed_roots)):
            raise HTTPException(403, "outside allowed folders")
        if any(rp == d or rp.startswith(d.rstrip("/") + "/") for d in map(os.path.realpath, cfg.paths.deny_paths)):
            raise HTTPException(403, "that folder is off-limits")
        return Path(rp)

    @app.get("/api/fs")
    async def fs(path: str = ""):
        base = _allowed(path or cfg.paths.allowed_roots[0])
        try:
            entries = sorted((e for e in base.iterdir() if e.is_dir() and not e.name.startswith(".")),
                             key=lambda e: e.name.lower())
        except PermissionError as e:
            raise HTTPException(403, "can't read that folder") from e
        return {"path": str(base), "parent": str(base.parent) if base.parent != base else None,
                "dirs": [e.name for e in entries][:500], "roots": cfg.paths.allowed_roots}

    @app.get("/api/detect")
    async def detect(path: str):
        base = _allowed(path)
        found: dict[str, Any] = {"venvs": [], "uv": (base / "uv.lock").exists(), "node": None, "conda": None,
                                 "hints": []}
        for cand in (".venv", "venv", "env"):
            if (base / cand / "pyvenv.cfg").exists():
                found["venvs"].append(cand)
        if (base / "package.json").exists():
            try:
                pkg = json.loads((base / "package.json").read_text())
                found["node"] = {"start": (pkg.get("scripts") or {}).get("start"), "name": pkg.get("name")}
            except (OSError, json.JSONDecodeError):
                found["node"] = {}
        if (base / "environment.yml").exists():
            found["conda"] = True
        if (base / "manage.py").exists():
            found["hints"].append({"kind": "django", "command": "gunicorn <project>.wsgi:application --bind 127.0.0.1:$PORT"})
        for f in ("main.py", "app.py", "server.py"):
            if (base / f).exists():
                found["hints"].append({"kind": "python", "command": f"python {f}"})
        return found

    @app.get("/api/dns")
    async def dns(domain: str):
        if not re.fullmatch(r"[a-z0-9.-]{1,253}", domain):
            raise HTTPException(422, "bad domain")

        def resolve(d: str) -> set[str]:
            try:
                return {ai[4][0] for ai in socket.getaddrinfo(d, None, socket.AF_INET)}
            except OSError:
                return set()
        ref = await run_in_threadpool(resolve, cfg.ui.domain) if cfg.ui.domain else set()
        got = await run_in_threadpool(resolve, domain)
        return {"domain": domain, "addresses": sorted(got), "points_here": bool(got & ref) if ref else None}

    # ------------------------------------------------------------ static UI
    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str):
        if path.startswith("api/"):
            raise HTTPException(404)
        f = (UI_DIST / path).resolve()
        if path and f.is_file() and str(f).startswith(str(UI_DIST.resolve()) + "/"):
            cache = "public, max-age=31536000, immutable" if "/_nuxt/" in f"/{path}" else "no-cache"
            return FileResponse(f, headers={"Cache-Control": cache})
        if index.exists():
            return FileResponse(index, headers={"Cache-Control": "no-cache"})
        return JSONResponse({"detail": "dashboard not built"}, status_code=503)

    return app
