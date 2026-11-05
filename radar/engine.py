"""Motor de decisão: uma oferta só vira notificação se passar nos filtros de qualidade,
atingir um critério de preço real e não for repetição."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .classify import Match
from .db import DB
from .models import Offer
from .util import brl, norm, sha

MARKETPLACES = {"mercadolivre", "aliexpress", "shopee"}

DEFAULTS = {
    "history": {"min_observations": 6, "min_span_days": 2, "window_days": 30},
    "alerts": {"resend_drop_pct": 5, "unverified_claim_min_pct": 40, "max_unverified_per_day": 3,
               "default_min_drop_pct": 10, "inflated_old_price_factor": 1.2, "jump_pct": 20},
}


def setting(cfg: dict, group: str, key: str):
    return ((cfg.get("settings") or {}).get(group) or {}).get(key, DEFAULTS[group][key])


@dataclass
class Decision:
    notify: bool
    subject: str
    subject_name: str
    reasons: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)
    reference_price: Optional[float] = None
    reference_label: str = ""
    discount_pct: Optional[float] = None
    discount_confirmed: bool = False
    history_n: int = 0
    other: Optional[dict] = None
    sig: str = ""
    notes: list[str] = field(default_factory=list)


def subject_spec(cfg: dict, subject_id: str) -> dict:
    for p in cfg.get("products", []) or []:
        if p["id"] == subject_id:
            return p
    for c in cfg.get("categories", []) or []:
        if c["id"] == subject_id:
            return c
    return {}


def evaluate(db: DB, offer: Offer, match: Match, cfg: dict) -> Decision:
    spec = subject_spec(cfg, match.subject)
    d = Decision(False, match.subject, match.name, notes=list(match.notes), flags=list(match.flags))
    price = offer.price
    trusted = set(cfg.get("trusted_domains") or [])

    # ---------------- filtros duros (qualidade / segurança) ----------------
    if not price:
        d.blocked.append("sem preço legível")
    unc = bool(getattr(offer, "unconfirmed", False))
    if unc:
        d.flags.append("preço DIVULGADO no grupo, NÃO verificado na loja (a loja bloqueou a abertura da página) — "
                       "confira preço, estoque e vendedor antes de comprar")
    if offer.in_stock is not True and not unc:
        d.blocked.append("esgotado" if offer.in_stock is False else "estoque não confirmado")
    if (offer.condition or "new") != "new" and not spec.get("allow_used"):
        d.blocked.append("produto usado/recondicionado")
    if not offer.verified and not unc:
        d.blocked.append("preço não verificado na página do produto")
    if match.confidence != "confirmed":
        d.blocked.append("característica obrigatória não confirmada (" + "; ".join(match.flags) + ")")
    if offer.store in MARKETPLACES or offer.store.startswith("outro:"):
        if offer.store.startswith("outro:") and offer.host not in trusted and not any(offer.host.endswith("." + t) for t in trusted):
            d.blocked.append("loja fora da lista de confiança")
        elif offer.store in MARKETPLACES and offer.seller_ok is not True and not offer.official_store and not unc:
            d.blocked.append("vendedor não confirmado como confiável")
    min_rating = spec.get("min_rating")
    if min_rating and offer.rating is not None and offer.rating < float(min_rating):
        d.blocked.append(f"avaliação baixa ({offer.rating:.1f})")
    if match.brand_ok is False:
        # marca desconhecida: exige prova social forte
        if not (offer.rating and offer.rating >= 4.5 and (offer.reviews or 0) >= 50):
            d.blocked.append("marca não reconhecida sem avaliações suficientes")
    if offer.rating is None:
        d.flags.append("avaliações não verificadas")
    if d.blocked:
        return d

    # ---------------- critérios de preço ----------------
    total = offer.total() or price
    hmin = setting(cfg, "history", "min_observations")
    hspan = setting(cfg, "history", "min_span_days")
    window = setting(cfg, "history", "window_days")
    stats = db.history_stats(offer.key, window)
    d.history_n = stats["n"]
    enough = stats["n"] >= hmin and stats["span_days"] >= hspan and stats["median"]
    jump = db.recent_jump(offer.key, 21)
    jumped = jump is not None and jump > setting(cfg, "alerts", "jump_pct")

    cap = spec.get("max_price")
    if cap and price <= float(cap):
        d.reasons.append(f"abaixo do preço-alvo ({brl(float(cap))})")

    min_drop = float(spec.get("min_drop_pct", setting(cfg, "alerts", "default_min_drop_pct")))
    if enough:
        med = stats["median"]
        d.reference_price, d.reference_label = med, f"mediana de {window} dias ({stats['n']} amostras)"
        d.discount_pct = (1 - price / med) * 100
        d.discount_confirmed = True
        if offer.old_price and offer.old_price > med * setting(cfg, "alerts", "inflated_old_price_factor"):
            d.flags.append(f"preço 'de' da loja parece inflado ({brl(offer.old_price)} vs. habitual {brl(med)})")
        if jumped:
            d.flags.append(f"preço subiu {jump:.0f}% nas últimas semanas (possível aumento pré-Black Friday)")
        hist_ok = not jumped or price <= (stats["min"] or price)
        if hist_ok and d.discount_pct >= min_drop:
            d.reasons.append(f"{d.discount_pct:.0f}% abaixo do preço habitual")
        if hist_ok and stats["min"] and price <= stats["min"] and d.discount_pct >= 3 and stats["n"] >= hmin:
            d.reasons.append("menor preço já registrado")
    else:
        claimed = (1 - price / offer.old_price) * 100 if offer.old_price and offer.old_price > price else None
        if offer.old_price:
            d.reference_price, d.reference_label = offer.old_price, "preço 'de' declarado pela loja (não confirmado)"
            d.discount_pct = claimed
        floor = setting(cfg, "alerts", "unverified_claim_min_pct")
        if (not cap or price > float(cap)) and claimed and claimed >= floor:
            if db.unverified_alerts_today(match.subject) < setting(cfg, "alerts", "max_unverified_per_day"):
                d.reasons.append(f"desconto declarado de {claimed:.0f}% (sem histórico suficiente para confirmar)")
            else:
                d.blocked.append("limite diário de alertas sem histórico atingido")
        if offer.old_price is None:
            d.flags.append("desconto não pôde ser confirmado (sem histórico e sem preço 'de')")

    if not d.reasons:
        d.blocked.append("nenhum critério de preço atingido")
        return d

    # ---------------- repetição / duplicidade ----------------
    d.sig = sha(offer.store, norm(offer.title)[:48], str(round(price)))
    last = db.last_alert(offer.key, match.subject)
    if last:
        drop_needed = setting(cfg, "alerts", "resend_drop_pct")
        further_drop = price <= last["price"] * (1 - drop_needed / 100)
        restocked = db.went_out_of_stock_since(offer.key, last["ts"])
        if not (further_drop or restocked):
            d.blocked.append("já avisado (sem nova queda relevante)")
            return d
        d.reasons.append("nova queda após o último alerta" if further_drop else "voltou ao estoque")
    elif db.sig_alerted_recently(d.sig):
        d.blocked.append("anúncio duplicado já avisado")
        return d

    # ---------------- comparação com outras lojas ----------------
    other = db.best_other(match.subject, offer.key)
    if other is not None:
        d.other = {"store": other["store"], "price": other["price"], "url": other["url"]}
        if other["price"] < total * 0.98:
            d.flags.append(f"mais barato em {other['store']}: {brl(other['price'])}")
    d.notify = True
    return d
