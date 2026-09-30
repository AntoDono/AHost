# Web UI

## Stack

- **Bun** as package manager and script runner
- **Nuxt, SPA mode** (`ssr: false`), built with `bun run generate` to static files that the AHost API serves:
  one process, one port, no Node server in production
- **Nuxt UI** (Tailwind v4 components: tables, forms, modals, toasts, command palette)
- **@nuxt/icon** with Iconify sets **lucide** (interface) and **simple-icons** (runtime logos: Python, Node, Django,
  FastAPI…), installed locally (`@iconify-json/lucide`, `@iconify-json/simple-icons`) so icons work offline
- **@nuxt/fonts**, self-hosted, so there are no third-party font requests at runtime
- API types generated from the FastAPI OpenAPI schema (`openapi-typescript`), so UI and backend can't drift
- Live logs and status over Server-Sent Events

```bash
cd ui
bun install
bun run dev         # proxies /api to the dev API
bun run generate    # -> .output/public, served by ahost
```

## Pages

**Dashboard** (the rack) · **App** (drawer: overview · logs · config · history) · **GPUs** · **Ports** ·
**Create hosting** · **Settings** (users, server config, observed units).

## Design direction: "patch bay"

**Subject:** a control panel for one operator running a dozen or more web and GPU apps on one server.
**The page's job:** show at a glance what's up, what's where, and what's using which card, and make fixing it one click.

The pain AHost removes is never knowing *which port goes where*, so the UI is modeled on a **rack patch bay**:
each app is a strip of equipment, and each strip shows its wiring.

**Signature element: the routing strip.** Each app row reads left to right as the path a request takes:

```
 ● blog-api       api.example.com ──▶ :10003 ──▶ ahost@blog-api ──▶ —          ▮▮▮▯ 2m ago
 ● whisper        asr.example.com ──▶ :10007 ──▶ ahost@whisper  ──▶ [RTX 3090]  ▮▮▮▮
 ○ discord-bot    (no domain)                    ahost@discord-bot              ▮▮▯▯
```

Domain → port → service → GPU, drawn as thin "patch cables" in the app's cable color. Hovering a port lights its cable
across the page. The Ports view shows every cable in the range, plus listeners AHost doesn't manage. This is the one
bold element; everything around it stays quiet.

**Palette:** steel and signal, light-first.

| Token | Hex | Use |
|---|---|---|
| `steel` | `#DCE1E5` | page ground (powder-coated rack) |
| `panel` | `#F5F7F8` | strips, cards |
| `ink` | `#1B232C` | text |
| `cable` | `#2D6BD8` | primary actions, links, active cables |
| `signal` | `#E8A400` | amber LED: starting, attention, pending apply |
| `fault` | `#D0433F` | failed, health check down |

Running = a small green LED `#2EA043` with a soft glow, the only skeuomorphic touch.
Dark: ground `#15191E`, panel `#1E242B`, same signal colors.

**Type**
- Labels and section heads: **Barlow Condensed**, uppercase, +6% tracking, like silkscreened equipment labels. Used sparingly.
- Body/UI: **Instrument Sans**
- Data (ports, domains, PIDs, logs, diffs): **IBM Plex Mono**

**Layout:** narrow left rail; the main area is the rack of strips. Clicking a strip opens a right-side drawer, so you
never lose the rack.

**Motion:** two moments only: a cable lights on hover, and the LED pulses while a service is starting. Both are off
under `prefers-reduced-motion`.

**Copy:** plain verbs, sentence case. Buttons say what happens ("Restart service", "Apply changes",
"Remove from hosting"), and toasts reuse the verb ("Service restarted"). Errors say what failed and what to do next:
"nginx rejected the config: unknown directive 'proxy_bufer' on line 12. Nothing was changed."

**Quality floor:** works at phone width, visible keyboard focus, every status also has a text label (never color alone).

## Controls

Every strip, and the drawer header, has **Start**, **Stop**, **Restart** (`lucide:play`, `lucide:square`, `lucide:rotate-cw`),
enabled only when they make sense.
- **Stop** = stay stopped (stop + disable). It confirms and names what goes offline ("api.example.com will stop responding").
- **Start** = enable + start. **Restart** doesn't confirm.
- While running, the LED pulses amber. It turns green only when systemd reports *active* **and** the health check passes;
  red with the last log lines if it doesn't come up within the start timeout.
  Toasts: "Service restarted" / "Restart failed: health check /healthz returned 502".
- Multi-process apps: buttons act on the whole app; the drawer has per-process restart.
- Bulk: select strips to restart or stop them together.
- Every action lands in the app's History tab.
- Observed units show the buttons disabled, with the tooltip "Managed outside AHost".

## Logs

Every strip has a **Logs** button (`lucide:scroll-text`, key `L`). It opens the drawer's Logs tab:
- live tail over SSE with a **Follow** toggle; scrolling up pauses it
- sources: **App** (journal) · **Web requests** (the app's nginx access log) · **Web errors** (its nginx error log)
- process picker for multi-process apps; each line tagged with its process
- filters: text search with highlighting, level (errors / warnings / all), time range (live, 15m, 1h, 24h, **since last restart**)
- lines colored by level; JSON lines expand on click
- **Download** the view as `.log`; **Copy** a selection
- a failing strip shows its last error line inline on the dashboard

## Create hosting

A multi-step form on the left, and a live **"what Apply will do"** preview on the right (mono, as a diff, the same output as
`ahost plan`).

1. **Basics:** name, project folder (picker limited to `allowed_roots`), type: app / static site / app + static
2. **Runtime:** auto-detected (`.venv`, `venv/`, `uv.lock`, `environment.yml`, `package.json`); start command with `$PORT`
3. **Port:** auto, or manual (checked live against the registry and listeners: "used by X")
4. **Domains:** live DNS check (resolves here ✓ / not yet), HTTPS toggle (issued on apply once DNS is ✓)
5. **Routing:** WebSocket ✓, streaming ✓, timeout, max upload, static folders, extra processes/routes
6. **GPUs:** card picker by name and bus ID with live memory and current users; stored as UUIDs
7. **Environment:** key/value editor, env file picker, masked secrets
8. **Isolation:** sandbox level, writable paths, shared caches, memory/CPU limits
9. **Health & restart:** health path, restart policy, depends on (postgres, redis…)

**Apply** streams progress step by step. **Remove from hosting** (in the drawer) removes everything AHost generated and
frees the port; it keeps the project folder, its data and the certificate (with a checkbox to delete the cert).
You confirm by typing the app name.

## GPU view

Each card is a horizontal bar sized by its VRAM, so a 32 GB card is visibly longer than a 12 GB one. It's filled with
segments colored by the app using that memory, including services AHost only observes.
