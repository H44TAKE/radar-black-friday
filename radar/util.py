"""Utilidades: parsing de preços em R$, normalização de texto, chaves."""
from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Iterable, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

_NUM = r"\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?|\d+\.\d{2}(?!\d)|\d+(?:,\d{1,2})?"
PRICE_RE = re.compile(r"R\$\s*(" + _NUM + r")")
INSTALLMENT_RE = re.compile(
    r"\b\d{1,2}\s*x\s*(?:de|of|sem juros|s/\s*juros)?\s*(?:R\$\s*[\d.,]+)?(?:\s*(?:sem juros|s/\s*juros|no cart[aã]o))?",
    re.I,
)
TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "gclid", "fbclid",
    "aff_fcid", "aff_fsk", "aff_platform", "aff_trace_key", "sk", "tt", "mm_cid", "ref", "tag",
    "matt_tool", "matt_word", "matt_source", "matt_campaign", "matt_ad_group", "matt_ad_type",
    "pdp_ext_f", "pdp_npi", "spm", "scm", "algo_pvid", "algo_exp_id", "btsid", "ws_ab_test",
    "obj_id", "dp_id", "aff_short_key", "afSmartRedirect", "sourceType", "channel", "afTraceInfo",
}


def parse_brl(value) -> Optional[float]:
    """'R$ 1.299,90' / '629,90' / 629.9 -> float. None se não der."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    s = str(value).strip()
    s = s.replace("R$", "").replace("\xa0", " ").strip()
    if not s:
        return None
    m = re.search(r"\d[\d.,]*", s)
    if not m:
        return None
    s = m.group(0)
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(?:\.\d{3})+", s):  # '1.299' (milhar sem centavos)
        s = s.replace(".", "")
    try:
        v = float(s)
    except ValueError:
        return None
    return v if v > 0 else None


def strip_installments(text: str) -> str:
    """Remove trechos de parcelamento ('10x de R$ 315', 'em 10x sem juros') para não confundir com preço."""
    t = re.sub(r"\b\d{1,2}\s*x\s*(?:de|of)\s*R\$\s*[\d.,]+", " ", text, flags=re.I)
    t = re.sub(r"\bem\s*\d{1,2}\s*x(?:\s*(?:sem juros|s/\s*juros|no cart[aã]o))?", " ", t, flags=re.I)
    return t


def find_prices(text: str) -> list[float]:
    out = []
    for m in PRICE_RE.finditer(text or ""):
        v = parse_brl(m.group(1))
        if v:
            out.append(v)
    return out


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s.lower()).strip()


def tokens(s: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", norm(s)) if len(t) > 1}


def jaccard(a: str, b: str) -> float:
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def sha(*parts: str, n: int = 16) -> str:
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:n]


def clean_url(url: str) -> str:
    """Remove parâmetros de rastreio/afiliado e fragmento."""
    try:
        p = urlparse(url)
        q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k not in TRACKING_PARAMS]
        return urlunparse((p.scheme, p.netloc, p.path, p.params, urlencode(q), ""))
    except Exception:
        return url


def host_of(url: str) -> str:
    try:
        h = urlparse(url).netloc.lower()
    except Exception:
        return ""
    return h[4:] if h.startswith("www.") else h


def any_in(text: str, words: Iterable[str]) -> bool:
    t = norm(text)
    return any(norm(w) in t for w in words)


def brl(v: Optional[float]) -> str:
    if v is None:
        return "—"
    s = f"{v:,.2f}"
    return "R$ " + s.replace(",", "X").replace(".", ",").replace("X", ".")
