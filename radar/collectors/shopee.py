"""Shopee via Open API de Afiliados (GraphQL). O site da Shopee bloqueia automação; o caminho legítimo é a
API de afiliados (SHOPEE_APP_ID / SHOPEE_SECRET, cadastro gratuito no programa de afiliados)."""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from typing import Optional

from ..classify import detect_condition
from ..models import Offer
from ..util import parse_brl
from .base import Collector

ENDPOINT = "https://open-api.affiliate.shopee.com.br/graphql"

QUERY = """query($kw:String!,$limit:Int!){productOfferV2(keyword:$kw,limit:$limit,sortType:5){
nodes{itemId productName priceMin priceMax productLink offerLink shopName ratingStar sales shopType}}}"""


class Shopee(Collector):
    name = "shopee"
    label = "Shopee"
    domains = ("shopee.com.br", "shope.ee", "s.shopee.com.br")
    marketplace = True
    needs_env = ("SHOPEE_APP_ID", "SHOPEE_SECRET")

    def _gql(self, query: str, variables: dict) -> dict:
        app, secret = os.environ["SHOPEE_APP_ID"], os.environ["SHOPEE_SECRET"]
        payload = json.dumps({"query": query, "variables": variables}, separators=(",", ":"))
        ts = str(int(time.time()))
        signature = hashlib.sha256((app + ts + payload + secret).encode()).hexdigest()
        r = self.f.post(ENDPOINT, data=payload.encode(), headers={
            "Content-Type": "application/json",
            "Authorization": f"SHA256 Credential={app}, Timestamp={ts}, Signature={signature}"})
        return r.json()

    def _to_offer(self, n: dict, verified: bool) -> Optional[Offer]:
        price = parse_brl(n.get("priceMin"))
        if not price:
            return None
        title = n.get("productName", "")
        stars, sales = float(n.get("ratingStar") or 0), int(n.get("sales") or 0)
        official = n.get("shopType") in ([1], [2], 1, 2) or (isinstance(n.get("shopType"), list) and any(x in (1, 2) for x in n["shopType"]))
        return Offer(store=self.name, title=title, url=n.get("productLink") or n.get("offerLink"), price=price,
                     in_stock=True, condition=detect_condition(title), seller=n.get("shopName"), official_store=bool(official),
                     seller_ok=bool(official or (stars >= 4.7 and sales >= 100)), rating=stars or None, reviews=sales,
                     ext_id=str(n.get("itemId")), verified=verified)

    def search(self, term: str, limit: int = 30) -> list[Offer]:
        j = self._gql(QUERY, {"kw": term, "limit": min(limit, 50)})
        nodes = (((j.get("data") or {}).get("productOfferV2") or {}).get("nodes")) or []
        return [o for o in (self._to_offer(n, False) for n in nodes) if o]

    def fetch_product(self, url: str) -> Optional[Offer]:
        m = re.search(r"i\.(\d+)\.(\d+)", url) or re.search(r"/product/(\d+)/(\d+)", url)
        if not m:
            return None
        q = """query($id:Int64!){productOfferV2(itemId:$id,limit:1){nodes{itemId productName priceMin productLink offerLink shopName ratingStar sales shopType}}}"""
        j = self._gql(q, {"id": int(m.group(2))})
        nodes = (((j.get("data") or {}).get("productOfferV2") or {}).get("nodes")) or []
        return self._to_offer(nodes[0], True) if nodes else None
