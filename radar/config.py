from __future__ import annotations

import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def load_config(path: str | None = None) -> dict:
    p = Path(path or os.environ.get("RADAR_CONFIG") or ROOT / "config" / "watchlist.yml")
    cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    cfg.setdefault("settings", {})
    cfg.setdefault("products", [])
    cfg.setdefault("categories", [])
    cfg.setdefault("stores", {})
    cfg.setdefault("telegram", {"channels": [], "bot_groups": True})
    cfg.setdefault("instagram", {"enabled": "auto", "profiles": []})
    cfg.setdefault("trusted_domains", [])
    return cfg


def subjects(cfg: dict) -> list[dict]:
    return [dict(s, _kind="product") for s in cfg["products"]] + [dict(s, _kind="category") for s in cfg["categories"]]
