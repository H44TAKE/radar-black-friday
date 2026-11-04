"""Pichau: front Next.js. Busca via __NEXT_DATA__ (walk genérico) com fallback para cartões HTML."""
from __future__ import annotations

import re
from typing import Optional

from ..classify import detect_condition
from ..extract import listing_cards, next_data, parse_product_generic, soup_of, walk_products
from ..models import Offer
from .base import Collector

BASE = "https://www.pichau.com.br"
LINK = re.compile(r"^(?:https://www\.pichau\.com\.br)?/[a-z0-9]+(?:-[a-z0-9]+){3,}/?$")


class Pichau(Collector):
    name = "pichau"
    label = "Pichau"
    domains = ("pichau.com.br",)

    def search(self, term: str, limit: int = 30) -> list[Offer]:
        r = self.f.get(f"{BASE}/search?q={self.q(term)}")
        soup = soup_of(r.text)
        out, seen = [], set()
        nd = next_data(soup)
        for p in (walk_products(nd, BASE) if nd else []):
            if p["url"] in seen or not LINK.match(p["url"]):
                continue
            seen.add(p["url"])
            out.append(Offer(store=self.name, title=p["title"], url=p["url"], price=p["price"], old_price=p["old_price"],
                             in_stock=p["in_stock"], condition=detect_condition(p["title"]), rating=p.get("rating"),
                             image=p.get("image"), verified=False))
        if not out:
            for c in listing_cards(soup, LINK, BASE):
                out.append(Offer(store=self.name, title=c["title"], url=c["url"], price=c["price"], old_price=c["old_price"],
                                 in_stock=c["in_stock"], condition=detect_condition(c["title"]), image=c.get("image"),
                                 verified=False))
        return out[:limit]

    def offer_from_page(self, html: str, url: str) -> Optional[Offer]:
        d = parse_product_generic(html, url)
        if not d.get("title") or not d.get("price"):
            soup = soup_of(html)
            nd = next_data(soup)
            for p in (walk_products(nd, BASE) if nd else []):
                if p["url"].rstrip("/") == url.rstrip("/"):
                    d.update({"title": d.get("title") or p["title"], "price": p["price"], "old_price": p["old_price"],
                              "in_stock": p["in_stock"] if d.get("in_stock") is None else d["in_stock"]})
                    break
        if not d.get("title") or not d.get("price"):
            return None
        return self.make_offer(d, url)
