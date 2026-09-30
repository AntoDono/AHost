"""System view: per-CPU load, the processes on a CPU, memory/disk. Reads /proc and /sys only (no root)."""

from __future__ import annotations

import os
import platform
import pwd
import shutil
import threading
import time
from pathlib import Path

CLK_TCK = os.sysconf("SC_CLK_TCK")
PAGE = os.sysconf("SC_PAGE_SIZE")
_lock = threading.Lock()
_prev: dict[str, object] = {"t": 0.0, "stat": None}


def _read_stat() -> list[tuple[int, int]]:
    """(idle, total) jiffies per cpuN."""
    out = []
    for line in Path("/proc/stat").read_text().splitlines():
        if line.startswith("cpu") and line[3].isdigit():
            vals = [int(x) for x in line.split()[1:]]
            idle = vals[3] + (vals[4] if len(vals) > 4 else 0)  # idle + iowait
            out.append((idle, sum(vals[:8])))
    return out


def cpu_percent() -> list[float]:
    """Per-CPU busy % since the previous call (sampled over 0.3s if the last sample is stale)."""
    with _lock:
        now = time.monotonic()
        prev = _prev["stat"]
        if prev is None or now - float(_prev["t"]) > 5:
            prev = _read_stat()
            time.sleep(0.3)
        cur = _read_stat()
        _prev.update(t=time.monotonic(), stat=cur)
    res = []
    for (i0, t0), (i1, t1) in zip(prev, cur, strict=False):  # type: ignore[arg-type]
        dt = t1 - t0
        res.append(round(100 * (1 - (i1 - i0) / dt), 1) if dt > 0 else 0.0)
    return res


def topology() -> list[dict]:
    cpus = []
    for d in sorted(Path("/sys/devices/system/cpu").glob("cpu[0-9]*"), key=lambda p: int(p.name[3:])):
        try:
            core = int((d / "topology/core_id").read_text())
            pkg = int((d / "topology/physical_package_id").read_text())
        except (OSError, ValueError):
            core, pkg = int(d.name[3:]), 0
        cpus.append({"id": int(d.name[3:]), "core": core, "package": pkg})
    return cpus


def _model() -> str:
    for line in Path("/proc/cpuinfo").read_text().splitlines():
        if line.startswith("model name"):
            return line.split(":", 1)[1].strip()
    return platform.processor()


def system(extra: dict) -> dict:
    pct = cpu_percent()
    topo = topology()
    for c in topo:
        c["pct"] = pct[c["id"]] if c["id"] < len(pct) else 0.0
    mem = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        k, v = line.split(":", 1)
        mem[k] = int(v.split()[0]) * 1024
    du = shutil.disk_usage("/")
    return {
        "model": _model(), "cpus": topo, "cores": len({(c["package"], c["core"]) for c in topo}),
        "load": [round(x, 2) for x in os.getloadavg()],
        "mem_total": mem.get("MemTotal"), "mem_available": mem.get("MemAvailable"),
        "swap_total": mem.get("SwapTotal"), "swap_free": mem.get("SwapFree"),
        "disk_total": du.total, "disk_used": du.used,
        "uptime_s": int(float(Path("/proc/uptime").read_text().split()[0])), **extra,
    }


def _proc_sample() -> dict[int, tuple[int, int, int, str]]:
    """pid -> (cpu ticks, last cpu, rss bytes, comm)."""
    out = {}
    for p in Path("/proc").iterdir():
        if not p.name.isdigit():
            continue
        try:
            raw = (p / "stat").read_text()
        except OSError:
            continue
        comm = raw[raw.index("(") + 1: raw.rindex(")")]
        f = raw[raw.rindex(")") + 2:].split()
        out[int(p.name)] = (int(f[11]) + int(f[12]), int(f[36]), int(f[21]) * PAGE, comm)
    return out


def procs_on_cpu(cpu: int, unit_to_app: dict[str, str], limit: int = 40) -> list[dict]:
    """Processes whose last CPU is `cpu`, with CPU % measured over 0.4s (not a lifetime average)."""
    a = _proc_sample()
    t = time.monotonic()
    time.sleep(0.4)
    b = _proc_sample()
    dt = time.monotonic() - t
    rows = []
    for pid, (ticks, last_cpu, rss, comm) in b.items():
        if last_cpu != cpu:
            continue
        prev = a.get(pid)
        pct = 100 * (ticks - prev[0]) / CLK_TCK / dt if prev else 0.0
        rows.append((pct, pid, rss, comm))
    rows.sort(key=lambda r: (-r[0], -r[2]))
    out = []
    for pct, pid, rss, comm in rows[:limit]:
        try:
            cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()
            uid = Path(f"/proc/{pid}").stat().st_uid
            unit = Path(f"/proc/{pid}/cgroup").read_text().strip().rsplit("/", 1)[-1]
        except OSError:
            continue
        try:
            user = pwd.getpwuid(uid).pw_name
        except KeyError:
            user = str(uid)
        out.append({"pid": pid, "name": comm, "cmd": cmd[:300] or f"[{comm}]", "user": user,
                    "pct": round(pct, 1), "rss": rss, "unit": unit, "app": unit_to_app.get(unit)})
    return out
