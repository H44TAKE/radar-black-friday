"""Mercado Livre. Caminho preferido: API oficial com token de aplicativo (ML_CLIENT_ID / ML_CLIENT_SECRET,
fluxo client_credentials) que traz reputação do vendedor. Sem credenciais: HTML público da listagem,
aceitando só Loja Oficial / MercadoLíder (única reputação visível sem API)."""
from __future__ import annotations

import os
import re
import time
from typing import Optional

from ..classify import detect_condition
from ..extract import listing_cards, parse_product_generic, soup_of
from ..models import Offer
from ..util import norm
from .base import Collector

API = "https://api.mercadolibre.com"
LINK = re.compile(r"(?:produto\.mercadolivre\.com\.br/MLB-?\d+|mercadolivre\.com\.br/[^/]+/p/MLB\d+|/MLB-?\d+)")
GOOD_LEVELS = {"5_green", "4_light_green"}


class MercadoLivre(Collector):
    name = "mercadolivre"
    label = "Mercado Livre"
    domains = ("mercadolivre.com.br", "mercadolibre.com")
    marketplace = True

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._tok: Optional[tuple[str, float]] = None

    # ---- token ------------------------------------------------------------
    def _token(self) -> Optional[str]:
        cid, sec = os.environ.get("ML_CLIENT_ID"), os.environ.get("ML_CLIENT_SECRET")
        if not (cid and sec):
            return None
        if self._tok and self._tok[1] > time.time() + 60:
            return self._tok[0]
        r = self.f.post(f"{API}/oauth/token", data={"grant_type": "client_credentials", "client_id": cid, "client_secret": sec},
                        headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"})
        if r.status_code != 200:
            return None
        j = r.json()
        self._tok = (j["access_token"], time.time() + int(j.get("expires_in", 21000)))
        return self._tok[0]

    def _api_get(self, path: str, **params):
        tok = self._token()
        if not tok:
            return None
        r = self.f.get(f"{API}{path}", params=params, headers={"Authorization": f"Bearer {tok}"}, check_robots=False)
        return r.json() if r.status_code == 200 else None

    @staticmethod
    def _seller_ok(seller: dict, official: bool) -> Optional[bool]:
        rep = (seller or {}).get("seller_reputation") or {}
        if official:
            return True
        if rep.get("power_seller_status") in ("gold", "platinum"):
            return True
        lvl = rep.get("level_id")
        if lvl:
            pos = ((rep.get("transactions") or {}).get("ratings") or {}).get("positive")
            return lvl in GOOD_LEVELS and (pos is None or pos >= 0.9)
        return None

    # ---- busca ------------------------------------------------------------
    def search(self, term: str, limit: int = 30) -> list[Offer]:
        j = self._api_get("/sites/MLB/search", q=term, limit=min(limit, 50), condition="new")
        out: list[Offer] = []
        if j and j.get("results"):
            for it in j["results"]:
                official = bool(it.get("official_store_id"))
                title = it.get("title", "")
                o = Offer(store=self.name, title=title, url=it["permalink"], price=it.get("price"),
                          old_price=it.get("original_price"), in_stock=(it.get("available_quantity") or 0) > 0,
                          condition="new" if it.get("condition") == "new" else "used",
                          seller=(it.get("seller") or {}).get("nickname"), official_store=official,
                          seller_ok=self._seller_ok(it.get("seller"), official),
                          free_shipping=(it.get("shipping") or {}).get("free_shipping"), image=it.get("thumbnail"),
                          ext_id=it.get("id"), verified=False)
                out.append(o)
            return out
        # fallback HTML
        r = self.f.get(f"https://lista.mercadolivre.com.br/{self.slug(term)}")
        soup = soup_of(r.text)
        cards = listing_cards(soup, LINK, "https://www.mercadolivre.com.br")
        for c in cards[:limit]:
            low = norm(c["text"])
            m = re.search(r"MLB-?(\d+)", c["url"])
            official = "loja oficial" in low
            top = "mercadolider" in low
            out.append(Offer(store=self.name, title=c["title"], url=c["url"], price=c["price"], old_price=c["old_price"],
                             in_stock=None, condition=detect_condition(c["title"]), official_store=official,
                             seller_ok=True if (official or top) else None,
                             free_shipping=True if "frete gratis" in low else None, image=c.get("image"),
                             ext_id=("MLB" + m.group(1)) if m else None, verified=False))
        return out

    # ---- produto ------------------------------------------------------------
    def fetch_product(self, url: str) -> Optional[Offer]:
        m = re.search(r"MLB-?(\d{8,})", url)
        if m and self._token():
            it = self._api_get(f"/items/MLB{m.group(1)}")
            if it and it.get("status") == "active":
                seller = self._api_get(f"/users/{it['seller_id']}") if it.get("seller_id") else None
                desc = self._api_get(f"/items/MLB{m.group(1)}/description") or {}
                official = bool(it.get("official_store_id"))
                return Offer(store=self.name, title=it["title"], url=it["permalink"], price=it.get("price"),
                             old_price=it.get("original_price"), in_stock=(it.get("available_quantity") or 0) > 0,
                             condition="new" if it.get("condition") == "new" else "used",
                             seller=(seller or {}).get("nickname"), official_store=official,
                             seller_ok=self._seller_ok(seller, official),
                             free_shipping=(it.get("shipping") or {}).get("free_shipping"),
                             description=desc.get("plain_text", ""), image=it.get("thumbnail"), ext_id=it["id"], verified=True)
        return super().fetch_product(url)

    def offer_from_page(self, html: str, url: str) -> Optional[Offer]:
        d = parse_product_generic(html, url)
        if not d.get("title") or not d.get("price"):
            return None
        low = norm(html[:200000])
        official = "loja oficial" in low
        m = re.search(r"MLB-?(\d+)", url)
        o = self.make_offer(d, url, official_store=official,
                            seller_ok=True if (official or "mercadolider" in low) else None,
                            free_shipping=True if "frete gratis" in low else None)
        o.ext_id = ("MLB" + m.group(1)) if m else None
        return o
