#!/usr/bin/env python3
"""Paths that differ between machines, read from config.toml at the repository root."""
import os
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_file = Path(os.environ.get("KASHSHAF_CONFIG", ROOT / "config.toml"))
_config = tomllib.loads(_file.read_text(encoding="utf-8")) if _file.exists() else {}


def _path(section: str, key: str, default: str = "") -> Path | None:
    value = _config.get(section, {}).get(key, "") or default
    return ROOT / value if value else None


DATA = _path("paths", "data", "data")
METADATA_DIR = _path("paths", "metadata", "metadata")
METADATA = METADATA_DIR / "metadata.xlsx"

SHAMELA_APP = _path("sources", "shamela")
AHLBAYT_APP = _path("sources", "ahlbayt")
