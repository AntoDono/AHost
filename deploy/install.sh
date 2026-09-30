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
mkdir -p /opt/ahost
/usr/local/bin/uv python install 3.12
rm -rf /opt/ahost/venv.new
/usr/local/bin/uv venv --python 3.12 /opt/ahost/venv.new
(cd "$src" && /usr/local/bin/uv export --frozen --no-dev --no-hashes --no-emit-project -o "$src/req.txt")
/usr/local/bin/uv pip install --python /opt/ahost/venv.new -r "$src/req.txt"
/usr/local/bin/uv pip install --python /opt/ahost/venv.new --no-deps "$src"
rm -rf /opt/ahost/venv.old; [ -d /opt/ahost/venv ] && mv /opt/ahost/venv /opt/ahost/venv.old
mv /opt/ahost/venv.new /opt/ahost/venv
chown -R root:root /opt/ahost /var/cache/ahost-uv; chmod -R go-w /opt/ahost
git -C "$repo" rev-parse "$rev" > /opt/ahost/REVISION

echo "== launchers"
install -o root -g root -m 0755 "$src/deploy/ahost-helper" /usr/local/sbin/ahost-helper
ln -sf /opt/ahost/venv/bin/ahost /usr/local/bin/ahost

echo "== user, group, sudoers"
getent group ahost >/dev/null || groupadd --system ahost
id ahost >/dev/null 2>&1 || useradd --system --gid ahost --home-dir /var/lib/ahost --shell /usr/sbin/nologin ahost
[ -n "$op_user" ] && usermod -aG ahost "$op_user"
visudo -cf "$src/deploy/sudoers" >/dev/null
install -o root -g root -m 0440 "$src/deploy/sudoers" /etc/sudoers.d/ahost

echo "== /etc/ahost"
install -d -o root -g root -m 0755 /etc/ahost /etc/ahost/apps
install -d -o root -g ahost -m 0750 /var/lib/ahost
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

echo
echo "installed AHost $(cat /opt/ahost/REVISION | cut -c1-8). Try: ahost status"
[ -n "$op_user" ] && echo "note: log out and back in (or 'newgrp ahost') for $op_user's new group membership."
