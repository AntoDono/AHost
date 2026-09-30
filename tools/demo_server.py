"""Run the real dashboard against a fictional server (for screenshots and UI work).

    uv run python tools/demo_server.py            # http://localhost:9911, sign in as demo / demo-password-123

Everything the API would read from the host (systemd, nginx, /proc, nvidia-smi, the journal) is replaced by the
made-up data below, so nothing about the machine you run it on shows up.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import random
import sys
import tempfile
import time
from pathlib import Path

import uvicorn
from fastapi.responses import StreamingResponse
from fastapi.routing import APIRoute

os.environ.setdefault("AHOST_UI_DIST", str(Path(__file__).resolve().parent.parent / "ui/.output/public"))

from ahost.api import app as api_app
from ahost.api import auth, status, system
from ahost.config import Config

NOW = time.time()
GPUS = [
    {"index": 0, "uuid": "GPU-demo-0", "name": "RTX 4090", "bus": "0000:01:00.0", "minor": 0, "total_mib": 24564,
     "used_mib": 17820, "util": 71, "temp": 64, "users": [{"name": "chat-ui", "mib": 15300}, {"name": "embedder", "mib": 2520}]},
    {"index": 1, "uuid": "GPU-demo-1", "name": "RTX 4090", "bus": "0000:21:00.0", "minor": 1, "total_mib": 24564,
     "used_mib": 9410, "util": 38, "temp": 55, "users": [{"name": "whisper", "mib": 6100}, {"name": "image-gen", "mib": 3310}]},
    {"index": 2, "uuid": "GPU-demo-2", "name": "RTX 3090", "bus": "0000:41:00.0", "minor": 2, "total_mib": 24576,
     "used_mib": 11980, "util": 12, "temp": 47, "users": [{"name": "ollama", "mib": 9900}, {"name": "whisper", "mib": 2080}]},
]


def _proc(unit, port, cmd, runtime="venv", mem=0, up=0, restarts=0, health=(True, 200, 6), active="active", sub="running"):
    ok, code, ms = health
    return {"id": "main", "unit": unit, "port": port, "command": cmd, "runtime": runtime, "active": active, "sub": sub,
            "boot": "enabled" if active != "inactive" else "disabled", "restarts": restarts, "pid": 4242 if active == "active" else 0,
            "memory": mem * 2**20 if active == "active" else None,
            "since": time.strftime("%a %Y-%m-%d %H:%M:%S", time.localtime(NOW - up)) if up else None,
            "health": {"ok": ok, "status": code, "ms": ms, "path": "/healthz"} if active == "active" else None}


def _app(name, desc, domains, proc, state="running", gpus=(), user="deploy"):
    return {"name": name, "description": desc, "domains": list(domains), "state": state, "user": user,
            "workdir": f"/home/deploy/projects/{name}", "processes": [proc], "gpus": list(gpus),
            "sandbox": "standard" if name in ("api", "shop-api") else "none", "legacy": None, "multi": False,
            "mounts": []}


APPS = [
    _app("api", "Public REST API", ["api.example.com"],
         _proc("ahost@api.service", 10003, "uvicorn main:app --host 127.0.0.1 --port $PORT", mem=212, up=86400 * 6)),
    _app("chat-ui", "LLM chat with streaming", ["chat.example.com"],
         _proc("ahost@chat-ui.service", 10007, "python -m server --port $PORT", mem=3900, up=3600 * 20, health=(True, 200, 11)),
         gpus=[0]),
    _app("whisper", "Speech-to-text", ["asr.example.com"],
         _proc("ahost@whisper.service", 10012, "uv run whisper-server --port $PORT", runtime="uv", mem=2100, up=3600 * 50),
         gpus=[1, 2]),
    _app("embedder", "Text embeddings (internal)", [],
         _proc("ahost@embedder.service", 10014, "python serve.py --port $PORT", mem=1400, up=86400 * 2)),
    _app("image-gen", "Diffusion API", ["img.example.com"],
         _proc("ahost@image-gen.service", 10015, "python app.py --port $PORT", mem=5200, up=900, restarts=2,
               health=(False, 502, 30)), state="unhealthy", gpus=[1]),
    _app("shop-api", "Store backend", ["shop.example.com", "api.shop.example"],
         _proc("ahost@shop-api.service", 10001, "gunicorn shop.wsgi --bind 127.0.0.1:$PORT --workers 4", mem=380, up=86400 * 12)),
    _app("discord-bot", "Community bot", [],
         _proc("ahost@discord-bot.service", None, "python bot.py", mem=64, up=86400 * 3, health=(True, 0, 0))),
    _app("scraper", "Nightly price scraper", ["scrape.example.com"],
         _proc("ahost@scraper.service", 10020, "node index.js", runtime="node", active="failed", sub="failed", restarts=5),
         state="failed"),
    _app("old-blog", "Archived blog", ["blog.example.com"],
         _proc("ahost@old-blog.service", 10002, "gunicorn blog.wsgi --bind 127.0.0.1:$PORT", active="inactive", sub="dead"),
         state="stopped"),
]
for a in APPS:
    if a["name"] == "discord-bot":
        a["processes"][0]["health"] = None

OBSERVED = [{"unit": u, "active": "active", "sub": "running", "memory": m * 2**20, "since": None}
            for u, m in (("postgresql.service", 310), ("redis-server.service", 12), ("ollama.service", 10400),
                         ("nginx.service", 48))]


ROUTERS = {
    "apps": {"name": "apps", "domain": "apps.example.com", "description": "Internal tools", "index": "/chat",
             "entries": [{"path": "/chat", "app": "chat-ui"}, {"path": "/asr", "app": "whisper"},
                         {"path": "/embed", "app": "embedder"}, {"path": "/grafana", "app": "api", "strip": False},
                         {"path": "/notes"}, {"path": "/wiki"}]},
    "lab": {"name": "lab", "domain": "lab.example.com", "description": "Experiments",
            "entries": [{"path": "/img", "app": "image-gen"}, {"path": "/scrape", "app": "scraper"},
                        {"path": "/playground"}]},
}


def _mounts():
    out: dict[str, list[str]] = {}
    for r in ROUTERS.values():
        for e in r["entries"]:
            if e.get("app"):
                out.setdefault(e["app"], []).append(f"{r['domain']}{e['path']}")
    return out


def fake_routers(cfg):
    from ahost import render
    from ahost.models import App, Router
    apps = {a["name"]: App.model_validate({"name": a["name"], "workdir": a["workdir"], "command": "run",
                                           "port": a["processes"][0]["port"] or "auto"}) for a in APPS}
    ports = {a["name"]: {"main": a["processes"][0]["port"]} for a in APPS if a["processes"][0]["port"]}
    out = []
    for raw in ROUTERS.values():
        r = Router.model_validate(raw)
        site, warnings = render.render_router(r, apps, ports, cfg, render.Facts(
            certs_present={x.cert_name for x in map(Router.model_validate, ROUTERS.values())},
            manifest_path=f"/home/deploy/apps/routers/{r.name}.toml"))
        out.append({**r.model_dump(), "cert_name": r.cert_name, "url": f"https://{r.domain}", "live": True,
                    "pending": False, "errors": [], "warnings": warnings, "config": site.content})
    return {"routers": out, "invalid": {}, "apps": sorted(a["name"] for a in APPS)}


def fake_overview(cfg):
    m = _mounts()
    for a in APPS:
        a["mounts"] = m.get(a["name"], [])
    return {"apps": APPS, "invalid": {}, "observed": OBSERVED, "gpus": GPUS, "ui_domain": "host.example.com",
            "time": int(time.time()),
            "system": {"hostname": "gpu-box", "load": [3.42, 3.1, 2.87], "cpus": 24, "mem_total": 128 * 2**30,
                       "mem_available": 71 * 2**30, "disk_total": 4 * 2**40, "disk_used": int(1.6 * 2**40),
                       "uptime_s": 86400 * 41}}


def fake_system(extra):
    t = time.time()
    cpus = []
    for i in range(24):
        base = [8, 64, 22, 91, 15, 4, 37, 12, 70, 5, 18, 44][i % 12]
        pct = max(0.0, min(100.0, base + 6 * math.sin(t / 3 + i)))
        cpus.append({"id": i, "core": i % 12, "package": 0, "pct": round(pct, 1)})
    return {"model": "AMD Ryzen Threadripper 7960X 24-Cores", "cpus": cpus, "cores": 12, "load": [3.42, 3.1, 2.87],
            "mem_total": 128 * 2**30, "mem_available": 71 * 2**30, "swap_total": 16 * 2**30, "swap_free": 15 * 2**30,
            "disk_total": 4 * 2**40, "disk_used": int(1.6 * 2**40), "uptime_s": 86400 * 41, "gpus": GPUS}


def fake_procs(cpu, _unit_to_app, limit=40):
    rows = [("python", "python -m server --port 10007", "chat-ui", 63.2, 3900), ("uvicorn", "uvicorn main:app --port 10003", "api", 11.4, 212),
            ("postgres", "postgres: 14/main: writer", None, 4.1, 96), ("node", "node index.js", None, 1.2, 140),
            ("nginx", "nginx: worker process", None, 0.8, 12)]
    random.seed(cpu)
    random.shuffle(rows)
    out = []
    for i, (name, cmd, app, pct, mem) in enumerate(rows[: 3 + cpu % 3]):
        out.append({"pid": 1000 + cpu * 37 + i, "name": name, "cmd": cmd, "user": {"postgres": "postgres", "nginx": "www-data"}.get(name, "deploy"),
                    "pct": round(pct * (0.5 + (cpu % 5) / 5), 1), "rss": mem * 2**20,
                    "unit": f"ahost@{app}.service" if app else f"{name}.service", "app": app})
    return out


def fake_ports(cfg):
    assigned = [{"port": p["port"], "app": a["name"], "process": "main", "pinned": False, "listening": a["state"] != "stopped"}
                for a in APPS for p in a["processes"] if p["port"]]
    other = [{"port": p, "who": w, "in_range": False, "reserved": p in (22, 80, 443)}
             for p, w in ((22, "sshd"), (80, "nginx"), (443, "nginx"), (5432, "postgres"), (6379, "redis-server"), (11434, "ollama"))]
    return {"range": [10000, 10999], "reserved": [22, 80, 443], "assigned": assigned, "other": other}


LOG = [
    (6, "Started AHost chat-ui: LLM chat with streaming."),
    (6, "INFO:     Loading model weights (bf16) onto cuda:0"),
    (6, "INFO:     Model ready in 41.2s (15.3 GiB VRAM)"),
    (6, "INFO:     Uvicorn running on http://127.0.0.1:10007"),
    (6, 'INFO:     127.0.0.1 - "POST /v1/chat/completions HTTP/1.1" 200 (stream, 812 tokens, 38.4 tok/s)'),
    (6, 'INFO:     127.0.0.1 - "GET /healthz HTTP/1.1" 200'),
    (4, "WARNING:  Request queue at 6/8, consider a second worker"),
    (6, 'INFO:     127.0.0.1 - "POST /v1/chat/completions HTTP/1.1" 200 (stream, 244 tokens, 41.0 tok/s)'),
    (3, "ERROR:    client disconnected mid-stream (req 7f3a); generation cancelled"),
    (6, 'INFO:     127.0.0.1 - "POST /v1/chat/completions HTTP/1.1" 200 (stream, 1290 tokens, 39.2 tok/s)'),
]


def main() -> None:
    state = Path(tempfile.mkdtemp(prefix="ahost-demo-"))
    (state / "apps").mkdir()
    cfg = Config.model_validate({"paths": {"apps_dir": str(state / "apps"), "state_dir": str(state),
                                           "allowed_roots": [str(state)]},
                                 "ui": {"domain": "host.example.com"}})
    store = auth.Store(cfg)
    store.add_user("demo", "demo-password-123")
    status.overview = fake_overview
    status.ports_view = fake_ports
    status.gpus_with_users = lambda _u: GPUS
    system.system = fake_system
    system.procs_on_cpu = fake_procs
    app = api_app.create_app(cfg, store)

    async def logs(name: str, source: str = "app", lines: int = 300, process: str | None = None, since: str | None = None):
        t0 = int((NOW - 600) * 1000)
        if source == "access":
            return {"lines": [{"msg": f'203.0.113.{i} - - [30/Sep/2026:14:{10 + i}:02 +0000] "GET /api/v1/items?page={i} HTTP/2.0" 200 {812 + i * 37} "-" "Mozilla/5.0"'} for i in range(12)]}
        if source == "error":
            return {"lines": []}
        return {"lines": [{"ts": t0 + i * 41000, "unit": f"ahost@{name}.service", "pri": p, "msg": m} for i, (p, m) in enumerate(LOG)]}

    async def stream(name: str):
        async def gen():
            yield ": connected\n\n"
            while True:
                await asyncio.sleep(3)
                line = {"ts": int(time.time() * 1000), "unit": f"ahost@{name}.service", "pri": 6,
                        "msg": f'INFO:     127.0.0.1 - "POST /v1/chat/completions HTTP/1.1" 200 (stream, {random.randint(90, 1400)} tokens)'}
                yield f"data: {json.dumps(line)}\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream")

    async def manifest(name: str):
        a = next(x for x in APPS if x["name"] == name)
        p = a["processes"][0]
        return {"toml": f'name = "{name}"\ndescription = "{a["description"]}"\nworkdir = "{a["workdir"]}"\n'
                        f'domains = {json.dumps(a["domains"])}\ncommand = "{p["command"]}"\nruntime = {{ venv = ".venv" }}\n'
                        f'port = "auto"\n'}

    async def preview(body: dict):
        return {"ok": True, "errors": [], "warnings": [], "ports": {"main": 10021},
                "steps": ["reserve port 10021 for main", "write unit override(s), env file(s), run script(s); systemctl daemon-reload",
                          "enable --now ahost@notes.service", "issue certificate 'notes' for notes.example.com (certbot webroot; DNS must point here)",
                          "write ahost.d/notes.conf; nginx -t; reload"],
                "text": "", "toml": 'name = "notes"\nworkdir = "/home/deploy/projects/notes"\ndomains = ["notes.example.com"]\n\n[processes.main]\ncommand = "uvicorn app:app --host 127.0.0.1 --port $PORT"\nruntime = { venv = ".venv" }\nport = "auto"\nhealth = "/healthz"\n'}

    async def detect(path: str):
        return {"venvs": [".venv"], "uv": False, "node": None, "conda": None, "hints": [{"kind": "python", "command": "python app.py"}]}

    async def dns(domain: str):
        return {"domain": domain, "addresses": ["198.51.100.20"], "points_here": True}

    async def routers_get():
        return fake_routers(cfg)

    async def router_put(name: str, body: dict):
        from ahost.models import Router
        ROUTERS[name] = Router.model_validate({**body, "name": name}).model_dump()
        await asyncio.sleep(0.4)
        return {"ok": True, "log": []}

    async def router_post(body: dict):
        from ahost.models import Router
        r = Router.model_validate({k: v for k, v in body.items() if k in ("name", "domain", "description")})
        ROUTERS[r.name] = r.model_dump()
        await asyncio.sleep(1.2)
        return {"ok": True, "log": []}

    async def router_apply(name: str):
        return {"ok": True, "log": []}

    async def router_delete(name: str, confirm: str = ""):
        ROUTERS.pop(name, None)
        return {"ok": True}

    for path, fn, methods in (("/api/routers", routers_get, ["GET"]), ("/api/routers", router_post, ["POST"]),
                              ("/api/routers/{name}", router_put, ["PUT"]), ("/api/routers/{name}", router_delete, ["DELETE"]),
                              ("/api/routers/{name}/apply", router_apply, ["POST"]),
                              ("/api/apps/{name}/logs", logs, ["GET"]), ("/api/apps/{name}/logs/stream", stream, ["GET"]),
                              ("/api/apps/{name}/manifest", manifest, ["GET"]), ("/api/preview", preview, ["POST"]),
                              ("/api/detect", detect, ["GET"]), ("/api/dns", dns, ["GET"])):
        app.router.routes.insert(0, APIRoute(path, fn, methods=methods))

    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9911
    print(f"AHost demo on http://localhost:{port}  (demo / demo-password-123)")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
