# AHost

**Self-hosted app hosting for a single Linux server.** Describe an app once in a small TOML file, and AHost
runs it: a systemd service, an nginx site with HTTPS, a port from a registry, GPU assignment, optional
sandboxing, and a web dashboard to start, stop, restart, and read logs.

It's built for the "one beefy box, many projects" setup (a GPU server, a homelab, a small VPS) where apps run
as plain processes in their own venv, uv, conda or Node environment, **not** in Docker, and where the
hand-written unit + nginx site + certbot routine has stopped scaling.

> **Status:** design complete, implementation starting. The docs describe the target behavior.

## What it does

- **One manifest per app** (`apps/<name>.toml`) is the only input. Everything else is generated.
- **Ports are allocated for you** from a reserved range, checked against live listeners, and freed on removal.
- **systemd template units** (`ahost@<app>.service`) with per-app overrides; multi-process apps are grouped
  under `ahost@<app>.target`.
- **nginx sites generated from one template**: WebSocket and streaming (SSE / LLM token streams) work by default,
  static folders are served by nginx, `nginx -t` runs before every reload, and a failed test is rolled back.
- **HTTPS via certbot** (webroot challenge); renewal uses certbot's own timer.
- **GPU assignment** by card UUID, enforced at the device level, not just through `CUDA_VISIBLE_DEVICES`.
- **Sandboxing** with systemd's native options (none / standard / strict), per app.
- **Dynamic DNS**: optionally generates the ddclient host list from your manifests.
- **Adopt existing services**: an importer turns hand-written units and nginx sites into manifests, and
  a cutover procedure moves them one at a time with a rollback at every step.
- **Web UI** (Nuxt + Bun) and a CLI (`ahost`) over the same core.
- **Least privilege**: the dashboard never runs as root. Root actions go through a small helper that
  renders files from validated manifests and accepts only a fixed set of verbs.

## How it fits together

```
 apps/*.toml ──▶ ahost (reconciler, runs as user "ahost")
                    │  plan: diff desired vs actual
                    ▼
               ahost-helper (root, via sudo, fixed verbs)
        ┌───────────┼──────────────┬───────────────┬─────────────┐
        ▼           ▼              ▼               ▼             ▼
  /etc/ahost/   systemd         nginx          certbot       ddclient
  apps/*.env    ahost@<app>     ahost.d/       (webroot)     host list
```

The core operation is idempotent: **make reality match the manifests**. `ahost plan` shows what would change;
`ahost apply` does it. Running it twice changes nothing the second time.

## Quick look

```toml
# apps/blog-api.toml
name     = "blog-api"
workdir  = "/home/me/projects/blog-api"
command  = "uvicorn main:app --host 127.0.0.1 --port $PORT"
runtime  = { venv = ".venv" }
domains  = ["api.example.com"]
health   = "/healthz"
```

```bash
ahost plan blog-api      # port 10003 · write unit + env · write nginx site · issue cert for api.example.com
ahost apply blog-api
ahost logs blog-api -f
```

More examples: [`examples/apps/`](examples/apps/): a static site, a GPU inference server, a multi-process app.

## Documentation

| Doc | What's in it |
|---|---|
| [docs/architecture.md](docs/architecture.md) | components, reconcile loop, systemd/nginx/cert/port/GPU/DNS details |
| [docs/manifest.md](docs/manifest.md) | every manifest field |
| [docs/security.md](docs/security.md) | threat model, the root helper, UI auth, sandbox levels |
| [docs/adopting-existing-services.md](docs/adopting-existing-services.md) | migrating hand-written units and nginx sites safely |
| [docs/ui.md](docs/ui.md) | web UI spec and design direction |
| [config/ahost.example.toml](config/ahost.example.toml) | server-wide settings |

## Layout on a server

```
/home/<you>/AHost/          this repo: platform code, templates, UI, docs
/home/<you>/apps/           your manifests (a separate git repo; server-specific, never pushed with AHost)
/etc/ahost/ahost.toml       server-wide config
/etc/ahost/apps/            generated env files (0600, may hold secrets)
/etc/nginx/ahost.d/         generated nginx sites
/etc/systemd/system/ahost@.service, ahost@.target, ahost@<app>.service.d/
/var/lib/ahost/             state: port registry, apply history, users (SQLite)
```

## Planned repo layout

```
ahost/        Python package: models, reconciler, ports, systemd, nginx, certs, gpu, api (FastAPI), cli (Typer)
helper/       ahost-helper: the root-side verbs (installed root-owned to /usr/local/sbin)
templates/    ahost@.service, ahost@.target, override.conf.j2, site.conf.j2, ddclient.j2
ui/           Nuxt (SPA) + Nuxt UI, built with Bun, served by the API as static files
deploy/       ahost.service, sudoers snippet, install script
examples/     example manifests
tests/        reconciler plan tests against fixture manifests (no root needed)
```

## Requirements

Linux with systemd ≥ 249 and cgroup v2, nginx, certbot, [uv](https://docs.astral.sh/uv/), Bun (UI build only).
NVIDIA driver for GPU features. Tested target: Ubuntu 22.04. uv provides Python 3.12, so the system Python version doesn't matter.

## Development

All Python is managed with **uv**: `pyproject.toml` + `uv.lock`, Python pinned in `.python-version`.

```bash
uv sync                 # create .venv from uv.lock
uv run ahost plan       # read-only: works without root
uv run pytest
uv add <package>        # never pip install
```

## Installation layout

The installed service and the root helper **never run from a user-writable checkout**. Apps run as a normal user,
so if AHost ran from that user's home, any compromised app could modify the code that runs as root.
`deploy/install.sh` (run with sudo) builds a root-owned copy with uv, using root's own Python and cache:

```
/opt/ahost/python/     uv-managed CPython   (UV_PYTHON_INSTALL_DIR)
/opt/ahost/venv/       runtime venv, installed --frozen from uv.lock
/var/cache/ahost-uv/   root's uv cache      (UV_CACHE_DIR), never a user's cache
/usr/local/sbin/ahost-helper   root-owned launcher → /opt/ahost/venv/bin/python -m ahost.helper
```

`ahost.service` runs `/opt/ahost/venv/bin/ahost serve` as the `ahost` user. To upgrade, review the diff and re-run the install.

## Why not Coolify / Dokploy / CapRover?

They're good, but Docker-first: they want to own ports 80/443 with their own proxy, and every app has to become an
image. AHost is for servers where apps already run as plain processes, and it adopts them one at a time
next to an existing nginx.
