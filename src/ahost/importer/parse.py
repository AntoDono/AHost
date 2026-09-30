"""Small parsers for legacy systemd units and nginx site files (enough for adoption, not a full grammar)."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field


# ---------------------------------------------------------------- systemd
@dataclass
class Unit:
    name: str
    sections: dict[str, list[tuple[str, str]]] = field(default_factory=dict)

    def all(self, section: str, key: str) -> list[str]:
        return [v for k, v in self.sections.get(section, []) if k == key]

    def get(self, section: str, key: str) -> str | None:
        vals = [v for v in self.all(section, key)]
        # systemd: an empty assignment resets list settings; for scalars the last one wins
        return vals[-1] if vals else None

    def keys(self, section: str) -> set[str]:
        return {k for k, _ in self.sections.get(section, [])}


def parse_unit(text: str, name: str = "") -> Unit:
    u = Unit(name)
    section = ""
    buf = ""
    for raw in text.splitlines():
        line = raw.rstrip()
        if buf:
            line = buf + " " + line.lstrip()
            buf = ""
        if line.endswith("\\"):
            buf = line[:-1].rstrip()
            continue
        s = line.strip()
        if not s or s.startswith(("#", ";")):
            continue
        if s.startswith("[") and s.endswith("]"):
            section = s[1:-1]
            u.sections.setdefault(section, [])
            continue
        if "=" in s:
            k, v = s.split("=", 1)
            u.sections.setdefault(section, []).append((k.strip(), v.strip()))
    return u


def parse_environment(values: list[str]) -> dict[str, str]:
    """Environment= lines -> dict. Handles `A=b`, `"A=b c"`, and several assignments per line."""
    env: dict[str, str] = {}
    for v in values:
        if v == "":
            env.clear()
            continue
        for tok in shlex.split(v):
            if "=" in tok:
                k, val = tok.split("=", 1)
                env[k] = val
    return env


def seconds(v: str | None) -> int | None:
    """systemd time span ('5', '5s', '2min', '1h 30s', 'infinity') -> seconds."""
    if v is None:
        return None
    if v.strip() == "infinity":
        return 0
    total = 0.0
    for num, unit in re.findall(r"(\d+(?:\.\d+)?)\s*([a-z]*)", v):
        mult = {"": 1, "s": 1, "sec": 1, "min": 60, "m": 60, "h": 3600, "hr": 3600, "ms": 0.001, "d": 86400}
        total += float(num) * mult.get(unit, 1)
    return int(total)


# ---------------------------------------------------------------- nginx
@dataclass
class Directive:
    name: str
    args: list[str]
    block: list[Directive] | None = None
    comment: str = ""  # trailing comment on the same line (e.g. "managed by Certbot")

    def find(self, name: str) -> list[Directive]:
        return [d for d in (self.block or []) if d.name == name]

    def first(self, name: str) -> Directive | None:
        found = self.find(name)
        return found[0] if found else None


_TOKEN = re.compile(r"""\s*(?:(#[^\n]*)|("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')|([{};])|([^\s{};"'#]+))""")


def _tokens(text: str):
    pos = 0
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m or m.end() == pos:
            if text[pos:].strip() == "":
                return
            raise ValueError(f"nginx parse error near: {text[pos:pos + 40]!r}")
        pos = m.end()
        comment, quoted, punct, word = m.groups()
        if comment is not None:
            yield ("comment", comment[1:].strip())
        elif quoted is not None:
            yield ("word", quoted)
        elif punct is not None:
            yield (punct, punct)
        elif word is not None:
            yield ("word", word)


def parse_nginx(text: str) -> list[Directive]:
    stack: list[list[Directive]] = [[]]
    cur: list[str] = []
    last: Directive | None = None
    for kind, val in _tokens(text):
        if kind == "comment":
            if last is not None and not cur:
                last.comment = (last.comment + " " + val).strip()
            continue
        if kind == "word":
            cur.append(val)
        elif kind == ";":
            if not cur:
                continue
            last = Directive(cur[0], cur[1:])
            stack[-1].append(last)
            cur = []
        elif kind == "{":
            d = Directive(cur[0] if cur else "", cur[1:], [])
            stack[-1].append(d)
            stack.append(d.block)
            cur, last = [], None
        elif kind == "}":
            if len(stack) == 1:
                raise ValueError("nginx parse error: unbalanced '}'")
            stack.pop()
            last = stack[-1][-1] if stack[-1] else None
    if len(stack) != 1:
        raise ValueError("nginx parse error: missing '}'")
    return stack[0]


def unquote(s: str) -> str:
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    return s


def render_directive(d: Directive, indent: int = 0) -> str:
    pad = "    " * indent
    head = " ".join([d.name, *d.args]).strip()
    if d.block is None:
        return f"{pad}{head};"
    inner = "\n".join(render_directive(c, indent + 1) for c in d.block)
    return f"{pad}{head} {{\n{inner}\n{pad}}}" if inner else f"{pad}{head} {{ }}"
