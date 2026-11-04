from __future__ import annotations

import os
from typing import Optional
from urllib.parse import quote_plus

from ..classify import detect_condition
from ..extract import parse_product_generic
from ..http import Fetcher
from ..models import Offer
from ..util import host_of


class Collector:
    name = ""
    label = ""
    domains: tuple[str, ...] = ()
    marketplace = False
    needs_env: tuple[str, ...] = ()

    def __init__(self, fetcher: Fetcher, cfg: Optional[dict] = None):
        self.f = fetcher
        self.cfg = cfg or {}

    # ---- ativação ---------------------------------------------------------
    def enabled(self) -> bool:
        v = self.cfg.get("enabled", True)
        if v == "auto":
            return all(os.environ.get(k) for k in self.needs_env) if self.needs_env else True
        return bool(v)

    def owns(self, url: str) -> bool:
        h = host_of(url)
        return any(h == d or h.endswith("." + d) for d in self.domains)

    # ---- API --------------------------------------------------------------
    def search(self, term: str, limit: int = 30) -> list[Offer]:  # pragma: no cover - interface
        raise NotImplementedError

    def fetch_product(self, url: str) -> Optional[Offer]:
        """Implementação padrão: baixa a página e usa a extração genérica."""
        r = self.f.get(url)
        if r.status_code == 404:
            return None
        return self.offer_from_page(r.text, r.url)

    def offer_from_page(self, html: str, url: str) -> Optional[Offer]:
        d = parse_product_generic(html, url)
        if not d.get("title") or not d.get("price"):
            return None
        return self.make_offer(d, url)

    def make_offer(self, d: dict, url: str, **extra) -> Offer:
        title = d["title"].strip()
        o = Offer(
            store=self.name, title=title, url=url, price=d.get("price"), old_price=d.get("old_price"),
            in_stock=d.get("in_stock"), condition=detect_condition(title), rating=d.get("rating"),
            reviews=d.get("reviews"), description=d.get("description", "") or "", image=d.get("image"),
            verified=True, ext_id=d.get("id"),
        )
        for k, v in extra.items():
            setattr(o, k, v)
        return o

    @staticmethod
    def q(term: str) -> str:
        return quote_plus(term)

    @staticmethod
    def slug(term: str) -> str:
        from ..util import norm
        import re
        return re.sub(r"[^a-z0-9]+", "-", norm(term)).strip("-")
