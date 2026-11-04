"""AliExpress via API oficial de Afiliados (Portals). O site bloqueia raspagem automatizada, então o
caminho legítimo é a API. Requer ALI_APP_KEY, ALI_APP_SECRET (e opcional ALI_TRACKING_ID) de um app
aprovado em https://portals.aliexpress.com . Sem credenciais, a fonte fica desativada (e o painel avisa)."""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import time
from typing import Optional

from ..classify import detect_condition
from ..models import Offer
from ..util import parse_brl
from .base import Collector

ENDPOINT = "https://api-sg.aliexpress.com/sync"


def sign(params: dict, secret: str) -> str:
    base = "".join(f"{k}{params[k]}" for k in sorted(params))
    return hmac.new(secret.encode(), base.encode(), hashlib.sha256).hexdigest().upper()


class AliExpress(Collector):
    name = "aliexpress"
    label = "AliExpress"
    domains = ("aliexpress.com", "aliexpress.us", "s.click.aliexpress.com")
    marketplace = True
    needs_env = ("ALI_APP_KEY", "ALI_APP_SECRET")

    def _call(self, method: str, **kw) -> dict:
        p = {"app_key": os.environ["ALI_APP_KEY"], "timestamp": str(int(time.time() * 1000)), "sign_method": "sha256",
             "method": method, **{k: str(v) for k, v in kw.items()}}
        p["sign"] = sign(p, os.environ["ALI_APP_SECRET"])
        r = self.f.post(ENDPOINT, data=p)
        return r.json()

    @staticmethod
    def _items(resp: dict) -> list[dict]:
        for k, v in resp.items():
            if k.endswith("_response"):
                res = (v.get("resp_result") or {}).get("result") or {}
                prods = res.get("products") or {}
                return prods.get("product") or prods.get("traffic_product_d_t_o") or []
        return []

    def _to_offer(self, p: dict, verified: bool) -> Optional[Offer]:
        price = parse_brl(p.get("target_sale_price") or p.get("target_app_sale_price"))
        if not price:
            return None
        rate = parse_brl(str(p.get("evaluate_rate", "")).replace("%", ""))
        sales = int(re.sub(r"\D", "", str(p.get("lastest_volume") or p.get("latest_volume") or 0)) or 0)
        link = p.get("product_detail_url") or p.get("promotion_link")
        title = p.get("product_title", "")
        return Offer(store=self.name, title=title, url=link, price=price, old_price=parse_brl(p.get("target_original_price")),
                     in_stock=True, condition=detect_condition(title), seller=p.get("shop_url"),
                     seller_ok=bool(rate and rate >= 95 and sales >= 50), rating=(rate / 20 if rate else None),
                     reviews=sales, image=p.get("product_main_image_url"), ext_id=str(p.get("product_id")), verified=verified)

    def search(self, term: str, limit: int = 30) -> list[Offer]:
        resp = self._call("aliexpress.affiliate.product.query", keywords=term, target_currency="BRL", target_language="PT",
                          ship_to_country="BR", page_size=min(limit, 50), page_no=1, sort="SALE_PRICE_ASC",
                          tracking_id=os.environ.get("ALI_TRACKING_ID", "default"), fields="commission_rate,sale_price")
        return [o for o in (self._to_offer(p, False) for p in self._items(resp)) if o]

    def fetch_product(self, url: str) -> Optional[Offer]:
        m = re.search(r"/item/(\d+)", url) or re.search(r"(\d{10,})", url)
        if not m:
            return None
        resp = self._call("aliexpress.affiliate.productdetail.get", product_ids=m.group(1), target_currency="BRL",
                          target_language="PT", country="BR", tracking_id=os.environ.get("ALI_TRACKING_ID", "default"))
        items = self._items(resp)
        return self._to_offer(items[0], True) if items else None
