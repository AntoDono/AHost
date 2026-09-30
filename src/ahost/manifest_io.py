"""Read and write manifests. Writing uses tomlkit so hand-written comments survive UI edits."""

from __future__ import annotations

import tomllib
from pathlib import Path

import tomlkit

from .models import App


def load_app(path: Path) -> App:
    with path.open("rb") as f:
        data = tomllib.load(f)
    app = App.model_validate(data)
    if app.name != path.stem:
        raise ValueError(f"{path}: name {app.name!r} must match the file name {path.stem!r}")
    return app


def load_all(apps_dir: Path) -> tuple[dict[str, App], dict[str, str]]:
    """All manifests in apps_dir. Returns (apps, errors) so one broken file doesn't hide the rest."""
    apps: dict[str, App] = {}
    errors: dict[str, str] = {}
    for p in sorted(apps_dir.glob("*.toml")):
        if p.name.startswith(("_", ".")):
            continue
        try:
            apps[p.stem] = load_app(p)
        except Exception as e:  # noqa: BLE001 - reported to the user
            errors[p.stem] = str(e)
    return apps, errors


_INLINE = {"runtime", "env", "certs", "headers", "systemd"}


def _to_toml(value, key: str | None = None):
    if isinstance(value, dict):
        if key in _INLINE:
            t = tomlkit.inline_table()
            for k, v in value.items():
                t[k] = _to_toml(v)
            return t
        t = tomlkit.table()
        for k, v in value.items():
            t[k] = _to_toml(v, k)
        return t
    if isinstance(value, list) and value and all(isinstance(x, dict) for x in value):
        aot = tomlkit.aot()
        for x in value:
            aot.append(_to_toml(x))
        return aot
    if isinstance(value, str) and "\n" in value:
        return tomlkit.string(value, multiline=True)
    return value


ORDER = ["name", "description", "workdir", "domains", "user", "group", "env_file", "env", "after", "wants",
         "enabled", "certs", "legacy", "processes", "routes", "proxy", "sandbox"]


def dumps(data: dict, header: str = "") -> str:
    doc = tomlkit.document()
    if header:
        for line in header.strip().splitlines():
            doc.add(tomlkit.comment(line))
        doc.add(tomlkit.nl())
    for k in sorted(data, key=lambda k: ORDER.index(k) if k in ORDER else 99):
        doc[k] = _to_toml(data[k], k)
    return tomlkit.dumps(doc)
