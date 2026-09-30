# Adopting existing services

How to move hand-written systemd units and nginx sites under AHost **without breaking them**. This is the
generic procedure; keep your server-specific notes (which apps, their quirks, a tracker) outside this repo.

## Rules

1. **One app at a time.** The next app waits until the previous one has passed its soak period.
2. **Adoption changes who manages the app, not how it behaves.** Same port, command, user, environment and nginx
   behavior. Improvements (binding to 127.0.0.1, sandboxing, GPU pinning, moving off root) are separate later steps,
   each with its own check and rollback.
3. **Nothing gets deleted.** Legacy units and sites are disabled and archived until the very end.
4. **Every step has a rollback** that takes under a minute.
5. **Baseline before, compare after.**

## 0. Survey first

Before anything changes, find out:
- which units are custom (`/etc/systemd/system/*.service`), what each runs, as which user, with which env files
- every nginx site's server_names, upstream ports, static aliases, body sizes, timeouts, special locations
- **what's actually alive**: probe every domain through local nginx (`curl --resolve d:443:127.0.0.1`), and list listeners (`ss -ltnp`)
- **who calls whom**: grep project configs for other apps' domains and ports; check crontabs and scripts for `systemctl … <unit name>`
- where each app writes data (open file handles in `/proc/<pid>/fd`, DB files, caches outside the workdir)
- whether any process an app depends on runs **outside** its unit (e.g. a display server started from a login shell)
- the nginx HTTPS default server (if none is explicit, which site is acting as it?)
- GPU numbering (nvidia-smi index vs `/dev/nvidiaN` vs what apps put in `cuda:N`)
- DNS: which records point at the server, which ones dynamic DNS updates, and which are dangling

An independent read-only review of the plan against the live server is worth doing; it catches real mistakes.

## 1. Prep

- **Back up** `/etc/nginx`, `/etc/systemd/system`, `/etc/letsencrypt`, DNS client config, sudoers, under `umask 077`
  (the archive contains private keys). Snapshot `ss -ltnp`, `nginx -T`, `certbot certificates`.
- **Retire dead sites**: disable the symlink, move the file to `sites-archive/`, move the cert's renewal config to
  `renewal-archive/`, and remove the name from dynamic DNS **and** delete its record.
- **Fix out-of-unit dependencies** so each unit really owns its processes.
- **Install AHost**, including the HTTPS catch-all, *before* adopting anything. Check that every live domain behaves exactly as before.
- Put the units being adopted in `/etc/ahost/legacy.allow`, and anything that must stay root in `root.allow`.

## 2. Import (no system changes)

```bash
ahost import --all --dry-run      # writes manifests + _import-report.md
```

The importer maps unit settings (command, runtime, user, env/env files, ordering, restart policy, timeouts, reload,
kill mode, hardening) and nginx settings (proxy locations, aliases → static routes, body size, timeouts, buffering,
headers) into a manifest. It keeps existing ports and certificates, and rewrites hard-coded binds to `$PORT` with the
same value. Anything it can't express goes **verbatim** into `raw_server`/`raw_http` (upstreams renamed with the app prefix).

Then a **fidelity diff**: the legacy `systemctl show` and nginx block vs the generated ones, directive by directive.
Every difference is either zero or listed in the report as intended. Review and commit the manifests.

## 3. Cutover procedure (per app)

**Pre-flight**
```bash
ahost plan <app>
ahost baseline <app>          # status/headers/body hash of check URLs, via the domain and the backend port
sqlite3 <db> ".backup 'backups/<app>-<ts>.sqlite3'"      # or whatever the app's data is
```
Look at the legacy unit's recent errors, so pre-existing problems aren't blamed on the migration.

**Shadow test** (only for request/response apps where a second copy has no side effects)
```bash
ahost shadow <app>            # runs the new unit on a temp port, compares GET checks with the baseline, stops it
```
Skip it for bots, schedulers, queue workers, anything holding GPU memory, or apps whose start script forces its own
port. Those use a direct cutover: stop old, start new, and the downtime is the app's startup time.

**Swap**
```bash
ahost adopt <app>
```
Stop + disable the legacy unit → start `ahost@<app>` on the same port → wait for port + health → `swap-site`
(legacy site out, generated site in, one reload) → re-run the baseline via the public domain.
Each step undoes itself on failure.

**Verify**
- [ ] unit active and enabled; the port is owned by the new unit's cgroup
- [ ] baseline comparison matches
- [ ] same certificate is served (serial); `certbot renew --dry-run --cert-name <name>` passes
- [ ] HTTP → HTTPS redirect works
- [ ] no new warnings in the journal
- [ ] app-specific checks (log in, a real request, streaming, websockets, uploads, the bot is online…)
- [ ] downstream apps that call this one still work
- [ ] `systemctl restart ahost@<app>` comes back healthy

**Soak** 24h (72h for GPU and multi-process apps). Watch `NRestarts` and health history.

**Rollback** at any time before finalizing:
```bash
ahost rollback <app>
```
Manual equivalent, if AHost itself is broken:
```bash
sudo systemctl disable --now ahost@<app>
sudo rm /etc/nginx/ahost.d/<app>.conf
sudo ln -s ../sites-available/<site> /etc/nginx/sites-enabled/<site>
sudo nginx -t && sudo systemctl reload nginx
sudo systemctl enable --now <legacy>.service
```

## 4. Improve (after soak, one change at a time)

Move off root (check where the app's caches live, e.g. `/root/.cache/huggingface`), bind to 127.0.0.1 (ask first
if LAN clients may use the port directly), pin GPUs by UUID (and fix hard-coded `cuda:N`), then raise the sandbox level.
Each change: apply → app checks → rollback by reverting the manifest.

## 5. Finalize

Full reboot test; archive legacy unit files and sites; empty `legacy.allow`; final backup; switch adopted certs to
webroot (`certbot reconfigure`); tag the manifests repo.

## Order

Start with the simplest stateless HTTP app (already on 127.0.0.1, no GPU, no background jobs). Then the other simple
ones, then apps with background work, then GPU apps, then multi-process apps. Leave anything other apps depend on
for quiet hours, and check its dependents after its cutover.
