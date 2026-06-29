"""Tiny .env loader (no external dependency).

Reads a simple `KEY=VALUE` file and puts the values into the process
environment, so users can keep their settings in a `.env` file instead of
typing `export ...` every time. Existing environment variables win, so you
can still override a value on the command line.
"""

from __future__ import annotations

import os


def load_env_file(path: str) -> None:
    """Load KEY=VALUE lines from `path` into os.environ (if not already set).

    * Blank lines and lines starting with `#` are ignored.
    * Surrounding single/double quotes around the value are stripped.
    * A missing file is silently ignored (env vars may be set another way).
    """
    if not path or not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def load_default_env() -> None:
    """Load a `.env` file sitting next to the scripts, if present."""
    here = os.path.dirname(os.path.abspath(__file__))
    load_env_file(os.path.join(here, ".env"))
