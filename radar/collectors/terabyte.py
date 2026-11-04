"""TerabyteShop: páginas server-rendered, sem JSON-LD (verificado na página do Isengard Magic 360).
Preço à vista (Pix/boleto) é o principal: o site mostra 'R$ X com 15% de desconto no boleto ou pix'."""
from __future__ import annotations

import re
from typing import Optional

from ..extract import listing_cards, parse_product_generic, soup_of
from ..models import Offer
from ..classify import detect_condition
from ..util import parse_brl
from .base import Collector

LINK = re.compile(r"/produto/\d+/")


class Terabyte(Collector):
    name = "terabyte"
    label = "TerabyteShop"
    domains = ("terabyteshop.com.br",)

    def search(self, term: str, limit: int = 30) -> list[Offer]:
        r = self.f.get(f"https://www.terabyteshop.com.br/busca?str={self.q(term)}")
        soup = soup_of(r.text)
        out = []
        for c in listing_cards(soup, LINK, "https://www.terabyteshop.com.br")[:limit]:
            m = re.search(r"/produto/(\d+)/", c["url"])
            out.append(Offer(store=self.name, title=c["title"], url=c["url"], price=c["price"], old_price=c["old_price"],
                             in_stock=c["in_stock"], condition=detect_condition(c["title"]), image=c.get("image"),
                             ext_id=m.group(1) if m else None, verified=False))
        return out

    def offer_from_page(self, html: str, url: str) -> Optional[Offer]:
        soup = soup_of(html)
        d = parse_product_generic(html, url)
        # seletores específicos (tentativa; a extração genérica cobre o resto)
        for sel in ("#valVista", ".valVista", "p.val-prod", "[id*=valVista]"):
            el = soup.select_one(sel)
            if el and parse_brl(el.get_text()):
                d["price"] = parse_brl(el.get_text())
                break
        if not d.get("title") or not d.get("price"):
            return None
        m = re.search(r"/produto/(\d+)/", url)
        d["id"] = m.group(1) if m else None
        return self.make_offer(d, url)
