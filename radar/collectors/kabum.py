"""KaBuM!: o site é Next.js. Busca pelo endpoint JSON que o próprio front-end usa; se falhar,
cai para o HTML (__NEXT_DATA__ / cartões). Tudo passa por Fetcher (robots + detecção de bloqueio)."""
from __future__ import annotations

import json
import re
from typing import Optional

from ..classify import detect_condition
from ..extract import listing_cards, next_data, parse_product_generic, soup_of, walk_products
from ..http import Blocked, RobotsDisallow
from ..models import Offer
from .base import Collector

BASE = "https://www.kabum.com.br"
LINK = re.compile(r"/produto/\d+")


class Kabum(Collector):
    name = "kabum"
    label = "KaBuM!"
    domains = ("kabum.com.br",)

    @staticmethod
    def _catalog_items(obj, _depth: int = 0):
        """Formato atual do catálogo da KaBuM!: {code, name, friendlyName, price, priceWithDiscount, oldPrice, available}.
        Não há campo de URL — ela é /produto/<code>/<friendlyName>. O walk_products genérico descarta esses itens."""
        if _depth > 14:
            return
        if isinstance(obj, dict):
            if obj.get("code") and obj.get("friendlyName") and isinstance(obj.get("name"), str):
                prices = [float(obj[k]) for k in ("priceWithDiscount", "price") if isinstance(obj.get(k), (int, float)) and obj[k] > 0]
                if prices:
                    cash = min(prices)
                    old = obj.get("oldPrice")
                    yield {"title": obj["name"], "price": cash,
                           "old_price": float(old) if isinstance(old, (int, float)) and old > cash * 1.01 else None,
                           "url": f"{BASE}/produto/{obj['code']}/{obj['friendlyName']}",
                           "in_stock": obj["available"] if isinstance(obj.get("available"), bool) else None,
                           "image": obj.get("image") if isinstance(obj.get("image"), str) else None,
                           "rating": obj.get("rating") or None,
                           "raw": {"has_free_shipping": obj.get("hasFreeShipping", obj.get("has_free_shipping"))}}
                    return
            for v in obj.values():
                yield from Kabum._catalog_items(v, _depth + 1)
        elif isinstance(obj, list):
            for v in obj:
                yield from Kabum._catalog_items(v, _depth + 1)

    def _from_walk(self, data, base=BASE) -> list[Offer]:
        out, seen = [], set()
        for p in [*self._catalog_items(data), *walk_products(data, base)]:
            m = re.search(r"/produto/(\d+)", p["url"])
            if not m or m.group(1) in seen:
                continue
            seen.add(m.group(1))
            raw = p.get("raw", {})
            free = raw.get("has_free_shipping", raw.get("hasFreeShipping"))
            out.append(Offer(store=self.name, title=p["title"], url=p["url"], price=p["price"], old_price=p["old_price"],
                             in_stock=p["in_stock"], condition=detect_condition(p["title"]), rating=p.get("rating"),
                             image=p.get("image"), ext_id=m.group(1), verified=False,
                             free_shipping=free if isinstance(free, bool) else None))
        return out

    def search(self, term: str, limit: int = 30) -> list[Offer]:
        api = ("https://servicespub.prod.api.kabum.com.br/catalog/v2/products?"
               f"query={self.q(term)}&page_number=1&page_size={min(limit, 50)}&sort=most_searched")
        try:
            r = self.f.get(api, headers={"Accept": "application/json"})
            if r.status_code == 200 and "json" in r.headers.get("content-type", ""):
                offers = self._from_walk(r.json(), BASE)
                if offers:
                    return offers[:limit]
        except (Blocked, RobotsDisallow):
            raise
        except Exception:
            pass  # cai para HTML
        r = self.f.get(f"{BASE}/busca/{self.slug(term)}")
        soup = soup_of(r.text)
        nd = next_data(soup)
        offers = self._from_walk(nd, BASE) if nd else []
        if not offers:
            for c in listing_cards(soup, LINK, BASE):
                m = re.search(r"/produto/(\d+)", c["url"])
                offers.append(Offer(store=self.name, title=c["title"], url=c["url"], price=c["price"], old_price=c["old_price"],
                                    in_stock=c["in_stock"], condition=detect_condition(c["title"]), ext_id=m.group(1) if m else None,
                                    verified=False))
        return offers[:limit]

    def offer_from_page(self, html: str, url: str) -> Optional[Offer]:
        d = parse_product_generic(html, url)
        m = re.search(r"/produto/(\d+)", url)
        pid = m.group(1) if m else None
        if pid:
            soup = soup_of(html)
            nd = next_data(soup)
            for p in (walk_products(nd, BASE) if nd else []):
                if f"/produto/{pid}" in p["url"]:
                    d["price"] = d.get("price") or p["price"]
                    d["old_price"] = d.get("old_price") or p["old_price"]
                    if d.get("in_stock") is None:
                        d["in_stock"] = p["in_stock"]
                    d["title"] = d.get("title") or p["title"]
                    break
        if not d.get("title") or not d.get("price"):
            return None
        d["id"] = pid
        return self.make_offer(d, url)
