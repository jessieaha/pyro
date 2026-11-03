"""Minimal KEY=VALUE env-file loader and ${VAR} expansion (no third-party deps)."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path

_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class MissingVariable(KeyError):
    pass


def load_env_file(path: str | os.PathLike, environ: dict[str, str] | None = None) -> list[str]:
    """Load ``KEY=VALUE`` lines into ``environ`` without overriding existing keys.

    Returns the keys that were set. Values are never logged.
    """
    environ = os.environ if environ is None else environ
    set_keys: list[str] = []
    p = Path(path)
    if not p.is_file():
        return set_keys
    for raw in p.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key not in environ:
            environ[key] = value
            set_keys.append(key)
    return set_keys


def expand(value: str, environ: Mapping[str, str] | None = None) -> str:
    """Expand ``${VAR}`` and ``${VAR:-default}``; raise MissingVariable if unset."""
    environ = os.environ if environ is None else environ

    def repl(m: re.Match[str]) -> str:
        name, default = m.group(1), m.group(2)
        if name in environ and environ[name] != "":
            return environ[name]
        if default is not None:
            return default
        raise MissingVariable(name)

    return _VAR.sub(repl, value)
