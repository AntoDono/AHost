# `ahost` command reference

`ahost` runs as your normal user. Read-only commands never need root. Commands that change the system call
`sudo /usr/local/sbin/ahost-helper <verb>`, which asks for your password. The helper takes names only and renders
every file itself from the validated manifest.

Every command takes `-c/--config PATH`. The default is `$AHOST_CONFIG`, then `/etc/ahost/ahost.toml`. The
`--json` flag prints machine-readable output. For a step-by-step guide aimed at agents and scripts, run
`ahost guide` ([agents.md](agents.md)).

## Read-only

| Command | What it does |
|---|---|
| `ahost status [--json]` | every app process: unit, active/enabled, port, domains, router paths |
| `ahost validate [APP...]` | schema + policy check of manifests; exit 1 on errors |
| `ahost plan [APP...] [--no-diff] [--json]` | what `apply` would change: ports, files (with diffs), steps; exit 1 on errors |
| `ahost logs APP [-n N] [-f]` | journal of all the app's processes |
| `ahost router list [--json]` | routers and their paths |
| `ahost router plan NAME` | the nginx site a router would get, as a diff |
| `ahost baseline APP [--compare]` | record (or compare) how the app responds through local nginx |
| `ahost import --unit U --name N [--site S] [--write]` | draft a manifest from a hand-written unit + nginx site (prints unless `--write`) |
| `ahost guide` | print the agent guide |

## Apps (need sudo)

| Command | What it does |
|---|---|
| `ahost apply APP` | make the system match the manifest: port, unit, start, wait until healthy, nginx site, certificate, and every router path pointing at the app. Idempotent. |
| `ahost start APP` | enable + start (comes back after reboots) |
| `ahost stop APP` | stop + disable (stays stopped after reboots) |
| `ahost restart APP` | restart all its processes (re-runs the start command; doesn't rebuild or migrate) |
| `ahost remove APP [--yes]` | off hosting: units, site and port released; its router paths become reserved. The folder, data and certificate are kept; the manifest moves to `<apps_dir>/.deleted/` |

Editing a manifest changes nothing until `ahost apply`.

## Routers (need sudo unless `--no-apply`)

A router is one domain whose paths lead to apps. See [routers.md](routers.md). Each change is saved and applied in
one step. With `--no-apply` only the file changes; run `ahost router apply NAME` later. If the apply fails, the
previous file is put back.

| Command | What it does |
|---|---|
| `ahost router create NAME DOMAIN [--description T]` | new router; the first apply gets its certificate (DNS must point here) |
| `ahost router add NAME /PATH [--app APP] [--no-strip]` | add a path, pointed at APP or reserved (404) |
| `ahost router assign NAME /PATH [APP] [--strip/--no-strip]` | point a path at APP, or reserve it again when APP is omitted |
| `ahost router drop NAME /PATH` | remove a path |
| `ahost router apply NAME` | write its nginx site (nginx -t, reload) |
| `ahost router remove NAME` | take its site down (file and certificate kept) |

## Migration (legacy services)

| Command | What it does |
|---|---|
| `ahost adopt APP [--yes]` | switch an imported app from its old unit + site to AHost; compares responses before/after and rolls back on any difference |
| `ahost rollback APP` | undo `adopt`: old unit and site back |

See [adopting-existing-services.md](adopting-existing-services.md).

## Server

| Command | What it does |
|---|---|
| `ahost ddclient [--apply]` | sync ddclient's host list with app domains, router domains, `dns.static_hosts` and `ui.domain` (credentials untouched) |
| `ahost ui-site` | the dashboard's nginx site + certificate (`ui.domain`) |
| `ahost serve` | run the dashboard API (normally `ahost.service`) |
| `ahost user add\|passwd\|list\|delete NAME` | dashboard accounts; run as the ahost user: `sudo -u ahost ahost user add NAME` |

## Exit codes

`0` success. `1` validation or plan errors, or a failed change (the message says what was rolled back). `2` bad
usage or a missing config.
