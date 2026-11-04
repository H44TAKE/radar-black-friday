"""Orquestração: tarefas por frequência, tratamento de falhas por fonte (com backoff), avaliação e
envio das notificações."""
from __future__ import annotations

import contextlib
import copy
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import requests

from .classify import Match, match_subjects
from .collectors import Collector, build_collectors
from .community import Ctx, post_to_offer
from .db import DB
from .engine import Decision, evaluate
from .http import Blocked, Fetcher, RobotsDisallow
from .models import Offer
from .notify import Notifier, build_message, store_name
from .sources import bot_updates, fetch_channel, instagram_posts
from .util import brl

TIER_DEFAULTS = {"priority": 10, "general": 30, "community": 5}


class Run:
    """Estado de uma execução (contadores para o log/painel)."""

    def __init__(self):
        self.alerts = 0
        self.checked = 0
        self.errors: list[str] = []
        self.seen_keys: set[str] = set()


class App:
    def __init__(self, cfg: dict, db: DB, fetcher: Fetcher, notifier: Notifier, collectors: Optional[dict] = None):
        self.cfg, self.db, self.f, self.n = cfg, db, fetcher, notifier
        self.collectors = collectors or build_collectors(fetcher, cfg)
        self.trusted = set(cfg.get("trusted_domains") or [])
        self.run = Run()
        self.tz = ZoneInfo((cfg.get("settings") or {}).get("timezone", "America/Sao_Paulo"))

    # ------------------------------------------------------------------ fontes
    def _backoff_skip(self, source: str) -> bool:
        row = self.db.source_get(source)
        if not row or row["ok"] or row["status"] not in ("bloqueado", "erro", "robots"):
            return False
        wait = min(15 * (2 ** min(row["fail_streak"] - 1, 4)), 240)  # 15, 30, 60, 120, 240 min
        age = datetime.now(timezone.utc) - datetime.fromisoformat(row["ts"])
        return age < timedelta(minutes=wait)

    @contextlib.contextmanager
    def source(self, name: str):
        """Registra sucesso/falha da fonte. Bloqueios NÃO são contornados: viram status + backoff."""
        box = {"items": 0, "skip": False}
        if self._backoff_skip(name):
            box["skip"] = True
            yield box
            return
        try:
            yield box
            self.db.source_report(name, True, "ok", box["items"])
        except Blocked as e:
            self.db.source_report(name, False, "bloqueado", box["items"], e.reason)
            print(f"[fonte {name}] bloqueada: {e.reason}")
        except RobotsDisallow as e:
            self.db.source_report(name, False, "robots", box["items"], str(e))
            print(f"[fonte {name}] robots.txt: {e}")
        except Exception as e:  # noqa: BLE001
            self.db.source_report(name, False, "erro", box["items"], f"{type(e).__name__}: {e}")
            self.run.errors.append(f"{name}: {e}")
            print(f"[fonte {name}] erro: {type(e).__name__}: {e}")

    # ------------------------------------------------------------------ ofertas
    def _verify(self, o: Offer) -> Optional[Offer]:
        coll = self.collectors.get(o.store)
        if coll is None or not coll.enabled():
            return None
        try:
            return coll.fetch_product(o.url)
        except (Blocked, RobotsDisallow):
            return None
        except Exception as e:  # noqa: BLE001
            print(f"[verificação] {o.url}: {e}")
            return None

    def handle_offer(self, o: Offer) -> int:
        """Classifica, guarda histórico, verifica na página (se necessário) e notifica. Retorna nº de alertas."""
        if o.key in self.run.seen_keys:
            return 0
        self.run.seen_keys.add(o.key)
        self.run.checked += 1
        matches = match_subjects(o.title, o.description, self.cfg)
        if not matches or not o.price:
            return 0
        self.db.upsert_offer(o, matches)

        if not o.verified or any(m.confidence != "confirmed" for m in matches):
            # pré-checagem barata: se nem com tudo "confirmado" passaria, não gasta requisição
            probe = copy.copy(o)
            probe.verified = True
            probe.in_stock = True if o.in_stock is None else o.in_stock
            promising = False
            for m in matches:
                pm = copy.copy(m)
                pm.confidence = "confirmed"
                if evaluate(self.db, probe, pm, self.cfg).notify:
                    promising = True
                    break
            if not promising:
                return 0
            ver = self._verify(o)
            if ver is None:
                return 0
            ver.origin, ver.origin_label, ver.origin_url = o.origin, o.origin_label, o.origin_url
            ver.announced_price, ver.coupon = o.announced_price, o.coupon or ver.coupon
            ver.seller = ver.seller or o.seller
            ver.seller_ok = o.seller_ok if ver.seller_ok is None else ver.seller_ok
            ver.official_store = ver.official_store or o.official_store
            ver.free_shipping = ver.free_shipping if ver.free_shipping is not None else o.free_shipping
            o = ver
            matches = match_subjects(o.title, o.description, self.cfg)
            if not matches:
                return 0
            self.db.upsert_offer(o, matches)
        return self._decide_and_notify(o, matches)

    def _decide_and_notify(self, o: Offer, matches: list[Match]) -> int:
        decisions = [(m, evaluate(self.db, o, m, self.cfg)) for m in matches]
        good = [(m, d) for m, d in decisions if d.notify]
        if not good:
            return 0
        base = good[0][1]
        merged = Decision(True, base.subject, "; ".join(m.name for m, _ in good), reasons=[], flags=[], notes=[],
                          reference_price=base.reference_price, reference_label=base.reference_label,
                          discount_pct=base.discount_pct, discount_confirmed=base.discount_confirmed,
                          history_n=base.history_n, other=base.other, sig=base.sig)
        for _, d in good:
            for field_name in ("reasons", "flags", "notes"):
                for item in getattr(d, field_name):
                    if item not in getattr(merged, field_name):
                        getattr(merged, field_name).append(item)
        msg = build_message(o, merged)
        sent = self.n.send(msg, url=o.url, button=f"Ver na {store_name(o.store)}")
        for m, d in good:
            self.db.log_alert(o.key, m.subject, o.price, "; ".join(d.reasons), o.origin_label or "loja", d.sig, sent, msg)
        self.db.c.commit()
        if sent:
            self.run.alerts += 1
        return int(sent)

    # ------------------------------------------------------------------ tarefas
    def _stores(self) -> list[Collector]:
        return [c for c in self.collectors.values() if c.enabled()]

    def job_priority(self):
        for p in [x for x in self.cfg["products"] if x.get("priority")]:
            for store, url in (p.get("urls") or {}).items():
                coll = self.collectors.get(store)
                if not coll or not coll.enabled():
                    continue
                with self.source(f"loja:{store}:produto") as box:
                    if box["skip"]:
                        continue
                    o = coll.fetch_product(url)
                    if o is None:
                        raise RuntimeError(f"não consegui ler preço em {url} (layout mudou ou produto removido)")
                    box["items"] += 1
                    self.handle_offer(o)
            self._search_all(p.get("search_terms", [])[:2])

    def job_general(self):
        for c in self.cfg["categories"]:
            self._search_all(c.get("search_terms", []))

    def _search_all(self, terms: list[str]):
        for coll in self._stores():
            with self.source(f"loja:{coll.name}") as box:
                if box["skip"]:
                    continue
                for term in terms:
                    results = coll.search(term, limit=int((self.cfg.get("settings") or {}).get("results_per_search", 30)))
                    box["items"] += len(results)
                    for o in results:
                        self.handle_offer(o)

    def job_community(self):
        st = self.cfg.get("settings") or {}
        max_age = float((st.get("alerts") or {}).get("community_max_age_hours", 8))
        tg = self.cfg.get("telegram") or {}
        ctx = Ctx(self.db, self.f, self.collectors, self.cfg, self.trusted)
        posts = []
        for ch in tg.get("channels") or []:
            handle = ch["handle"] if isinstance(ch, dict) else ch
            extra = tuple((ch.get("redirect_hosts") or []) if isinstance(ch, dict) else [])
            with self.source(f"telegram:{handle}") as box:
                if box["skip"]:
                    continue
                got = fetch_channel(self.f, handle, extra, respect_robots=bool(tg.get("respect_robots", True)))
                box["items"] = len(got)
                posts += got
        tok = os.environ.get("TELEGRAM_BOT_TOKEN")
        if tok and tg.get("bot_groups", True):
            with self.source("telegram:grupos-do-bot") as box:
                if not box["skip"]:
                    got = bot_updates(self.db, tok, tg.get("groups_allow"), os.environ.get("TELEGRAM_CHAT_ID"))
                    box["items"] = len(got)
                    posts += got
        ig = self.cfg.get("instagram") or {}
        profiles = [p if isinstance(p, str) else p["username"] for p in ig.get("profiles") or []]
        if profiles and ig.get("enabled", "auto") != False and os.environ.get("IG_ACCESS_TOKEN"):  # noqa: E712
            with self.source("instagram") as box:
                if not box["skip"]:
                    got = instagram_posts(self.f, profiles)
                    box["items"] = len(got)
                    posts += got
        now = datetime.now(timezone.utc)
        for post in posts:
            if self.db.post_seen(post.uid):
                continue
            age_h = (now - post.ts).total_seconds() / 3600
            if age_h > max_age:
                self.db.post_log(post.uid, post.source, post.ts.isoformat(), "antigo", f"{age_h:.0f}h", post.text)
                continue
            try:
                offer, status = post_to_offer(post, ctx)
            except Exception as e:  # noqa: BLE001
                self.db.post_log(post.uid, post.source, post.ts.isoformat(), "erro", str(e), post.text)
                continue
            if offer is None:
                self.db.post_log(post.uid, post.source, post.ts.isoformat(), "descartado" if status != "irrelevante" else "irrelevante",
                                 status, post.text)
                continue
            sent = self.handle_offer(offer)
            self.db.post_log(post.uid, post.source, post.ts.isoformat(), "alertado" if sent else "avaliado",
                             f"{offer.title[:80]} @ {brl(offer.price)}", post.text)
        self.db.c.commit()

    # ------------------------------------------------------------------ agenda
    def due(self, tier: str) -> bool:
        iv = ((self.cfg.get("settings") or {}).get("intervals_minutes") or {}).get(tier, TIER_DEFAULTS[tier])
        last = self.db.job_last(f"tier:{tier}")
        return last is None or datetime.now(timezone.utc) - last >= timedelta(minutes=iv - 1)

    def run_tiers(self, tiers: list[str], force: bool = False):
        jobs = {"priority": self.job_priority, "general": self.job_general, "community": self.job_community}
        for t in tiers:
            if force or self.due(t):
                started = time.time()
                try:
                    jobs[t]()
                finally:
                    self.db.job_done(f"tier:{t}")
                    self.db.c.commit()
                print(f"[tier {t}] concluído em {time.time() - started:.1f}s")
            else:
                print(f"[tier {t}] ainda não é a hora")
        self.heartbeat()
        hc = os.environ.get("HC_PING_URL")
        if hc:
            with contextlib.suppress(requests.RequestException):
                requests.get(hc, timeout=10)

    # ------------------------------------------------------------------ status
    def heartbeat(self):
        hour = (self.cfg.get("settings") or {}).get("heartbeat_hour", 9)
        now = datetime.now(self.tz)
        today = now.strftime("%Y-%m-%d")
        if now.hour < hour or self.db.kv_get("heartbeat_day") == today:
            return
        rows = self.db.c.execute("SELECT * FROM source_status ORDER BY source").fetchall()
        bad = [r for r in rows if not r["ok"]]
        n_list = self.db.c.execute("SELECT COUNT(*) n FROM listings").fetchone()["n"]
        n_alert = self.db.c.execute("SELECT COUNT(*) n FROM alerts WHERE sent=1 AND ts>=?",
                                    ((datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),)).fetchone()["n"]
        lines = ["🟢 <b>Radar Black Friday — bom dia!</b>", "", f"Fontes ok: {len(rows) - len(bad)}/{len(rows)} · anúncios acompanhados: {n_list} · alertas nas últimas 24h: {n_alert}"]
        if bad:
            lines.append("")
            lines.append("⚠️ <b>Fontes com problema:</b>")
            for r in bad[:10]:
                lines.append(f"• {r['source']}: {r['status']} ({(r['error'] or '')[:70]}) — há {r['fail_streak']} tentativa(s)")
        if self.n.send("\n".join(lines)):
            self.db.kv_set("heartbeat_day", today)
        self.db.c.commit()
