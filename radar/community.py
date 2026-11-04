"""Da publicação (Telegram/Instagram) até uma oferta VERIFICADA na página da loja.
Fluxo: extrair produto/preço -> filtrar por interesse -> resolver link -> abrir a página da loja ->
(se não houver link) procurar nas lojas pelo nome -> só devolve oferta se o preço foi lido ao vivo."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from .classify import match_subjects
from .collectors import Collector, GenericStore, collector_for
from .db import DB
from .http import Blocked, Fetcher, RobotsDisallow
from .models import Offer
from .sources import Post
from .util import clean_url, find_prices, host_of, jaccard, parse_brl, strip_installments

SHORTENERS = {"meli.la", "amzn.to", "s.click.aliexpress.com", "a.aliexpress.com", "shope.ee", "s.shopee.com.br",
              "tidd.ly", "bit.ly", "cutt.ly", "is.gd", "tinyurl.com", "t.co", "abre.ai", "l.instagram.com",
              "kbum.com.br", "tera.bz", "pichau.me", "mercadolivre.com/sec", "link.mercadolivre.com.br"}


@dataclass
class Candidate:
    title: str
    price: Optional[float]
    old_price: Optional[float]
    coupon: Optional[str]


def extract_candidate(text: str) -> Candidate:
    raw = re.sub(r"https?://\S+", " ", text)
    clean = strip_installments(raw)
    coupon = None
    mc = re.search(r"cupom[^A-Za-z0-9]{0,6}([A-Z0-9][A-Z0-9_-]{3,24})", raw, re.I)
    if mc and not re.fullmatch(r"(?i)(de|do|da|off|para|com|aplicado|exclusivo)", mc.group(1)):
        coupon = mc.group(1).upper()
    old = None
    mo = re.search(r"\bde\s*R\$\s*([\d.,]+)", clean, re.I)
    if mo:
        old = parse_brl(mo.group(1))
    price = None
    mp = re.search(r"\bpor\s*[^\dA-Za-z]{0,4}R\$\s*([\d.,]+)", clean, re.I)
    if mp:
        price = parse_brl(mp.group(1))
    if price is None:
        stripped = re.sub(r"\bde\s*R\$\s*[\d.,]+", " ", clean, flags=re.I)
        ps = find_prices(stripped)
        price = ps[0] if ps else None
    # título: texto até o primeiro marcador de preço/link
    head = re.split(r"[👉➡🔗💰✅]|\bde\s*R\$|\bpor\s*R\$|R\$|\bcupom\b", raw, maxsplit=1, flags=re.I)[0]
    head = re.sub(r"^[^\wÀ-ÿ]+", "", head).strip(" -–—:|\n\t")
    if len(head) < 8:  # primeira linha curta: junta com a segunda
        lines = [l.strip() for l in raw.splitlines() if len(l.strip()) > 8]
        head = lines[0] if lines else raw[:120]
        head = re.sub(r"^[^\wÀ-ÿ]+", "", head)
    return Candidate(title=head[:200], price=price, old_price=old, coupon=coupon)


@dataclass
class Ctx:
    db: DB
    f: Fetcher
    collectors: dict[str, Collector]
    cfg: dict
    trusted: set[str] = field(default_factory=set)


def _allowed_to_visit(url: str, ctx: Ctx, extra_hosts: tuple[str, ...]) -> bool:
    h = host_of(url)
    if h in SHORTENERS or h in extra_hosts or any(h.endswith("." + s) for s in SHORTENERS):
        return True
    if any(c.owns(url) for c in ctx.collectors.values()):
        return True
    return h in ctx.trusted or any(h.endswith("." + t) for t in ctx.trusted)


def _tag(o: Offer, post: Post, cand: Candidate) -> Offer:
    o.origin, o.origin_label, o.origin_url = "community", post.label, post.permalink
    o.announced_price = cand.price
    o.coupon = o.coupon or cand.coupon
    return o


def post_to_offer(post: Post, ctx: Ctx) -> tuple[Optional[Offer], str]:
    """Retorna (oferta verificada | None, status)."""
    cand = extract_candidate(post.text)
    if not match_subjects(cand.title, post.text, ctx.cfg):
        return None, "irrelevante"

    note = "sem link verificável"
    visited = 0
    for url in post.links:
        if visited >= 3:
            break
        if not _allowed_to_visit(url, ctx, post.extra_hosts):
            note = f"link fora da lista de confiança ({host_of(url)})"
            continue
        visited += 1
        try:
            direct = {d for col in ctx.collectors.values() for d in col.domains} | ctx.trusted
            h0 = host_of(url)
            needs_resolve = not (h0 in direct or any(h0.endswith("." + d) for d in direct)) or h0 in SHORTENERS
            final = clean_url(ctx.f.resolve(url) if needs_resolve else url)
            coll = collector_for(final, ctx.collectors, ctx.f, ctx.trusted)
            if coll is None:
                note = f"destino não suportado/confiável ({host_of(final)})"
                continue
            if not isinstance(coll, GenericStore) and not coll.enabled():
                note = f"{coll.label}: sem acesso configurado"
                continue
            offer = coll.fetch_product(final)
        except Blocked as e:
            note = f"bloqueado ao abrir link ({e.reason})"
            continue
        except RobotsDisallow:
            note = "robots.txt não permite abrir o link"
            continue
        except Exception as e:  # link quebrado, timeout...
            note = f"link quebrado/inacessível ({str(e)[:60]})"
            continue
        if offer is None:
            note = "página do produto sem preço legível (link quebrado ou produto removido)"
            continue
        if jaccard(cand.title, offer.title) < 0.2:
            note = "produto do link não corresponde ao anunciado"
            continue
        return _tag(offer, post, cand), "ok"

    # sem link utilizável (ex.: Instagram): procurar pelo nome nas lojas e conferir o preço divulgado
    if cand.price and cand.title:
        o = _verify_by_search(cand, post, ctx)
        if o:
            return o, "ok"
        note += "; busca por nome não confirmou o preço"
    return None, note


def _verify_by_search(cand: Candidate, post: Post, ctx: Ctx) -> Optional[Offer]:
    query = " ".join(cand.title.split()[:7])
    tried = 0
    for coll in ctx.collectors.values():
        if not coll.enabled() or tried >= 3:
            continue
        tried += 1
        try:
            results = coll.search(query, limit=10)
        except Exception:
            continue
        results = [r for r in results if r.price and abs(r.price - cand.price) / cand.price <= 0.06
                   and jaccard(cand.title, r.title) >= 0.5]
        if not results:
            continue
        best = max(results, key=lambda r: jaccard(cand.title, r.title))
        try:
            ver = coll.fetch_product(best.url)
        except Exception:
            continue
        if ver and abs(ver.price - cand.price) / cand.price <= 0.06:
            return _tag(ver, post, cand)
    return None
