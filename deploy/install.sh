#!/usr/bin/env bash
# Install (or upgrade) AHost from a committed git revision into root-owned /opt/ahost.
#
#   sudo deploy/install.sh --config /path/to/ahost.toml [--rev HEAD] [--legacy-allow FILE] [--root-allow FILE]
#
# Safety: builds from `git archive <rev>` (committed code only, not the working tree), with root's own uv,
# Python and cache, so nothing under a user's home can change what runs as root afterwards.
set -euo pipefail
[ "$EUID" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }

UV_VERSION=0.9.28
repo="$(cd "$(dirname "$0")/.." && pwd)"
rev=HEAD; config=""; legacy_allow=""; root_allow=""
while [ $# -gt 0 ]; do
  case "$1" in
    --rev) rev="$2"; shift 2 ;;
    --config) config="$2"; shift 2 ;;
    --legacy-allow) legacy_allow="$2"; shift 2 ;;
    --root-allow) root_allow="$2"; shift 2 ;;
    *) echo "unknown arg $1" >&2; exit 2 ;;
  esac
done
op_user="${SUDO_USER:-}"
umask 022

echo "== uv (root-owned, checksum-verified) $UV_VERSION"
if ! /usr/local/bin/uv --version 2>/dev/null | grep -q " $UV_VERSION"; then
  tmp=$(mktemp -d)
  base="https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-x86_64-unknown-linux-gnu.tar.gz"
  curl -fsSL -o "$tmp/uv.tgz" "$base"
  curl -fsSL -o "$tmp/uv.tgz.sha256" "$base.sha256"
  (cd "$tmp" && echo "$(cut -d' ' -f1 uv.tgz.sha256)  uv.tgz" | sha256sum -c -)
  tar -xzf "$tmp/uv.tgz" -C "$tmp"
  install -o root -g root -m 0755 "$tmp"/uv-x86_64-unknown-linux-gnu/uv /usr/local/bin/uv
  rm -rf "$tmp"
fi
export UV_PYTHON_INSTALL_DIR=/opt/ahost/python UV_CACHE_DIR=/var/cache/ahost-uv UV_NO_CONFIG=1 \
       UV_PYTHON_PREFERENCE=only-managed HOME=/root

echo "== source: $(git -C "$repo" rev-parse --short "$rev") ($(git -C "$repo" log -1 --format=%s "$rev"))"
src=$(mktemp -d); trap 'rm -rf "$src"' EXIT
git -C "$repo" archive "$rev" | tar -x -C "$src"
[ -f "$src/uv.lock" ] || { echo "uv.lock missing in $rev" >&2; exit 1; }

echo "== python + venv"
# Each install gets its own directory; /opt/ahost/venv is a symlink to the active one (atomic switch, easy rollback).
# Building in place matters: venv scripts hard-code their interpreter path, so a venv can't be renamed afterwards.
mkdir -p /opt/ahost/venvs
/usr/local/bin/uv python install 3.12
revshort=$(git -C "$repo" rev-parse --short=12 "$rev")
target="/opt/ahost/venvs/$revshort-$(date +%Y%m%d%H%M%S)"
/usr/local/bin/uv venv --python 3.12 "$target"
(cd "$src" && /usr/local/bin/uv export --frozen --no-dev --no-hashes --no-emit-project -o "$src/req.txt")
/usr/local/bin/uv pip install --python "$target" -r "$src/req.txt"
/usr/local/bin/uv pip install --python "$target" --no-deps "$src"
"$target/bin/python" -I -c "import ahost.helper, ahost.cli" || { echo "smoke test failed" >&2; exit 1; }
[ -L /opt/ahost/venv ] || rm -rf /opt/ahost/venv /opt/ahost/venv.old   # migrate from the old layout
ln -sfn "$target" /opt/ahost/venv.tmp && mv -T /opt/ahost/venv.tmp /opt/ahost/venv
# keep the 3 newest installs for rollback: ln -sfn /opt/ahost/venvs/<older> /opt/ahost/venv
ls -1dt /opt/ahost/venvs/* | tail -n +4 | xargs -r rm -rf
chown -R root:root /opt/ahost /var/cache/ahost-uv; chmod -R go-w /opt/ahost
git -C "$repo" rev-parse "$rev" > /opt/ahost/REVISION

echo "== dashboard UI (built by $op_user with bun; only static files are installed)"
if [ -d "$src/ui" ]; then
  [ -n "$op_user" ] || { echo "run via sudo so the UI can be built as your user" >&2; exit 1; }
  bun="$(getent passwd "$op_user" | cut -d: -f6)/.bun/bin/bun"
  [ -x "$bun" ] || { echo "bun not found at $bun" >&2; exit 1; }
  build=$(sudo -u "$op_user" mktemp -d)
  tar -C "$src" -c ui | sudo -u "$op_user" tar -C "$build" -x
  sudo -u "$op_user" -H bash -c "cd '$build/ui' && '$bun' install --frozen-lockfile && '$bun' run generate" > "$build/build.log" 2>&1 \
    || { tail -30 "$build/build.log" >&2; echo "UI build failed (log: $build/build.log)" >&2; exit 1; }
  rm -rf /opt/ahost/ui.new && cp -r "$build/ui/.output/public" /opt/ahost/ui.new
  chown -R root:root /opt/ahost/ui.new && chmod -R go-w,a+rX /opt/ahost/ui.new
  rm -rf /opt/ahost/ui.old; [ -d /opt/ahost/ui ] && mv /opt/ahost/ui /opt/ahost/ui.old
  mv /opt/ahost/ui.new /opt/ahost/ui
  rm -rf "$build"
fi

echo "== launchers"
install -o root -g root -m 0755 "$src/deploy/ahost-helper" /usr/local/sbin/ahost-helper
rm -f /usr/local/bin/ahost
install -o root -g root -m 0755 "$src/deploy/ahost" /usr/local/bin/ahost

echo "== user, group, sudoers"
getent group ahost >/dev/null || groupadd --system ahost
id ahost >/dev/null 2>&1 || useradd --system --gid ahost --home-dir /var/lib/ahost --shell /usr/sbin/nologin ahost
[ -n "$op_user" ] && usermod -aG ahost "$op_user"
visudo -cf "$src/deploy/sudoers" >/dev/null
install -o root -g root -m 0440 "$src/deploy/sudoers" /etc/sudoers.d/ahost

echo "== /etc/ahost"
install -d -o root -g root -m 0755 /etc/ahost /etc/ahost/apps
install -d -o root -g ahost -m 0750 /var/lib/ahost
install -d -o ahost -g ahost -m 0700 /var/lib/ahost/ui
if [ -n "$config" ]; then install -o root -g root -m 0644 "$config" /etc/ahost/ahost.toml; fi
[ -f /etc/ahost/ahost.toml ] || { echo "no /etc/ahost/ahost.toml; pass --config" >&2; exit 1; }
for f in legacy.allow root.allow; do [ -f /etc/ahost/$f ] || install -o root -g root -m 0644 /dev/null /etc/ahost/$f; done
[ -n "$legacy_allow" ] && install -o root -g root -m 0644 "$legacy_allow" /etc/ahost/legacy.allow
[ -n "$root_allow" ] && install -o root -g root -m 0644 "$root_allow" /etc/ahost/root.allow

apps_dir=$(/opt/ahost/venv/bin/python -I -c 'from ahost.config import load; print(load().paths.apps_dir)')
if [ -d "$apps_dir" ]; then chgrp -R ahost "$apps_dir"; chmod 2770 "$apps_dir"; fi

echo "== nginx include"
conf=/etc/nginx/nginx.conf
if ! grep -q 'include /etc/nginx/ahost.d/\*.conf;' "$conf"; then
  cp -p "$conf" "$conf.ahost-bak"
  sed -i 's|^\(\s*\)include /etc/nginx/sites-enabled/\*;|&\n\1include /etc/nginx/ahost.d/*.conf;|' "$conf"
  grep -q 'include /etc/nginx/ahost.d/\*.conf;' "$conf" || { echo "could not add include line" >&2; exit 1; }
  mkdir -p /etc/nginx/ahost.d
  if ! nginx -t 2>/dev/null; then cp -p "$conf.ahost-bak" "$conf"; echo "nginx -t failed; restored nginx.conf" >&2; exit 1; fi
fi

echo "== shared files (template units, nginx common + catch-all)"
/usr/local/sbin/ahost-helper install

echo "== dashboard service"
install -o root -g root -m 0644 "$src/deploy/ahost.service" /etc/systemd/system/ahost.service
systemctl daemon-reload
systemctl enable ahost.service >/dev/null 2>&1
systemctl restart ahost.service
ui_domain=$(/opt/ahost/venv/bin/python -I -c 'from ahost.config import load; print(load().ui.domain or "")')
if [ -n "$ui_domain" ]; then
  echo "== dashboard site https://$ui_domain (nginx + certificate)"
  /usr/local/sbin/ahost-helper ui-site
fi

echo
echo "installed AHost $(cat /opt/ahost/REVISION | cut -c1-8). Try: ahost status"
if [ -n "${ui_domain:-}" ] && [ -z "$(sudo -u ahost /opt/ahost/venv/bin/python -I -m ahost.cli user list 2>/dev/null)" ]; then
  echo "create your dashboard login:  sudo -u ahost ahost user add <name>"
fi
[ -n "$op_user" ] && echo "note: log out and back in (or 'newgrp ahost') for $op_user's new group membership."
