"""Port registry: which app/process owns which port.

Pinned ports (``port = 5120``) are recorded as-is. ``port = "auto"`` gets the lowest free port in the range that is
not reserved, not registered to another process, and not currently listening (IPv4 or IPv6).
"""

from __future__ import annotations

import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .models import App

SCHEMA = """
CREATE TABLE IF NOT EXISTS ports (
    port    INTEGER PRIMARY KEY,
    app     TEXT NOT NULL,
    process TEXT NOT NULL,
    pinned  INTEGER NOT NULL DEFAULT 0,
    UNIQUE (app, process)
);
"""


def listening_ports() -> dict[int, str]:
    """TCP listeners -> short description (process name if visible). Read-only, no root needed."""
    out = subprocess.run(["ss", "-ltnpH"], capture_output=True, text=True, check=False).stdout
    ports: dict[int, str] = {}
    for line in out.splitlines():
        cols = line.split()
        if len(cols) < 4:
            continue
        local = cols[3]
        try:
            port = int(local.rsplit(":", 1)[1])
        except (IndexError, ValueError):
            continue
        who = ""
        if len(cols) > 5 and 'users:((' in cols[5]:
            who = cols[5].split('"')[1] if '"' in cols[5] else ""
        ports.setdefault(port, who)
        if who and not ports[port]:
            ports[port] = who
    return ports


@dataclass
class Assignment:
    app: str
    process: str
    port: int
    pinned: bool


class PortError(Exception):
    pass


class Registry:
    def __init__(self, db_path: Path | str, cfg: Config):
        self.cfg = cfg
        self.db = sqlite3.connect(str(db_path))
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    def all(self) -> list[Assignment]:
        rows = self.db.execute("SELECT app, process, port, pinned FROM ports ORDER BY port").fetchall()
        return [Assignment(a, p, port, bool(pin)) for a, p, port, pin in rows]

    def get(self, app: str, process: str) -> int | None:
        row = self.db.execute("SELECT port FROM ports WHERE app=? AND process=?", (app, process)).fetchone()
        return row[0] if row else None

    def owner(self, port: int) -> tuple[str, str] | None:
        row = self.db.execute("SELECT app, process FROM ports WHERE port=?", (port,)).fetchone()
        return (row[0], row[1]) if row else None

    def resolve(self, app: App, *, listening: dict[int, str] | None = None, commit: bool = False) -> dict[str, int]:
        """Port for every process of ``app`` that has one. Raises PortError on conflicts.

        With commit=False nothing is written (used by `plan`); a would-be auto port is still computed.
        """
        listening = listening if listening is not None else listening_ports()
        result: dict[str, int] = {}
        taken_now: set[int] = set()
        for pid, proc in app.processes.items():
            if proc.port is None:
                continue
            current = self.get(app.name, pid)
            if isinstance(proc.port, int):
                port = proc.port
                owner = self.owner(port)
                if owner and owner != (app.name, pid):
                    raise PortError(f"{app.name}.{pid}: port {port} is registered to {owner[0]}.{owner[1]}")
                if port in self.cfg.ports.reserved:
                    raise PortError(f"{app.name}.{pid}: port {port} is reserved")
                pinned = True
            elif current is not None:
                port, pinned = current, False
            else:
                port, pinned = self._free(listening, taken_now), False
            taken_now.add(port)
            result[pid] = port
            if commit:
                self.db.execute("DELETE FROM ports WHERE app=? AND process=?", (app.name, pid))
                self.db.execute("INSERT INTO ports (port, app, process, pinned) VALUES (?,?,?,?)",
                                (port, app.name, pid, int(pinned)))
        if commit:
            # processes that no longer have a port
            keep = list(result)
            q = "DELETE FROM ports WHERE app=?" + (
                f" AND process NOT IN ({','.join('?' * len(keep))})" if keep else "")
            self.db.execute(q, (app.name, *keep))
            self.db.commit()
        return result

    def release(self, app: str) -> None:
        self.db.execute("DELETE FROM ports WHERE app=?", (app,))
        self.db.commit()

    def _free(self, listening: dict[int, str], taken_now: set[int]) -> int:
        lo, hi = self.cfg.ports.range
        used = {a.port for a in self.all()} | set(listening) | set(self.cfg.ports.reserved) | taken_now
        for p in range(lo, hi + 1):
            if p not in used:
                return p
        raise PortError(f"no free port in {lo}-{hi}")
