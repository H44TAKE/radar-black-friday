"""Exporta o estado para dashboard/data.json (consumido pelo painel estático)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .db import DB
from .models import now_iso
from .notify import store_name


def _series(db: DB, key: str, days: int = 30, max_points: int = 60) -> list:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = db.c.execute("SELECT ts, price FROM prices WHERE key=? AND ts>=? AND price IS NOT NULL ORDER BY ts", (key, since)).fetchall()
    pts = [[r["ts"], r["price"]] for r in rows]
    if len(pts) > max_points:
        step = len(pts) / max_points
        pts = [pts[int(i * step)] for i in range(max_points)] + [pts[-1]]
    return pts


def export(db: DB, cfg: dict, out_path: str | Path) -> dict:
    fresh = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    subjects = []
    for kind, items in (("product", cfg.get("products", [])), ("category", cfg.get("categories", []))):
        for s in items:
            rows = db.c.execute(
                """SELECT l.*, m.confidence FROM listings l JOIN matches m ON m.key=l.key
                   WHERE m.subject=? AND l.last_seen>=? ORDER BY (l.in_stock=1) DESC, l.price ASC LIMIT 12""",
                (s["id"], fresh)).fetchall()
            listings = []
            for r in rows:
                mn = db.c.execute("SELECT MIN(price) p FROM prices WHERE key=?", (r["key"],)).fetchone()["p"]
                cap = s.get("max_price")
                listings.append({
                    "key": r["key"], "store": store_name(r["store"]), "title": r["title"], "url": r["url"], "price": r["price"],
                    "old_price": r["old_price"], "in_stock": None if r["in_stock"] is None else bool(r["in_stock"]),
                    "min_price": mn, "last_seen": r["last_seen"], "verified": bool(r["verified"]),
                    "confidence": r["confidence"], "hit_target": bool(cap and r["price"] and r["price"] <= cap and r["in_stock"] == 1),
                    "series": _series(db, r["key"]),
                })
            avail = [x for x in listings if x["in_stock"]]
            subjects.append({"id": s["id"], "name": s["name"], "kind": kind, "max_price": s.get("max_price"),
                             "priority": bool(s.get("priority")), "listings": listings,
                             "best_price": min((x["price"] for x in avail), default=None)})
    alerts = [dict(r) for r in db.c.execute(
        """SELECT a.ts, a.subject, a.price, a.reason, a.origin, l.title, l.url, l.store FROM alerts a
           LEFT JOIN listings l ON l.key=a.key WHERE a.sent=1 ORDER BY a.ts DESC LIMIT 40""")]
    for a in alerts:
        a["store"] = store_name(a["store"]) if a.get("store") else None
    sources = [dict(r) for r in db.c.execute("SELECT source, ok, status, items, error, ts, last_ok, fail_streak FROM source_status ORDER BY source")]
    community = [dict(r) for r in db.c.execute(
        "SELECT source, ts, status, note FROM posts WHERE status NOT IN ('antigo','irrelevante') ORDER BY seen_at DESC LIMIT 40")]
    jobs = {r["name"]: r["last_run"] for r in db.c.execute("SELECT name, last_run FROM jobs")}
    data = {"generated_at": now_iso(), "jobs": jobs, "sources": sources, "subjects": subjects, "alerts": alerts, "community": community,
            "counts": {"listings": db.c.execute("SELECT COUNT(*) n FROM listings").fetchone()["n"],
                       "alerts_24h": db.c.execute("SELECT COUNT(*) n FROM alerts WHERE sent=1 AND ts>=?",
                                                  ((datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),)).fetchone()["n"]}}
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data
