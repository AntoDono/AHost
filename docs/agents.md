# AHost for AI agents and scripts

This guide is for coding agents and scripts that deploy apps on an AHost server with the `ahost` CLI. The CLI prints
it too: `ahost guide`. The full command reference is [cli.md](cli.md), and every manifest field is in
[manifest.md](manifest.md).

## The model in one paragraph

Each app is one TOML manifest, `<apps_dir>/<name>.toml` (`apps_dir` is set in `/etc/ahost/ahost.toml`, often
`~/apps`). AHost generates everything else from it: a systemd unit `ahost@<name>.service`, a port, an nginx site,
and an HTTPS certificate. A router is one domain whose paths lead to apps (`apps.example.com/<path>`); it has its own
file, `<apps_dir>/routers/<name>.toml`. Never write systemd units or nginx configs by hand. Write the manifest and let
AHost render the rest.

## Rules

1. **Read first, then change.** `ahost status --json`, `ahost router list --json` and `ahost plan <app> --json` are
   read-only. Run them before and after every change.
2. **Validate before applying.** `ahost validate <app>` and `ahost plan <app>` must show no errors. Plan exits
   with 1 when there are errors.
3. **Changes need root.** `apply`, `start`/`stop`/`restart`, `remove` and `router create|add|assign|drop|apply`
   call `sudo ahost-helper`, which prompts for the operator's password. If you can't answer that prompt (there's
   no TTY, or the call fails with "a password is required" or "a terminal is required"), stop and hand the exact command to the human. Don't look
   for ways around sudo.
4. **The app reads its port from `$PORT`.** Use `port = "auto"`. Never hard-code a port, and never set `PORT` in
   `env`. Bind to `127.0.0.1`; nginx is the only public entry point.
5. **Leave existing apps alone** unless asked. Apps with a `[legacy]` section are mid-migration; don't `apply` them
   (they use `ahost adopt` / `ahost rollback`).
6. **Secrets stay out of manifests.** Put them in the app's own `.env` and reference it with `env_file`.
7. **Don't delete.** `ahost remove` takes an app off hosting but keeps its folder and data. Use it only when asked.

## Recipe: host a new app on its own domain

```bash
# 1. the app must listen on $PORT; check it starts by hand first, e.g.
cd /srv/projects/notes && PORT=18555 .venv/bin/uvicorn app:app --host 127.0.0.1 --port 18555

# 2. write the manifest
cat > ~/apps/notes.toml <<'TOML'   # apps_dir from /etc/ahost/ahost.toml
name = "notes"
description = "Notes API"
workdir = "/srv/projects/notes"
domains = ["notes.example.com"]      # its DNS record must already point at this server
env_file = [".env"]                  # optional, relative to workdir
command = "uvicorn app:app --host 127.0.0.1 --port $PORT"
runtime = { venv = ".venv" }         # or { uv = true } / { node = "22.19.0" } / { conda = "env" }
port = "auto"
health = "/healthz"                  # optional; a path that returns < 500 when the app is up
TOML

# 3. check, then apply
ahost validate notes
ahost plan notes            # read the steps: port, unit, site, certificate
ahost apply notes           # writes files, starts, waits until healthy, gets the certificate
ahost status --json         # confirm "active" and the port
curl -sI https://notes.example.com/healthz
```

`apply` fails without changing the site if the service doesn't become healthy. Read `ahost logs notes -n 100`, fix,
and run `apply` again. It's idempotent.

## Recipe: host an app on a router path

Use this when the app shouldn't get its own domain. Pick a router from `ahost router list --json`.

```bash
# manifest without domains; it still needs a port or a static folder
cat > ~/apps/notes-api.toml <<'TOML'
name = "notes-api"
workdir = "/srv/projects/notes"
command = "uvicorn app:app --host 127.0.0.1 --port $PORT"
runtime = { venv = ".venv" }
port = "auto"
TOML

ahost validate notes-api && ahost apply notes-api
ahost router add apps /notes-api --app notes-api     # now https://apps.example.com/notes-api/
# or, for a path that was reserved earlier:
ahost router assign apps /notes-api notes-api
```

With the default, strip mode, the app sees `/` instead of `/notes-api/`, and nginx fixes up redirects and cookies. That works
for APIs. Frontends that link to absolute paths (`/assets/app.js`, `/_nuxt/...`) break under a path. Build them
with a base path (Vite `base`, Nuxt `app.baseURL`, Next.js `basePath`, Gradio `root_path`, Streamlit
`--server.baseUrlPath`) and use `--no-strip`, or give them their own domain. The limits are in [routers.md](routers.md).

A frontend and its API on the same router (`/notes/` and `/notes-api/`) share an origin: no CORS setup, and cookies
work.

## Recipe: a static site (no process)

```toml
name = "docs"
workdir = "/srv/projects/docs"
domains = ["docs.example.com"]

[[routes]]
path = "/"
static = "dist"                 # relative to workdir
spa_fallback = "/index.html"    # for single-page apps
```

## Recipe: GPU app

```toml
name = "whisper"
workdir = "/srv/projects/whisper"
command = "python serve.py --port $PORT"
runtime = { venv = ".venv" }
port = "auto"
gpus = ["GPU-8f1c..."]          # UUIDs from `nvidia-smi -L`; the app sees them as cuda:0, cuda:1, ...
start_timeout = 300             # model loading
```

## Changing an existing app

Edit its manifest, then `ahost plan <app>` (read the diff) and `ahost apply <app>`. For code changes only:
`ahost restart <app>`. That re-runs the start command, but it doesn't rebuild frontends or run migrations; do those
first.

## Read-only commands, safe to run any time

| Command | Output |
|---|---|
| `ahost status --json` | every process: unit, active/enabled, port, domains, router paths |
| `ahost plan [APP...] --json` | errors, warnings, ports, steps, files that would change |
| `ahost validate [APP...]` | schema + policy check |
| `ahost router list --json` | routers and their paths |
| `ahost router plan NAME` | the nginx site a router would get |
| `ahost logs APP -n 200` | journal of all of the app's processes |

## When something fails

| Symptom | Likely cause |
|---|---|
| plan: `outside allowed_roots` | workdir or a path isn't under an allowed root; move the project |
| plan: `PORT is managed by AHost` | remove `PORT` from `env`; use `port` |
| plan: `domain ... is still served by legacy site` | an old hand-written nginx site owns the domain; ask the human |
| apply: `not healthy` | the app didn't listen on `$PORT` in time, or `health` returned >= 500; check `ahost logs` |
| apply: certificate error | the domain's DNS doesn't point at this server yet |
| `a password is required` / `a terminal is required` | you can't sudo; hand the command to the human |
