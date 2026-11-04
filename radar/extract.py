"""Extração genérica de dados de produto: JSON-LD, meta tags, __NEXT_DATA__ e padrões de texto.
Cada loja tenta primeiro seus seletores específicos e cai aqui como alternativa."""
from __future__ import annotations

import copy
import json
import re
from typing import Any, Iterator, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .util import find_prices, norm, parse_brl, strip_installments

OOS_MARKERS = (
    "produto esgotado", "esgotado", "indisponível", "indisponivel", "avise-me", "avise me quando",
    "sem estoque", "fora de estoque", "temporariamente indisponível", "não disponível",
    "publicação pausada", "anúncio pausado", "nao esta disponivel", "este produto não está mais disponível",
)
BUY_MARKERS = ("comprar agora", "adicionar ao carrinho", "comprar", "adicionar à sacola", "colocar no carrinho")

CASH_PATTERNS = [
    re.compile(r"R\$\s*([\d.]+,\d{2})\s*(?:à vista|a vista|no pix|via pix|no boleto|no pix ou boleto|pix)", re.I),
    re.compile(r"R\$\s*([\d.]+,\d{2})\s*com\s*\d+\s*%\s*de\s*desconto", re.I),
    re.compile(r"(?:à vista|a vista|no pix|via pix)\D{0,30}R\$\s*([\d.]+,\d{2})", re.I),
]
PRICE_KEYS_CASH = ("pix_price", "pixPrice", "price_pix", "price_with_discount", "priceWithDiscount",
                   "finalPrice", "final_price", "sale_price", "salePrice", "selling_price", "current_price",
                   "currentPrice", "discountPrice", "price")
PRICE_KEYS_ALL = PRICE_KEYS_CASH + ("old_price", "oldPrice", "original_price", "originalPrice", "list_price",
                                    "listPrice", "priceFrom", "price_from", "regular_price", "regularPrice")
TITLE_KEYS = ("name", "title", "productName", "product_name", "nome")
URL_KEYS = ("url", "link", "product_link", "productLink", "permalink", "href", "path", "slug", "canonicalUrl")


def soup_of(html: str) -> BeautifulSoup:
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:
        return BeautifulSoup(html, "html.parser")


# ---------------------------------------------------------------- JSON
def iter_jsonld(soup: BeautifulSoup) -> Iterator[dict]:
    for tag in soup.find_all("script", type="application/ld+json"):
        raw = tag.string or tag.get_text() or ""
        try:
            data = json.loads(raw)
        except Exception:
            continue
        stack = [data]
        while stack:
            x = stack.pop()
            if isinstance(x, list):
                stack.extend(x)
            elif isinstance(x, dict):
                yield x
                for k in ("@graph", "itemListElement", "item", "mainEntity"):
                    if k in x:
                        stack.append(x[k])


def next_data(soup: BeautifulSoup) -> Optional[dict]:
    tag = soup.find("script", id="__NEXT_DATA__")
    if not tag:
        return None
    try:
        return json.loads(tag.string or tag.get_text())
    except Exception:
        return None


def _num(v: Any) -> Optional[float]:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if v > 0 else None
    if isinstance(v, str):
        return parse_brl(v)
    return None


def walk_products(obj: Any, base: str = "", _depth: int = 0) -> Iterator[dict]:
    """Percorre um JSON qualquer e devolve dicts que parecem produtos (título + preço + link)."""
    if _depth > 14:
        return
    if isinstance(obj, dict):
        title = next((obj[k] for k in TITLE_KEYS if isinstance(obj.get(k), str) and len(obj[k]) > 6), None)
        prices = {k: _num(obj.get(k)) for k in PRICE_KEYS_ALL if _num(obj.get(k))}
        # offers aninhados (schema.org)
        if "offers" in obj and isinstance(obj["offers"], (dict, list)):
            offs = obj["offers"] if isinstance(obj["offers"], list) else [obj["offers"]]
            for o in offs:
                if isinstance(o, dict):
                    for k in ("price", "lowPrice"):
                        if _num(o.get(k)):
                            prices.setdefault(k, _num(o.get(k)))
        link = next((obj[k] for k in URL_KEYS if isinstance(obj.get(k), str) and obj[k]), None)
        if title and prices and link:
            cash_candidates = [prices[k] for k in PRICE_KEYS_CASH if k in prices] or list(prices.values())
            cash = min(cash_candidates)
            olds = [v for v in prices.values() if v > cash * 1.01]
            avail = obj.get("available", obj.get("isAvailable", obj.get("in_stock", obj.get("inStock"))))
            if isinstance(obj.get("offers"), dict):
                a = str(obj["offers"].get("availability", ""))
                if a:
                    avail = a.lower().endswith("instock")
            yield {
                "title": title, "price": cash, "old_price": max(olds) if olds else None,
                "url": urljoin(base, link) if base else link,
                "in_stock": avail if isinstance(avail, bool) else None,
                "image": next((obj[k] for k in ("image", "thumbnail", "img") if isinstance(obj.get(k), str)), None),
                "rating": _num(obj.get("rating") or obj.get("ratingValue") or obj.get("stars")),
                "id": str(obj.get("id") or obj.get("productId") or obj.get("code") or "") or None,
                "raw": obj,
            }
        for v in obj.values():
            if isinstance(v, str) and 50 < len(v) < 400000 and v[:1] in "{[":
                try:
                    v = json.loads(v)
                except Exception:
                    continue
            yield from walk_products(v, base, _depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk_products(v, base, _depth + 1)


# ---------------------------------------------------------------- Página de produto
def detect_stock(text: str, jsonld_avail: Optional[bool] = None, buy_active: Optional[bool] = None) -> Optional[bool]:
    """JSON-LD é autoridade; depois botão de compra ativo/desativado; por fim marcadores de texto."""
    t = norm(text)
    if jsonld_avail is not None:
        return jsonld_avail
    hard_oos = ("produto esgotado", "sem estoque", "fora de estoque", "temporariamente indisponivel",
                "publicacao pausada", "anuncio pausado", "este produto nao esta mais disponivel")
    if buy_active is True:
        return not any(m in t for m in hard_oos)
    if buy_active is False:
        return False
    if any(m in t for m in hard_oos) or "avise-me" in t:
        return False
    if any(m in t for m in BUY_MARKERS):
        return True
    return None


def buy_button_state(soup: BeautifulSoup) -> Optional[bool]:
    """True = há botão 'comprar' ativo; False = só desativados; None = nenhum botão encontrado."""
    found = False
    for el in soup.find_all(["button", "a", "input"]):
        label = norm(el.get_text(" ") or el.get("value", "") or "")
        if any(k in label for k in ("comprar", "adicionar ao carrinho", "adicionar a sacola")) and len(label) < 40:
            found = True
            cls = " ".join(el.get("class", []))
            if not el.has_attr("disabled") and "disabled" not in cls and "indisponivel" not in cls:
                return True
    return False if found else None


def cash_price_from_text(text: str) -> Optional[float]:
    for pat in CASH_PATTERNS:
        m = pat.search(text)
        if m:
            return parse_brl(m.group(1))
    return None


OLD_SELECTOR = "del, s, strike, [class*=old], [class*=antigo], [class*=from], [class*=riscado], [class*=previous]"
NOISE_RE = re.compile(r"(?:economize|economia de|desconto de|cashback|acima de|a partir de|cupom|frete|ganhe)[^R]{0,25}R\$\s*[\d.,]+", re.I)


def strip_noise(text: str) -> str:
    return NOISE_RE.sub(" ", text)


def old_price_from_dom(soup: BeautifulSoup) -> Optional[float]:
    cands = soup.select(OLD_SELECTOR)
    for el in cands:
        ps = find_prices(el.get_text(" "))
        if ps:
            return max(ps)
    return None


def parse_product_generic(html: str, url: str) -> dict:
    """Retorna dict com title, price, old_price, in_stock, description, rating, reviews, image."""
    soup = soup_of(html)
    out: dict[str, Any] = {"title": None, "price": None, "old_price": None, "in_stock": None,
                           "description": "", "rating": None, "reviews": None, "image": None}
    avail: Optional[bool] = None

    # 1) JSON-LD Product
    for d in iter_jsonld(soup):
        t = d.get("@type")
        types = t if isinstance(t, list) else [t]
        if "Product" in types:
            out["title"] = out["title"] or d.get("name")
            out["description"] = out["description"] or (d.get("description") or "")
            img = d.get("image")
            out["image"] = out["image"] or (img[0] if isinstance(img, list) and img else img if isinstance(img, str) else None)
            agg = d.get("aggregateRating") or {}
            out["rating"] = out["rating"] or _num(agg.get("ratingValue"))
            rc = agg.get("reviewCount") or agg.get("ratingCount")
            out["reviews"] = out["reviews"] or (int(rc) if str(rc).isdigit() else None)
            offers = d.get("offers")
            offs = offers if isinstance(offers, list) else [offers] if isinstance(offers, dict) else []
            for o in offs:
                p = _num(o.get("price")) or _num(o.get("lowPrice"))
                if p and (out["price"] is None or p < out["price"]):
                    out["price"] = p
                a = str(o.get("availability", ""))
                if a:
                    avail = a.lower().endswith("instock") if avail is None else (avail or a.lower().endswith("instock"))

    # 2) meta tags
    def meta(*names):
        for n in names:
            el = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n})
            if el and el.get("content"):
                return el["content"]
        return None

    if out["price"] is None:
        out["price"] = _num(meta("product:price:amount", "og:price:amount", "twitter:data1"))
    out["title"] = out["title"] or meta("og:title") or (soup.title.string.strip() if soup.title and soup.title.string else None)
    out["image"] = out["image"] or meta("og:image")
    if not out["description"]:
        out["description"] = meta("og:description", "description") or ""

    # 3) texto da página
    body_text = soup.get_text(" ", strip=True)
    clean_text = strip_noise(strip_installments(body_text))
    cash = cash_price_from_text(clean_text)
    if cash and (out["price"] is None or cash < out["price"]):
        out["price"] = cash
    if out["price"] is None:
        ps = find_prices(clean_text[:4000])
        out["price"] = ps[0] if ps else None
    out["old_price"] = old_price_from_dom(soup)
    if out["old_price"] and out["price"] and out["old_price"] <= out["price"]:
        out["old_price"] = None
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        out["title"] = h1.get_text(" ", strip=True)
    out["in_stock"] = detect_stock(body_text, avail, buy_button_state(soup))
    # descrição estendida para classificação (limita tamanho)
    out["description"] = (out["description"] + " " + body_text[:6000]).strip()
    return out


# ---------------------------------------------------------------- Cartões de listagem
def listing_cards(soup: BeautifulSoup, link_re: re.Pattern, base: str, max_up: int = 6) -> list[dict]:
    """Heurística: para cada link de produto, sobe na árvore até achar o contêiner com 'R$'
    que contenha apenas aquele produto."""
    seen: dict[str, dict] = {}
    for a in soup.find_all("a", href=link_re):
        href = urljoin(base, a["href"].split("#")[0])
        if href in seen:
            continue
        node = a
        card = None
        for _ in range(max_up):
            parent = node.parent
            if parent is None:
                break
            links = {urljoin(base, x["href"].split("#")[0]) for x in parent.find_all("a", href=link_re)}
            if len(links) > 1:
                break
            node = parent
            if "R$" in node.get_text(" "):
                card = node
        if card is None:
            continue
        text = card.get_text(" ", strip=True)
        title = (a.get("title") or "").strip()
        if len(title) < 8:
            img = card.find("img", alt=True)
            title = (img["alt"].strip() if img and len(img["alt"]) > 8 else a.get_text(" ", strip=True))
        if len(title) < 8:
            continue
        old = old_price_from_dom(card)
        work = copy.copy(card)
        for el in work.select(OLD_SELECTOR):
            el.decompose()
        clean = strip_noise(strip_installments(work.get_text(" ", strip=True)))
        price = cash_price_from_text(clean)
        if price is None:
            ps = find_prices(clean)
            price = ps[0] if ps else None
        if not price:
            continue
        low = norm(text)
        seen[href] = {
            "title": title, "url": href, "price": price,
            "old_price": old if old and old > price else None,
            "in_stock": False if any(norm(m) in low for m in ("esgotado", "indisponivel")) else None,
            "text": text, "image": (card.find("img").get("src") if card.find("img") else None),
        }
    return list(seen.values())
