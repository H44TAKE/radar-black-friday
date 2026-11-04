from __future__ import annotations

from typing import Optional
from urllib.parse import urlparse

from ..http import Fetcher
from ..models import Offer
from ..util import host_of
from .aliexpress import AliExpress
from .base import Collector
from .kabum import Kabum
from .mercadolivre import MercadoLivre
from .pichau import Pichau
from .shopee import Shopee
from .terabyte import Terabyte

REGISTRY = {c.name: c for c in (Terabyte, Kabum, Pichau, MercadoLivre, AliExpress, Shopee)}


class GenericStore(Collector):
    """Loja confiável fora das seis principais: só valida a página de produto (JSON-LD/texto)."""

    def __init__(self, fetcher: Fetcher, host: str):
        super().__init__(fetcher, {})
        self.name = f"outro:{host}"
        self.label = host
        self.domains = (host,)

    def search(self, term: str, limit: int = 30) -> list[Offer]:
        return []


def build_collectors(fetcher: Fetcher, cfg: dict) -> dict[str, Collector]:
    stores_cfg = cfg.get("stores") or {}
    out = {}
    for name, cls in REGISTRY.items():
        c = cls(fetcher, stores_cfg.get(name) or {})
        out[name] = c
    return out


def collector_for(url: str, collectors: dict[str, Collector], fetcher: Fetcher, trusted: set[str]) -> Optional[Collector]:
    for c in collectors.values():
        if c.owns(url):
            return c if c.enabled() or True else None
    h = host_of(url)
    if h in trusted or any(h.endswith("." + t) for t in trusted):
        return GenericStore(fetcher, h)
    return None
