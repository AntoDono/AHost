"""GPU inventory. Cards are identified by UUID because nvidia-smi index, /dev/nvidiaN minor and CUDA's default
order can all differ on multi-GPU machines."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Gpu:
    index: int  # nvidia-smi index
    uuid: str
    name: str
    bus_id: str  # normalized "0000:01:00.0"
    minor: int | None  # /dev/nvidia<minor>
    memory_total_mib: int


def _norm_bus(bus: str) -> str:
    # nvidia-smi: "00000000:01:00.0"; /proc: "0000:01:00.0"
    dom, rest = bus.lower().split(":", 1)
    return f"{int(dom, 16):04x}:{rest}"


def minors_by_bus(proc_dir: Path = Path("/proc/driver/nvidia/gpus")) -> dict[str, int]:
    out: dict[str, int] = {}
    if not proc_dir.exists():
        return out
    for info in proc_dir.glob("*/information"):
        bus = minor = None
        for line in info.read_text().splitlines():
            k, _, v = line.partition(":")
            if k.strip() == "Bus Location":
                bus = _norm_bus(v.strip())
            elif k.strip() == "Device Minor":
                minor = int(v.strip())
        if bus is not None and minor is not None:
            out[bus] = minor
    return out


def inventory() -> list[Gpu]:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,uuid,name,pci.bus_id,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, check=True, timeout=10,
        ).stdout
    except (FileNotFoundError, subprocess.SubprocessError):
        return []
    minors = minors_by_bus()
    gpus = []
    for line in out.strip().splitlines():
        idx, uuid, name, bus, mem = [c.strip() for c in line.split(",")]
        nb = _norm_bus(bus)
        gpus.append(Gpu(int(idx), uuid, name, nb, minors.get(nb), int(float(mem))))
    return gpus


def minor_map(gpus: list[Gpu] | None = None) -> dict[str, int]:
    """UUID -> /dev/nvidia minor."""
    return {g.uuid: g.minor for g in (gpus if gpus is not None else inventory()) if g.minor is not None}
