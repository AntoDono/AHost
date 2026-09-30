# Security

AHost can start processes, write system config and reload nginx, so it's effectively root-adjacent. The design goal:
**a compromise of the web UI, or of any hosted app, must not become root.**

## Threat model

| Actor | Can | Must not be able to |
|---|---|---|
| A hosted app (runs as the app user) | read/write its own files; with sandboxing, only its binds | call the root helper; edit other apps' generated config; read `/etc/ahost/apps/*.env` |
| The `ahost` service (web UI/API) | edit manifests, ask the helper for fixed operations | write arbitrary files as root; run arbitrary root commands; start a unit as root unless allow-listed |
| An anonymous internet user | reach the login page | anything else |

## Privilege separation

- `ahost` runs as its own system user, **not** the user apps run as. Otherwise any app could impersonate the manager.
- It shares only the manifest directory with the operator through a group (`ahost`, setgid dir, 2770).
- One sudoers line, for that user only:
  ```
  ahost ALL=(root) NOPASSWD: /usr/local/sbin/ahost-helper
  ```
- The helper and everything it imports are root-owned (`/usr/local/sbin/ahost-helper`, `/opt/ahost/`), so neither `ahost`
  nor any app can change what runs as root. The runtime is built by uv with root's own Python install dir and cache
  (`UV_PYTHON_INSTALL_DIR=/opt/ahost/python`, `UV_CACHE_DIR=/var/cache/ahost-uv`). A user's uv cache or uv-managed
  Python would let that user poison what root executes.

## The helper renders; it never writes supplied content

The helper takes **an app name**, not file contents. It loads that app's manifest, validates it with the strict model,
and renders files from its own root-owned templates. Checks, in addition to the schema:

- **User:** the app user by default; `root` only if the app is in root-owned `/etc/ahost/root.allow`.
- **Paths** (workdir, static, rw/ro paths): `realpath` must be under `allowed_roots` and outside `deny_paths`.
  No symlink escapes.
- **Env names** denied: `LD_PRELOAD`, `LD_LIBRARY_PATH`, `LD_AUDIT`, `NODE_OPTIONS`, `PYTHONSTARTUP`, `BASH_ENV`, `ENV`,
  `PROMPT_COMMAND`, and `PATH`/`PYTHONPATH` for root apps.
- **Raw nginx** (`raw_server`, `raw_http`) denied directives: `access_log`, `error_log`, `include`, `load_module`,
  `ssl_certificate*`, `lua*`, `perl*`, `js_*`, `root`/`alias` outside the app's workdir, `proxy_pass` to a unix socket
  outside the app's workdir. `raw_http` names must be prefixed `<app>_`.
- **Domains:** unique across manifests and all enabled nginx files.
- **Units:** systemctl only on `ahost@*` / `ahost@*.target`, plus legacy units listed in root-owned
  `/etc/ahost/legacy.allow` (used only while adopting existing services).

Verbs:

| Verb | Effect |
|---|---|
| `install` | template units, nginx common + catch-all files (idempotent) |
| `apply-unit <app>` | allocate ports; render env file(s) (0600), run script(s), unit override(s); daemon-reload |
| `apply-site <app>` | render site, `nginx -t`, reload; restore the previous file on failure. Refuses while a legacy site still serves the domain |
| `swap-site <app>` | disable the manifest's `legacy.site` + render the new site, **one** `nginx -t` + reload; restore both on failure |
| `unswap-site <app>` | rollback: remove the generated site and re-enable the legacy one, one reload |
| `legacy-unit <action> <app>` | systemctl on the manifest's `legacy.unit`, only if listed in `legacy.allow` |
| `remove <app>` | stop/disable, remove generated files, archive the cert's renewal config |
| `unit <start\|stop\|restart\|enable\|disable> <app>` | systemctl on the app's `ahost@` units (or its target) |
| `cert <app>` | certbot webroot for the manifest's domains |
| `ddclient-hosts` | render the ddclient host list, restart ddclient |
| `nginx-log <app> <access\|error> [--follow]` | read-only stream of that app's nginx logs |

Every call is logged to the journal with caller, verb, app and result.

## Web UI

- Binds to `127.0.0.1`; exposed only through nginx with HTTPS (as its own AHost app, `ahost-ui`).
- Password login: argon2id hashes, HttpOnly + Secure + SameSite=Strict session cookie, CSRF token on every mutating
  request, login rate limiting (per IP and per account), optional TOTP.
- No default password: the first user is created on the CLI (`ahost user add`).
- Secrets entered in the UI are write-only: shown masked, never returned by the API.
- Every mutating action is recorded in the apply history (who, when, what, result).

### The dashboard gets an origin of its own

Nothing else is ever served from the dashboard's origin (scheme + host + port). This is why a router can't use
`ui.domain`, and why apps can't be mounted under a path of the dashboard's domain. The helper and the API refuse both.

Browsers isolate pages by origin, not by path. If an app were served at `https://ahost.example.com/chat/`,
next to the dashboard at `https://ahost.example.com/`, any script running on the app's pages would be same-origin
with the dashboard. That includes an XSS bug in the app, or a compromised npm package in its frontend. Such a script
could:

- `fetch('/api/...')` with the signed-in admin's session. The browser attaches the cookie because the request goes to
  `/api/`; a cookie `Path` only filters by the request URL, never by the page that made the request. The `X-AHost`
  header check passes too, since same-origin scripts may set custom headers.
- fake the path it came from. `Origin` carries no path. `Referer` does, but same-origin scripts can set it
  (`fetch(url, { referrer: '/rack' })`) or leave it out.
- `window.open('/rack')` and operate the dashboard through its DOM, which same-origin pages are allowed to do.

Stripping the session cookie in nginx before it reaches the app only hides it from the app's backend. The attack runs
in the admin's browser, so that doesn't help. Hardening such as `Cross-Origin-Opener-Policy`, in-memory tokens and
blocking service workers makes an attack harder, but it doesn't add up to a boundary. Through the dashboard, an
attacker controls the whole server: units, manifests, and root-rendered nginx and systemd config. So AHost relies only
on what browsers do enforce:

| Setup | Dashboard | Apps / routers | Why it is safe |
|---|---|---|---|
| With domains | `https://ahost.example.com` | own domains, or a router such as `https://apps.example.com/<app>` | different host |
| Without domains (LAN, localhost) | `http://server:9900` | `http://server/<app>` via nginx on port 80 | different port |

The session cookie is host-only: it has no `Domain` attribute, so a sibling subdomain like `apps.example.com` never
receives it.

Apps under one router do share an origin with each other (see [routers.md](routers.md)). That is a trade-off you
choose per app. It never puts the dashboard at risk.

## Secrets

- Put secrets in `/etc/ahost/apps/<app>.env` (0600, root) via the UI or `ahost secret set`, or keep them in the app's
  own `.env` (make sure it's `0600`). Never in the manifest, which is meant to be committed.
- `ahost doctor` warns about world/group-readable `.env` files in app workdirs.

## Sandboxing

See [architecture.md](architecture.md#sandboxing). Adopted services start at `none`, so their behavior doesn't change
during adoption. Raise the level per app afterwards and check with `systemd-analyze security ahost@<app>`.
