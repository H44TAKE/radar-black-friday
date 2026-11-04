from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Optional

from .util import clean_url, host_of, sha


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class Offer:
    store: str                      # terabyte, kabum, pichau, mercadolivre, aliexpress, shopee, outro:<host>
    title: str
    url: str
    price: Optional[float] = None   # preço à vista (Pix/boleto) quando existir
    old_price: Optional[float] = None
    installment_total: Optional[float] = None
    in_stock: Optional[bool] = None  # None = não deu para confirmar
    condition: Optional[str] = None  # new | used | refurb
    seller: Optional[str] = None
    seller_ok: Optional[bool] = None  # reputação do vendedor aprovada (marketplaces)
    official_store: bool = False
    rating: Optional[float] = None
    reviews: Optional[int] = None
    shipping: Optional[float] = None  # None = não calculado
    free_shipping: Optional[bool] = None
    coupon: Optional[str] = None
    description: str = ""           # texto da página (para classificação)
    image: Optional[str] = None
    ext_id: Optional[str] = None
    fetched_at: str = field(default_factory=now_iso)
    verified: bool = False          # preço lido da página do produto (não só de listagem)
    # origem (comunidades)
    origin: str = "store"           # store | community
    origin_label: Optional[str] = None
    origin_url: Optional[str] = None
    announced_price: Optional[float] = None

    def __post_init__(self):
        self.url = clean_url(self.url)

    @property
    def key(self) -> str:
        return sha(self.store, self.ext_id or self.url)

    @property
    def host(self) -> str:
        return host_of(self.url)

    def total(self) -> Optional[float]:
        if self.price is None:
            return None
        return self.price + (self.shipping or 0.0)

    def to_dict(self) -> dict:
        return asdict(self)
