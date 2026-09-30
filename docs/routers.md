# Routers: several apps under one domain

A router is a shared domain whose paths lead to different apps:

```
https://apps.example.com/chat/   -> chat
https://apps.example.com/docs/   -> docs
https://apps.example.com/wiki/   (reserved: 404 until assigned)
```

Use one when a separate domain per app is more than you want (DNS records, certificates), for internal tools, or
to hand out addresses before the apps exist.

## File

One file per router, `<apps_dir>/routers/<name>.toml`. The Router page and `ahost router ...` edit the same file.

```toml
name = "apps"
domain = "apps.example.com"
description = "Internal tools"
index = "/chat"          # the bare domain redirects here; leave out for 404
# cert = "existing-cert" # default: a new certificate named ahost-router.<name>
# max_body = "50m"

[[entries]]
path = "/chat"
app = "chat"

[[entries]]
path = "/wiki"           # no app: reserved

[[entries]]
path = "/grafana"
app = "grafana"
strip = false            # the app is configured to live under /grafana
```

Paths are `/name`, lowercase letters, digits, `.`, `_` and `-`, up to four levels (`/team/tools/x`).

## What an app sees

**`strip = true` (default).** The prefix is removed: `/chat/api/x` reaches the app as `/api/x`. To keep the app
working without knowing about the prefix, nginx also:

- rewrites redirects that are relative or point at the router's domain (`Location: /login` becomes `/chat/login`);
  redirects to other sites (OAuth providers) are left alone;
- moves cookies set for `/` under `/chat/`, so apps on the same router don't see each other's cookies;
- sends `X-Forwarded-Prefix: /chat`, which many frameworks can use to build links.

What nginx can't fix: HTML or JavaScript that links to absolute paths (`<script src="/static/app.js">`). Such apps
need their own base-path setting (`base` in Vite/Nuxt, `SCRIPT_NAME`/`root_path` in Python, `basePath` in Next.js);
then use `strip = false`.

**`strip = false`.** The URI is passed through unchanged; the app must serve everything under its prefix itself.

The app's own routes are all carried under the prefix: a `/static/` folder becomes `/chat/static/`, an exact
`= /health` becomes `= /chat/health`. Regex routes and `raw_server`/`raw_http` snippets are skipped (with a warning).
An app can have its own domains and router paths at the same time; it may also have none of its own domain.

## Why a router has its own domain

A router can't live on the dashboard's domain (`ui.domain`). `ahost.example.com/chat` next to the dashboard at
`ahost.example.com` would let any script on the chat app act as the signed-in admin: browsers isolate by origin
(scheme + host + port), not by path, and neither nginx nor the API can tell which page a request came from. The
details, and the alternatives that were considered, are in [security.md](security.md#the-dashboard-gets-an-origin-of-its-own).

The usual setup:

```
https://ahost.example.com          the AHost dashboard (manage everything)
https://apps.example.com/<app>     a router (what visitors use)
https://chat.example.com           an app on its own domain (optional, alongside router paths)
```

The router's domain needs one DNS record and one certificate, created once. Every app you add after that only needs a path.

## Security note

Apps on one router share an origin: cookies (unless the app scopes them), `localStorage` and service workers are
shared. Put apps that shouldn't trust each other on their own domains.

## Commands

```bash
ahost router list
ahost router plan apps     # the nginx site it would write (nothing changes)
ahost router apply apps    # write it, nginx -t, reload; gets the certificate the first time
ahost router remove apps   # take the site down (file and certificate kept)
```

`ahost apply <app>` also refreshes every router the app is on (its port may have changed). Removing an app turns
its paths back into reserved ones. Router domains are included in the ddclient host list (`ahost ddclient`).

As root, the helper renders the site itself from the router file and the apps' validated manifests
(`apply-router <name>`, `remove-router <name>`). A router's domain can't be an app's, a legacy site's, or the
dashboard's.
