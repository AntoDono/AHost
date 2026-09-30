# Manifest reference

One file per app: `<apps_dir>/<name>.toml`. It's validated by the same Pydantic model that the API and UI use.
Unknown keys are an error.

## Top level

| Key | Type | Default | Notes |
|---|---|---|---|
| `name` | string | required | `^[a-z][a-z0-9-]{0,39}$`; must match the file name |
| `workdir` | path | required | must resolve under an `allowed_roots` entry |
| `domains` | list of strings | `[]` | empty = no nginx site (workers, bots) |
| `user` | string | `run.default_user` | `root` only if listed in `/etc/ahost/root.allow` |
| `env_file` | path | none | relative to workdir; the app's own `.env`, applied *before* AHost's env |
| `env` | table | `{}` | non-secret values. Secrets go in the UI's secret editor, which writes `/etc/ahost/apps/<app>.env` (0600) |
| `after` / `wants` | list | `[]` | extra units to order after, e.g. `"postgresql.service"` |
| `enabled` | bool | `true` | `false` = stopped and disabled (what Stop in the UI sets) |
| `certs` | table | auto | domain → existing cert name, for adopted services; omitted = AHost issues one cert per app |
| `description` | string | `""` | shown in the dashboard |

Single-process shorthand: `command`, `runtime`, `port`, `gpus`, `health`, `restart` at the top level define a
process called `main`, plus a `/` route to it (when `domains` is set).

## `[processes.<id>]`

| Key | Type | Default | Notes |
|---|---|---|---|
| `command` | string | required | run by `bash -c` after runtime setup; `$PORT` is available |
| `runtime` | table | plain | one of `{ venv = "path" }`, `{ uv = true }`, `{ conda = "env" }`, `{ node = "22.19.0" }`; optional `dir` (subdir to run in) |
| `port` | int or `"auto"` | none | `"auto"` = allocate from the range; omit for processes with no listener |
| `gpus` | list of UUIDs | `[]` | the UI picks by name; stored as UUIDs |
| `health` | string | none | HTTP path probed on `127.0.0.1:$PORT` |
| `env` | table | `{}` | merged over top-level `env` |
| `after` | list of process ids | `[]` | ordering within the app, e.g. web after xvfb |
| `restart` | `always`/`on-failure`/`no` | `always` | |
| `stop_timeout` / `start_timeout` | seconds | 90 / 90 | adopted services copy their legacy values |
| `type` | `simple`/`exec`/`notify`/`forking` | `simple` | |
| `reload` | string | none | e.g. `"kill -HUP $MAINPID"` |
| `kill_mode` | `control-group`/`mixed`/`process` | `control-group` | |
| `memory_max`, `cpu_quota` | string | none | `"16G"`, `"200%"` |

## `[[routes]]`

Order matters (it's the nginx location order); more specific paths first.

| Key | Type | Default | Notes |
|---|---|---|---|
| `path` | string | required | `"/"`, `"/api/"`, or `"~ regex"` for regex locations |
| `to` | process id | — | proxy to that process's port. Exactly one of `to` / `static` |
| `static` | path | — | serve a directory (relative to workdir) |
| `spa_fallback` | string | none | e.g. `"/index.html"` for single-page apps |
| `websocket` | bool | `true` | upgrade headers |
| `streaming` | bool | `true` | no buffering/caching, `X-Accel-Buffering: no` |
| `timeout` | duration | `"1h"` (`"24h"` when streaming) | read/send timeout |
| `max_body` | size | `"10m"` | `client_max_body_size` |
| `cache` | duration | none | `expires` + `Cache-Control` for static routes |
| `headers` | table | `{}` | response headers (e.g. CORS) |

## `[proxy]`

| Key | Notes |
|---|---|
| `max_body` | default for all routes |
| `gzip` | bool, default `false` |
| `raw_server` | nginx directives inserted verbatim inside the HTTPS `server {}`, for things the model can't express (e.g. `auth_request` flows). Checked against a deny-list, see [security.md](security.md) |
| `raw_http` | directives placed at `http {}` level (upstreams, maps). Names must be prefixed `<app>_` |

## `[sandbox]`

| Key | Default | Notes |
|---|---|---|
| `level` | `"none"` | `none` / `standard` / `strict`, see [architecture.md](architecture.md#sandboxing) |
| `rw_paths` | `[]` | extra writable paths (relative to workdir, or absolute under `allowed_roots`) |
| `ro_paths` | `[]` | extra read-only paths |
| `caches` | `[]` | shared caches to bind rw: `huggingface`, `torch`, `uv`, `pip`, `npm`, `playwright` |

## Example: multi-process app

```toml
name     = "assistant"
workdir  = "/home/me/assistant"
domains  = ["assistant.example.com"]
env_file = "backend/.env"
after    = ["redis-server.service"]

[processes.xvfb]
command = "Xvfb :99 -screen 0 1920x1080x24 -nolisten tcp"

[processes.web]
command = "gunicorn app.wsgi:application --bind 127.0.0.1:$PORT --workers 3"
runtime = { uv = true, dir = "backend" }
port    = "auto"
env     = { DISPLAY = ":99" }
after   = ["xvfb"]
health  = "/api/health"

[processes.scheduler]
command = "python manage.py run_scheduler"
runtime = { uv = true, dir = "backend" }

[[routes]]
path = "/api/"
to   = "web"
[[routes]]
path  = "/_nuxt/"
static = "frontend/.output/public/_nuxt/"
cache = "1y"
[[routes]]
path = "/"
static = "frontend/.output/public"
spa_fallback = "/index.html"
```
