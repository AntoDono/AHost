"""Collect live status for the dashboard: units, health, ports, GPUs, system."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .. import gpu as gpu_mod
from .. import ops
from ..config import Config
from ..manifest_io import load_all
from ..models import App
from ..plan import open_registry
from ..ports import listening_ports

UNIT_PROPS = ["Id", "ActiveState", "SubState", "UnitFileState", "NRestarts", "MainPID", "MemoryCurrent",
              "ActiveEnterTimestamp", "Description", "CPUUsageNSec"]
_health_cache: dict[str, tuple[float, dict]] = {}


def units_show(units: list[str]) -> dict[str, dict[str, str]]:
    if not units:
        return {}
    out = subprocess.run(["systemctl", "show", *units, *[f"-p{p}" for p in UNIT_PROPS]],
                         capture_output=True, text=True, check=False).stdout
    res: dict[str, dict[str, str]] = {}
    for block in out.strip().split("\n\n"):
        d = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        if "Id" in d:
            res[d["Id"]] = d
    return res


def _unit_summary(p: dict[str, str]) -> dict:
    mem = p.get("MemoryCurrent", "")
    return {
        "active": p.get("ActiveState"), "sub": p.get("SubState"), "boot": p.get("UnitFileState"),
        "restarts": int(p.get("NRestarts") or 0), "pid": int(p.get("MainPID") or 0),
        "memory": int(mem) if mem.isdigit() else None, "since": p.get("ActiveEnterTimestamp") or None,
        "cpu_ns": int(p["CPUUsageNSec"]) if p.get("CPUUsageNSec", "").isdigit() else None,
    }


def _health(a: App, pid: str, port: int, max_age: float = 10) -> dict:
    key = f"{a.name}:{pid}:{port}"
    hit = _health_cache.get(key)
    if hit and time.time() - hit[0] < max_age:
        return hit[1]
    res = ops.probe(a, pid, port)
    _health_cache[key] = (time.time(), res)
    return res


def gpus_with_users(unit_to_app: dict[str, str]) -> list[dict]:
    inv = gpu_mod.inventory()
    if not inv:
        return []
    q = subprocess.run(["nvidia-smi", "--query-gpu=uuid,memory.used,utilization.gpu,temperature.gpu",
                        "--format=csv,noheader,nounits"], capture_output=True, text=True, check=False).stdout
    live = {}
    for line in q.strip().splitlines():
        uuid, used, util, temp = [c.strip() for c in line.split(",")]
        live[uuid] = {"used_mib": int(float(used)), "util": int(float(util)), "temp": int(float(temp))}
    apps_q = subprocess.run(["nvidia-smi", "--query-compute-apps=gpu_uuid,pid,used_memory",
                             "--format=csv,noheader,nounits"], capture_output=True, text=True, check=False).stdout
    users: dict[str, dict[str, int]] = {}
    for line in apps_q.strip().splitlines():
        try:
            uuid, pid, mem = [c.strip() for c in line.split(",")]
            cg = Path(f"/proc/{pid}/cgroup").read_text().strip().rsplit("/", 1)[-1]
        except (ValueError, OSError):
            continue
        who = unit_to_app.get(cg, cg.removesuffix(".service"))
        users.setdefault(uuid, {})
        users[uuid][who] = users[uuid].get(who, 0) + int(float(mem))
    return [{"index": g.index, "uuid": g.uuid, "name": g.name.replace("NVIDIA GeForce ", ""), "bus": g.bus_id,
             "minor": g.minor, "total_mib": g.memory_total_mib, **live.get(g.uuid, {}),
             "users": [{"name": k, "mib": v} for k, v in sorted(users.get(g.uuid, {}).items(), key=lambda x: -x[1])]}
            for g in inv]


def system_info() -> dict:
    load = os.getloadavg()
    mem = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        k, v = line.split(":", 1)
        mem[k] = int(v.split()[0]) * 1024
    du = shutil.disk_usage("/")
    up = float(Path("/proc/uptime").read_text().split()[0])
    return {"hostname": os.uname().nodename, "load": [round(x, 2) for x in load], "cpus": os.cpu_count(),
            "mem_total": mem.get("MemTotal"), "mem_available": mem.get("MemAvailable"),
            "disk_total": du.total, "disk_used": du.used, "uptime_s": int(up)}


def overview(cfg: Config) -> dict:
    apps, errors = load_all(Path(cfg.paths.apps_dir))
    reg = open_registry(cfg)
    units = [a.unit(p) for a in apps.values() for p in a.processes] + list(cfg.observe.units)
    props = units_show(units)
    unit_to_app = {a.unit(p): a.name for a in apps.values() for p in a.processes}

    jobs = []
    for a in apps.values():
        for pid, proc in a.processes.items():
            port = reg.get(a.name, pid) or (proc.port if isinstance(proc.port, int) else None)
            if port and props.get(a.unit(pid), {}).get("ActiveState") == "active":
                jobs.append((a, pid, port))
    with ThreadPoolExecutor(max_workers=12) as ex:
        health = dict(zip([(a.name, pid) for a, pid, _ in jobs],
                          ex.map(lambda j: _health(*j), jobs), strict=True))

    gpus = gpus_with_users(unit_to_app)
    gpu_by_app: dict[str, list[int]] = {}
    for g in gpus:
        for u in g["users"]:
            gpu_by_app.setdefault(u["name"], []).append(g["index"])

    out_apps = []
    for name, a in sorted(apps.items()):
        procs = []
        for pid, proc in a.processes.items():
            port = reg.get(name, pid) or (proc.port if isinstance(proc.port, int) else None)
            procs.append({"id": pid, "unit": a.unit(pid), "port": port, "command": proc.command,
                          "runtime": proc.runtime.kind, **_unit_summary(props.get(a.unit(pid), {})),
                          "health": health.get((name, pid))})
        states = {p["active"] for p in procs}
        state = ("running" if states == {"active"} else "failed" if "failed" in states
                 else "starting" if states & {"activating", "reloading"} else "stopped")
        if state == "running" and any(p["health"] and not p["health"]["ok"] for p in procs):
            state = "unhealthy"
        out_apps.append({
            "name": name, "description": a.description, "domains": a.domains, "state": state,
            "user": a.user or cfg.run.default_user, "workdir": a.workdir, "processes": procs,
            "gpus": sorted(set(gpu_by_app.get(name, []))), "sandbox": a.sandbox.level,
            "legacy": a.legacy.model_dump() if a.legacy else None, "multi": a.multi,
        })
    observed = [{"unit": u, **_unit_summary(props.get(u, {}))} for u in cfg.observe.units]
    return {"apps": out_apps, "invalid": errors, "observed": observed, "gpus": gpus, "system": system_info(),
            "ui_domain": cfg.ui.domain, "time": int(time.time())}


def ports_view(cfg: Config) -> dict:
    reg = open_registry(cfg)
    listening = listening_ports()
    assigned = [{"port": a.port, "app": a.app, "process": a.process, "pinned": a.pinned,
                 "listening": a.port in listening} for a in reg.all()]
    # manifests' pinned ports too (covers apps whose ports aren't in the registry yet, or an unreadable registry)
    have = {(x["app"], x["process"]) for x in assigned}
    apps, _ = load_all(Path(cfg.paths.apps_dir))
    for a in apps.values():
        for pid, proc in a.processes.items():
            if isinstance(proc.port, int) and (a.name, pid) not in have:
                assigned.append({"port": proc.port, "app": a.name, "process": pid, "pinned": True,
                                 "listening": proc.port in listening})
    known = {a["port"] for a in assigned}
    lo, hi = cfg.ports.range
    other = [{"port": p, "who": who or "?", "in_range": lo <= p <= hi, "reserved": p in cfg.ports.reserved}
             for p, who in sorted(listening.items()) if p not in known]
    return {"range": [lo, hi], "reserved": cfg.ports.reserved, "assigned": assigned, "other": other}
