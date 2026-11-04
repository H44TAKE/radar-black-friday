"""Persistência em SQLite (arquivo único, versionado no branch `data` do repositório)."""
from __future__ import annotations

import json
import sqlite3
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from .models import Offer, now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings(
  key TEXT PRIMARY KEY, store TEXT, url TEXT, title TEXT, condition TEXT, seller TEXT,
  first_seen TEXT, last_seen TEXT, price REAL, old_price REAL, in_stock INTEGER, shipping REAL,
  coupon TEXT, rating REAL, reviews INTEGER, verified INTEGER, extra TEXT);
CREATE TABLE IF NOT EXISTS matches(key TEXT, subject TEXT, confidence TEXT, PRIMARY KEY(key, subject));
CREATE TABLE IF NOT EXISTS prices(key TEXT, ts TEXT, price REAL, old_price REAL, in_stock INTEGER);
CREATE INDEX IF NOT EXISTS idx_prices_key ON prices(key, ts);
CREATE TABLE IF NOT EXISTS alerts(
  id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT, subject TEXT, ts TEXT, price REAL, reason TEXT,
  origin TEXT, sig TEXT, sent INTEGER, message TEXT);
CREATE TABLE IF NOT EXISTS source_status(
  source TEXT PRIMARY KEY, ts TEXT, ok INTEGER, status TEXT, items INTEGER, error TEXT,
  fail_streak INTEGER DEFAULT 0, last_ok TEXT);
CREATE TABLE IF NOT EXISTS jobs(name TEXT PRIMARY KEY, last_run TEXT);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS posts(id TEXT PRIMARY KEY, source TEXT, ts TEXT, seen_at TEXT, status TEXT, note TEXT, text TEXT);
"""


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


class DB:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.c = sqlite3.connect(self.path)
        self.c.row_factory = sqlite3.Row
        self.c.executescript(SCHEMA)

    def close(self):
        self.c.commit()
        self.c.close()

    # ---- kv / jobs ----------------------------------------------------
    def kv_get(self, k: str, default: Optional[str] = None) -> Optional[str]:
        r = self.c.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return r["v"] if r else default

    def kv_set(self, k: str, v: str):
        self.c.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))

    def job_last(self, name: str) -> Optional[datetime]:
        r = self.c.execute("SELECT last_run FROM jobs WHERE name=?", (name,)).fetchone()
        return _dt(r["last_run"]) if r else None

    def job_done(self, name: str):
        self.c.execute("INSERT INTO jobs(name,last_run) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET last_run=excluded.last_run",
                       (name, now_iso()))

    # ---- listings / preços -------------------------------------------
    def upsert_offer(self, o: Offer, matches: list) -> None:
        now = now_iso()
        key = o.key
        prev = self.c.execute("SELECT price, in_stock FROM listings WHERE key=?", (key,)).fetchone()
        extra = json.dumps({"origin": o.origin, "origin_label": o.origin_label, "official": o.official_store,
                            "seller_ok": o.seller_ok, "free_shipping": o.free_shipping, "image": o.image}, ensure_ascii=False)
        stock = None if o.in_stock is None else int(o.in_stock)
        self.c.execute(
            """INSERT INTO listings(key,store,url,title,condition,seller,first_seen,last_seen,price,old_price,in_stock,
               shipping,coupon,rating,reviews,verified,extra) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(key) DO UPDATE SET title=excluded.title, last_seen=excluded.last_seen, price=excluded.price,
               old_price=excluded.old_price, in_stock=excluded.in_stock, shipping=excluded.shipping, coupon=excluded.coupon,
               rating=COALESCE(excluded.rating, listings.rating), reviews=COALESCE(excluded.reviews, listings.reviews),
               verified=excluded.verified, extra=excluded.extra, condition=excluded.condition, seller=excluded.seller""",
            (key, o.store, o.url, o.title, o.condition, o.seller, now, now, o.price, o.old_price, stock, o.shipping,
             o.coupon, o.rating, o.reviews, int(o.verified), extra))
        for m in matches:
            self.c.execute("INSERT INTO matches(key,subject,confidence) VALUES(?,?,?) ON CONFLICT(key,subject) DO UPDATE SET confidence=excluded.confidence",
                           (key, m.subject, m.confidence))
        # amostra de preço: mudou, ou última amostra tem > 3h
        last = self.c.execute("SELECT ts, price, in_stock FROM prices WHERE key=? ORDER BY ts DESC LIMIT 1", (key,)).fetchone()
        changed = (not last) or last["price"] != o.price or last["in_stock"] != stock
        stale = last and (datetime.now(timezone.utc) - _dt(last["ts"])) > timedelta(hours=3)
        if o.price and (changed or stale):
            self.c.execute("INSERT INTO prices(key,ts,price,old_price,in_stock) VALUES(?,?,?,?,?)",
                           (key, now, o.price, o.old_price, stock))

    def history_stats(self, key: str, window_days: int = 30) -> dict:
        since = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
        rows = self.c.execute("SELECT ts, price FROM prices WHERE key=? AND ts>=? AND price IS NOT NULL ORDER BY ts", (key, since)).fetchall()
        allrows = self.c.execute("SELECT MIN(price) mn, COUNT(*) n FROM prices WHERE key=?", (key,)).fetchone()
        out = {"n": len(rows), "median": None, "min": allrows["mn"], "span_days": 0.0, "max": None, "first_price": None}
        if rows:
            vals = [r["price"] for r in rows]
            out["median"] = statistics.median(vals)
            out["max"] = max(vals)
            out["first_price"] = vals[0]
            out["span_days"] = (_dt(rows[-1]["ts"]) - _dt(rows[0]["ts"])).total_seconds() / 86400
        return out

    def recent_jump(self, key: str, days: int = 21) -> Optional[float]:
        """Variação % do menor preço dos 60 dias anteriores até o maior preço dos últimos `days` dias (aumento artificial)."""
        now = datetime.now(timezone.utc)
        recent_since = (now - timedelta(days=days)).isoformat()
        old_since = (now - timedelta(days=60)).isoformat()
        a = self.c.execute("SELECT MIN(price) p FROM prices WHERE key=? AND ts>=? AND ts<?", (key, old_since, recent_since)).fetchone()
        b = self.c.execute("SELECT MAX(price) p FROM prices WHERE key=? AND ts>=?", (key, recent_since)).fetchone()
        if a and b and a["p"] and b["p"] and a["p"] > 0:
            return (b["p"] / a["p"] - 1) * 100
        return None

    def best_other(self, subject: str, exclude_key: str, hours: int = 24) -> Optional[sqlite3.Row]:
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        return self.c.execute(
            """SELECT l.* FROM listings l JOIN matches m ON m.key=l.key
               WHERE m.subject=? AND l.key<>? AND l.last_seen>=? AND l.in_stock=1 AND l.price IS NOT NULL
               AND COALESCE(l.condition,'new')='new' ORDER BY l.price ASC LIMIT 1""",
            (subject, exclude_key, since)).fetchone()

    # ---- alertas --------------------------------------------------------
    def last_alert(self, key: str, subject: str) -> Optional[sqlite3.Row]:
        return self.c.execute("SELECT * FROM alerts WHERE key=? AND subject=? AND sent=1 ORDER BY ts DESC LIMIT 1", (key, subject)).fetchone()

    def sig_alerted_recently(self, sig: str, hours: int = 24) -> bool:
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        return self.c.execute("SELECT 1 FROM alerts WHERE sig=? AND ts>=? AND sent=1", (sig, since)).fetchone() is not None

    def went_out_of_stock_since(self, key: str, ts: str) -> bool:
        return self.c.execute("SELECT 1 FROM prices WHERE key=? AND ts>? AND in_stock=0", (key, ts)).fetchone() is not None

    def unverified_alerts_today(self, subject: str) -> int:
        since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        return self.c.execute("SELECT COUNT(*) n FROM alerts WHERE subject=? AND ts>=? AND reason LIKE '%sem histórico%'", (subject, since)).fetchone()["n"]

    def log_alert(self, key, subject, price, reason, origin, sig, sent, message):
        self.c.execute("INSERT INTO alerts(key,subject,ts,price,reason,origin,sig,sent,message) VALUES(?,?,?,?,?,?,?,?,?)",
                       (key, subject, now_iso(), price, reason, origin, sig, int(sent), message))

    # ---- fontes ---------------------------------------------------------
    def source_get(self, source: str) -> Optional[sqlite3.Row]:
        return self.c.execute("SELECT * FROM source_status WHERE source=?", (source,)).fetchone()

    def source_report(self, source: str, ok: bool, status: str, items: int = 0, error: str = ""):
        prev = self.source_get(source)
        streak = 0 if ok else ((prev["fail_streak"] if prev else 0) + 1)
        last_ok = now_iso() if ok else (prev["last_ok"] if prev else None)
        self.c.execute(
            """INSERT INTO source_status(source,ts,ok,status,items,error,fail_streak,last_ok) VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(source) DO UPDATE SET ts=excluded.ts, ok=excluded.ok, status=excluded.status, items=excluded.items,
               error=excluded.error, fail_streak=excluded.fail_streak, last_ok=excluded.last_ok""",
            (source, now_iso(), int(ok), status, items, error[:300], streak, last_ok))

    # ---- posts de comunidades ------------------------------------------
    def post_seen(self, pid: str) -> bool:
        return self.c.execute("SELECT 1 FROM posts WHERE id=?", (pid,)).fetchone() is not None

    def post_log(self, pid, source, ts, status, note="", text=""):
        self.c.execute("INSERT OR REPLACE INTO posts(id,source,ts,seen_at,status,note,text) VALUES(?,?,?,?,?,?,?)",
                       (pid, source, ts, now_iso(), status, note[:300], text[:600]))

    # ---- manutenção -----------------------------------------------------
    def prune(self, days: int = 120):
        cut = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        self.c.execute("DELETE FROM prices WHERE ts<?", (cut,))
        self.c.execute("DELETE FROM posts WHERE seen_at<?", ((datetime.now(timezone.utc) - timedelta(days=10)).isoformat(),))
        self.c.execute("DELETE FROM alerts WHERE ts<?", (cut,))
        self.c.execute("DELETE FROM listings WHERE last_seen<?", (cut,))
        self.c.execute("DELETE FROM matches WHERE key NOT IN (SELECT key FROM listings)")
        self.c.commit()
