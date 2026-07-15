"""Per-user configuration and secrets from the XDG config directory."""

from __future__ import annotations

import os
import tomllib
from functools import cache
from pathlib import Path

from dotenv import load_dotenv

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "shipshape"
CONFIG_FILE = CONFIG_DIR / "config.toml"
ENV_FILE = CONFIG_DIR / ".env"
USER_ID_FILE = CONFIG_DIR / "notion_user_id"


@cache
def _config() -> dict:
    if not CONFIG_FILE.exists():
        raise SystemExit(
            f"Missing {CONFIG_FILE}.\nCopy config.example.toml there and set your Notion data source ID.",
        )
    return tomllib.loads(CONFIG_FILE.read_text())


def notion_data_source_id() -> str:
    return _config()["notion"]["data_source_id"]


def notion_token() -> str:
    load_dotenv(ENV_FILE)
    if "NOTION_TOKEN" not in os.environ:
        raise SystemExit(f"NOTION_TOKEN not set. Export it, or add it to {ENV_FILE}.")
    return os.environ["NOTION_TOKEN"]


def notion_user_id() -> str:
    if not USER_ID_FILE.exists():
        raise SystemExit("Notion user ID not set. Run:\n  update-tickets --setup")
    return USER_ID_FILE.read_text().strip()
