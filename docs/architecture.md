# Architecture

## Components

| Component | Runs as | Role |
|---|---|---|
| `ahost` (FastAPI + CLI) | `ahost` system user | reads manifests, computes plans, serves the API and UI, streams logs/status |
| `ahost-helper` | root, via one sudoers rule for user `ahost` | renders and installs generated files, runs systemctl/nginx/certbot/ddclient. Fixed verbs only. See [security.md](security.md) |
| `ahost@.service` / `ahost@.target` | systemd templates | one instance per app (or per process of a multi-process app) |
| `/usr/local/lib/ahost/run` | inside each unit | sets up the runtime (venv/uv/conda/node) and `exec`s the app command |
| nginx | as installed | serves `/etc/nginx/ahost.d/*.conf` next to any existing sites |
| certbot | as installed | issues certs with the webroot challenge; its own timer renews them |
| SQLite (`/var/lib/ahost/ahost.db`) | `ahost` | port registry, apply history, health samples, UI users |

Manifests are the **source of truth**. The database only holds things that can't be derived from them
(which port an app was given, history, users). Generated files are never hand-edited; the next apply overwrites them.

## The reconcile loop

```
load manifests ─▶ validate ─▶ allocate/free ports ─▶ render desired files ─▶ diff against disk + live state
      ─▶ plan (list of steps) ─▶ apply steps in order, each verified, each reversible
```

- **Idempotent.** A second `apply` with no manifest changes is a no-op.
- **Minimal.** Only what changed is touched: a GPU change restarts the service but leaves nginx alone,
  and a route change reloads nginx without restarting the service.
- **Dry-run first-class.** `ahost plan [app]` prints the exact steps and file diffs; the UI preview uses the same output.
- **Ordered and verified:** unit → start → wait for port + health → site → reload → probe the public URL.
  A failed step rolls back that step and stops; earlier successful steps stay (and are reported).

## Apps: processes + routes

An app is a set of **processes** (what systemd runs) and **routes** (what nginx serves). One model covers
an API, a static site, a GPU worker without HTTP, and a multi-process app with a frontend, API and scheduler.
Field reference: [manifest.md](manifest.md).

## systemd

Unit naming: `ahost@<app>.service`, or `ahost@<app>:<process>.service` for multi-process apps. App names match
`^[a-z][a-z0-9-]*$`, so `:` never collides. Multi-process apps also get `ahost@<app>.target`, and every process has
`PartOf=` it, so start/stop/restart of the app acts on all processes.

Template (abridged):
```ini
[Unit]
Description=AHost app %i
After=network.target

[Service]
Type=simple
EnvironmentFile=-/etc/ahost/apps/%i.env
ExecStart=/usr/local/lib/ahost/run
Restart=always
RestartSec=5
KillMode=control-group
SyslogIdentifier=ahost-%i

[Install]
WantedBy=multi-user.target
```

The per-app `override.conf` sets `User`, `WorkingDirectory`, ordering (`After=`/`Wants=`, e.g. postgresql),
`Type`, `ExecReload`, timeouts, restart policy, resource limits and sandbox directives.

Design notes:
- `After=network.target`, not `network-online.target`: on hosts where `systemd-networkd-wait-online` fails
  (e.g. an unplugged NIC), `network-online` adds minutes to boot.
- **Environment precedence:** `EnvironmentFile=` entries are applied in order, later ones win. The app's own `.env`
  comes first and AHost's `/etc/ahost/apps/<app>.env` last, so `PORT` and AHost-managed values always win.
  (A start script that re-sources `.env` itself can still override; the importer flags that.)
- The runner `exec`s the command, so `$MAINPID` is the app process, which keeps `ExecReload=kill -HUP $MAINPID` and
  `KillMode=mixed` correct. It uses `/bin/bash` by absolute path, so an app's restricted `PATH` can't break it.
- **Stop in the UI = stop + disable** (the app stays stopped across reboots); Start = enable + start.

## Ports

- Auto-allocated from `ports.range` (default 10000–10999) and stored in the registry. Before assigning, AHost checks
  live listeners (`ss -ltn`, v4 and v6), so services outside AHost are never collided with. `ports.reserved` is never used.
- A manifest may pin a port (`port = 5120`), e.g. when adopting a service other things already call by port.
- Injected as `$PORT`; multi-process apps get one per process that declares a port.
- Freed when the app is removed; kept across restarts and edits.

## nginx

- `nginx.conf` includes `/etc/nginx/ahost.d/*.conf` **after** existing `sites-enabled`, so adoption can happen next to
  hand-written sites.
- `00-default.conf`: an explicit **HTTPS catch-all** (`listen 443 ssl default_server`, self-signed cert, `return 444`).
  Without one, nginx uses the first 443 block it loads, and that silently changes as sites are added or moved.
- `00-common.conf`: shared snippets defined once: the `map $http_upgrade $connection_upgrade`, and the ACME webroot location.
- One file per app: an HTTP→HTTPS redirect server (which also serves ACME challenges) and one HTTPS server per certificate.
- Route defaults (overridable per route): WebSocket upgrade on; streaming on (`proxy_buffering off`, `proxy_cache off`,
  `X-Accel-Buffering: no`, long read timeout); standard `X-Forwarded-*` headers; per-app access/error logs in `log_dir`.
- Upstream names are prefixed with the app (`<app>_<process>`), so they never clash across files.
- **Duplicate `server_name` detection is done by AHost** across all enabled nginx files before writing.
  nginx only warns about duplicates, so `nginx -t` alone doesn't catch them.
- Every write goes: render → `nginx -t` → reload, and on failure the previous file is restored. Swapping a legacy
  site for a generated one is a single helper verb with a single reload.

## Certificates

certbot with the **webroot** challenge; certbot never edits nginx files.

For a new domain:
1. check that DNS resolves to this server; if not, stop with a clear message (no wasted Let's Encrypt attempts)
2. write the site HTTP-only (challenge + redirect), test, reload
3. `certbot certonly --webroot -w <webroot> --cert-name <app> -d … --deploy-hook "systemctl reload nginx"`
4. write the full site, test, reload

Renewal is certbot's own timer. Adding or removing a domain reissues the app's cert with the new list.
Adopted services can keep existing certificates (`certs` in the manifest), whatever authenticator they used, and
be switched to webroot later with `certbot reconfigure`. The dashboard shows each domain's expiry from the live
TLS handshake. On removal, the cert's renewal config is archived so certbot stops renewing an unserved name;
deleting the cert is a separate, explicit choice.

## GPUs

- Cards are identified by **UUID**. On multi-GPU machines, the `nvidia-smi` index, the `/dev/nvidiaN` minor number and
  CUDA's default device order are often three **different** numberings, so any index-based scheme pins the wrong card.
- AHost reads the mapping from `nvidia-smi --query-gpu=index,uuid,pci.bus_id` and
  `/proc/driver/nvidia/gpus/*/information` (bus → device minor).
- For an app with `gpus = [...]`: `CUDA_DEVICE_ORDER=PCI_BUS_ID`, `CUDA_VISIBLE_DEVICES=<uuids>`, and at sandbox level
  `strict`, `DevicePolicy=closed` + `DeviceAllow=` for the chosen `/dev/nvidiaN` plus `nvidiactl`, `nvidia-uvm`,
  `nvidia-uvm-tools`, `nvidia-modeset`.
- `CUDA_VISIBLE_DEVICES` renumbers visible cards from 0. Apps that hard-code `cuda:N` need that value updated; the UI warns
  when a manifest's env contains `cuda:` device strings.
- The GPU panel maps `nvidia-smi --query-compute-apps` PIDs to units through `/proc/<pid>/cgroup`, so it attributes
  memory to AHost apps and to unmanaged services alike.

## Sandboxing

systemd's own sandboxing: the same kernel primitives as bubblewrap (namespaces, seccomp, cgroups), but declarative,
auditable with `systemd-analyze security`, and with resource limits built in.

| Level | Adds |
|---|---|
| `none` | nothing (default for adopted services) |
| `standard` | `ProtectSystem=strict`, `PrivateTmp`, `NoNewPrivileges`, home hidden (`ProtectHome=tmpfs`) except bound paths |
| `strict` | + no capabilities, `SystemCallFilter=@system-service`, GPU device allow-list, `MemoryMax`/`CPUQuota` if set |

What survives restarts:

| Path inside the app | Backed by | Persistent |
|---|---|---|
| workdir, `rw_paths` | bind mount of the real directory | yes |
| declared `caches` (huggingface, torch, uv, pip…) | bind mount | yes |
| toolchains (uv pythons, nvm, conda) | read-only bind, auto-detected from the runtime | n/a |
| `/tmp` | private | no |
| any other home path | size-capped tmpfs | no, and writes fail when full, so a forgotten bind fails loudly instead of eating RAM |

Apps that run their own sandbox inside (e.g. something that calls `bwrap`) can't use `strict`.

## DNS (optional)

With `dns.provider = "ddclient"`, AHost renders ddclient's host list from all manifests' domains plus
`dns.static_hosts`. Names leave the list when their app is removed; the UI then reminds you to delete the DNS record,
because a stale A record on a dynamic IP eventually points at someone else's machine. A wildcard record
(`*.example.com`) removes the per-app DNS step entirely.

## Observed units

Services AHost doesn't manage (databases, a hand-tuned gateway, anything you're not ready to adopt) can be listed in
`observe` in the server config. They appear in the dashboard with status, logs and GPU usage, and their controls
are disabled. AHost never writes their unit or site.
