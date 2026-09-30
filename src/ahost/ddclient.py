"""Keep ddclient's host list in sync with the manifests.

ddclient.conf holds credentials, so this module never touches (or returns) anything but host lines. The file is split
into blocks: a block starts at a `protocol=` line; its zone is the `login=` value (Namecheap uses the domain as login).
Within each block, host lines (lines without `=`) are replaced by the desired hosts for that zone. Every other line
(settings, logins, passwords, comments) is kept byte-for-byte.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Block:
    zone: str | None
    lines: list[str] = field(default_factory=list)  # raw lines, host lines included
    hosts: list[str] = field(default_factory=list)


def _is_host_line(line: str) -> bool:
    s = line.split("#", 1)[0].strip()
    return bool(s) and "=" not in s


def _hosts_in(line: str) -> list[str]:
    s = line.split("#", 1)[0].strip().rstrip("\\").strip()
    return [h for h in s.replace(",", " ").split() if h]


def parse(text: str) -> tuple[list[str], list[Block]]:
    """-> (header lines before the first block, blocks)."""
    header: list[str] = []
    blocks: list[Block] = []
    cur: Block | None = None
    for line in text.splitlines():
        s = line.split("#", 1)[0].strip()
        if s.startswith("protocol="):
            cur = Block(zone=None)
            blocks.append(cur)
        if cur is None:
            header.append(line)
            continue
        if s.startswith("login="):
            cur.zone = s.split("=", 1)[1].strip().strip("'\"")
        if _is_host_line(line):
            cur.hosts += _hosts_in(line)
        cur.lines.append(line)
    return header, blocks


def zone_of(host: str, zones: list[str]) -> str | None:
    """Longest zone that host belongs to ('@.x.com' and 'x.com' both belong to x.com)."""
    h = host.removeprefix("@.")
    best = None
    for z in zones:
        if (h == z or h.endswith("." + z)) and (best is None or len(z) > len(best)):
            best = z
    return best


@dataclass
class SyncPlan:
    added: dict[str, list[str]]
    removed: dict[str, list[str]]
    unplaceable: list[str]  # desired hosts whose zone has no block (no credentials)
    new_text: str

    @property
    def changed(self) -> bool:
        return any(self.added.values()) or any(self.removed.values())


def sync(text: str, desired: set[str]) -> SyncPlan:
    header, blocks = parse(text)
    zones = [b.zone for b in blocks if b.zone]
    want: dict[str, list[str]] = {z: [] for z in zones}
    unplaceable = []
    for h in sorted(desired):
        z = zone_of(h, zones)
        if z is None:
            unplaceable.append(h)
        else:
            want[z].append(h)
    out = list(header)
    added: dict[str, list[str]] = {}
    removed: dict[str, list[str]] = {}
    for b in blocks:
        if not b.zone:  # a block we can't attribute: keep untouched
            out += b.lines
            continue
        new_hosts = want.get(b.zone, [])
        added[b.zone] = sorted(set(new_hosts) - set(b.hosts))
        removed[b.zone] = sorted(set(b.hosts) - set(new_hosts))
        placed = False
        for line in b.lines:
            if _is_host_line(line):
                if not placed:
                    out += new_hosts
                    placed = True
                continue
            out.append(line)
        if not placed:
            out += new_hosts
    new_text = "\n".join(out) + ("\n" if text.endswith("\n") else "")
    return SyncPlan(added, removed, unplaceable, new_text)
