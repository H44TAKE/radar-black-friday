"""Classificação: decide se um anúncio pertence a um produto/categoria monitorada.
Regras rígidas para evitar falsos positivos (ex.: water cooler só com RGB != water cooler com tela LCD)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from .util import any_in, norm

USED_WORDS = ("usado", "seminovo", "semi-novo", "semi novo", "recondicionado", "reembalado", "open box",
              "openbox", "vitrine", "com defeito", "para retirada de pecas", "retirada de peças", "refurbished")

# Lista inicial (editável em config/watchlist.yml -> known_lcd_models) de modelos de water cooler com
# tela LCD/OLED personalizável confirmada pelo fabricante.
DEFAULT_KNOWN_LCD = [
    r"isengard magic", r"superframe.*(magic|lcd)", r"kraken (elite|z\d{2}|20(23|24))",
    r"galahad ii? lcd|galahad 2 lcd", r"h\d{3}i elite.*lcd|titan \d{3} rx.*lcd|icue link titan.*lcd",
    r"frozen warframe", r"mpg coreliquid k\d{3}",
]
FAN_BRANDS = ["redragon", "pichau", "corsair", "cooler master", "deepcool", "thermalright", "lian li", "gamdias",
              "rise mode", "mancer", "t-dagger", "gamemax", "id-cooling", "id cooling", "pcyes", "fortrek",
              "mymax", "cougar", "nzxt", "phanteks", "be quiet", "noctua", "arctic", "hayom", "aigo", "xigmatek",
              "bluecase", "k-mex", "kmex", "evus", "vinik", "liketec", "warrior", "oex"]


@dataclass
class Match:
    subject: str
    kind: str                      # product | category
    name: str
    confidence: str = "confirmed"  # confirmed | unconfirmed
    notes: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    brand_ok: Optional[bool] = None


def detect_condition(title: str) -> str:
    return "used" if any_in(title, USED_WORDS) else "new"


def _size_mm(text: str) -> Optional[int]:
    m = re.search(r"\b(120|140|240|280|360|420)\s*mm\b", text)
    if m:
        return int(m.group(1))
    m = re.search(r"\b(240|280|360|420)\b", text)
    return int(m.group(1)) if m else None


# ----------------------------------------------------------------------- regras de categoria
def rule_monitor_arm(title: str, desc: str, cfg: dict) -> Optional[Match]:
    t = norm(title)
    kinds = ("braco articulado", "suporte articulado", "monitor arm", "suporte para monitor", "suporte de monitor",
             "braco para monitor", "braco monitor", "suporte monitor", "articulado para monitor", "articulado monitor",
             "base articulada para monitor")
    if not any(k in t for k in kinds) or "monitor" not in t:
        return None
    if any(w in t for w in ("suporte para tv", "projetor", "microfone", "tablet", "celular", "camera", "lampada",
                            "luminaria", "tripe", "hdmi", "rack", "elevado", "elevacao", "riser", "suporte de parede para tv")):
        return None
    if not any(w in t for w in ("articulad", "braco", "gas", "pistao", "mola", "arm")):
        return None
    m = Match("", "category", "")
    full = norm(title + " " + desc[:3000])
    if "vesa" in full:
        m.notes.append("compatível VESA")
    else:
        m.notes.append("VESA não mencionado no anúncio")
    if "gas" in full or "pistao" in full:
        m.notes.append("pistão a gás (ajuste fino)")
    mm = re.search(r"(\d{1,2})\s*(?:a|-|até)?\s*(\d{1,2})?\s*kg", full)
    if mm:
        m.notes.append(f"capacidade citada: {mm.group(0)}")
    return m


def _fan_count(t: str) -> Optional[int]:
    pats = [r"kit\s*(?:com|de|c/)?\s*(\d{1,2})\b", r"\b(\d{1,2})\s*x\s*(?:fan|ventoinha|cooler|ventilador)",
            r"\b(\d{1,2})\s*(?:fans|ventoinhas|ventiladores|coolers|unidades|pecas|pcs)\b",
            r"\b(\d{1,2})\s*(?:em|in)\s*1\b", r"\b(?:fan|ventoinha|cooler)s?\b[^\d]{0,20}\bx\s*(\d{1,2})\b",
            r"\b(\d{1,2})\s*(?:fan|ventoinha|cooler)\b"]
    for p in pats:
        m = re.search(p, t)
        if m:
            n = int(m.group(1))
            if 1 <= n <= 12:
                return n
    return None


def rule_argb_fan_kit(title: str, desc: str, cfg: dict) -> Optional[Match]:
    t = norm(title)
    if not any(w in t for w in ("ventoinha", "fan", "ventilador", "cooler fan")):
        return None
    if "argb" not in t and "a-rgb" not in t:
        return None
    if any(w in t for w in ("water cooler", "watercooler", "notebook", "usb", "mesa", "teto", "gabinete completo",
                            "pc gamer", "hub apenas", "somente controladora", "controladora apenas")):
        return None
    n = _fan_count(t)
    minimum = int(cfg.get("min_fans", 3))
    if n is None:
        # sem quantidade clara no título -> não arrisca
        return None
    if n < minimum:
        return None
    m = Match("", "category", "")
    m.notes.append(f"{n} ventoinhas")
    full = norm(title + " " + desc[:2000])
    if any(w in full for w in ("controladora", "controller", " hub", "controle remoto")):
        m.notes.append("controladora inclusa")
    brands = [b for b in (cfg.get("trusted_brands") or FAN_BRANDS) if norm(b) in t]
    m.brand_ok = bool(brands)
    if brands:
        m.notes.append(f"marca reconhecida: {brands[0]}")
    else:
        m.flags.append("marca não reconhecida (exige avaliações altas)")
    return m


def rule_lcd_watercooler(title: str, desc: str, cfg: dict) -> Optional[Match]:
    t = norm(title)
    full = norm(title + " " + (desc or "")[:6000])
    known_pats = cfg.get("_known_lcd") or DEFAULT_KNOWN_LCD
    is_wc = any(w in t for w in ("water cooler", "watercooler", "water-cooler", "cooler liquido", "refrigeracao liquida",
                                 "liquid cooler", "watercool", " aio ", "kraken", "galahad", "frozen warframe"))
    if not is_wc:
        return None
    known = any(re.search(p, t) for p in known_pats)
    screen_title = any(w in t for w in ("lcd", "display", "tela", "screen", " ips ", "oled"))
    custom_words = ("customiz", "personaliz", "gif", "animad", "animac", "imagem", "imagens", "upload", "wallpaper", "video")
    custom_title = any(w in t for w in custom_words)
    screen_desc = any(w in full for w in ("lcd", "display", "tela ", "screen", "oled"))
    custom_desc = any(w in full for w in ("customiz", "personaliz", "gif", "animac", "suas imagens", "sua propria imagem"))
    if not (known or screen_title or (screen_desc and custom_desc)):
        return None  # só RGB/ARGB -> NÃO é o que o usuário quer
    m = Match("", "category", "")
    if known or (screen_title and (custom_title or custom_desc)) or (screen_desc and custom_desc and "lcd" in full):
        m.confidence = "confirmed"
        m.notes.append("tela personalizável confirmada" + (" (modelo conhecido)" if known else " (texto do anúncio)"))
    else:
        m.confidence = "unconfirmed"
        m.flags.append("tela citada, mas sem evidência de personalização")
    size = _size_mm(t) or _size_mm(full[:1500])
    if size:
        m.notes.append(f"{size}mm")
        sizes = cfg.get("sizes") or [240, 360]
        if size not in sizes:
            m.flags.append(f"tamanho {size}mm fora do preferido ({'/'.join(map(str, sizes))})")
    if any(w in full for w in ("redond", "circular", "round", "circle")):
        m.notes.append("tela redonda")
    else:
        m.notes.append("formato da tela não confirmado")
    has_intel = "intel" in full or "lga" in full
    has_amd = "amd" in full or re.search(r"\bam[45]\b", full) is not None
    if has_intel and has_amd:
        m.notes.append("compatível Intel e AMD")
    else:
        m.flags.append("compatibilidade Intel/AMD não confirmada no texto")
    return m


RULES = {"monitor_arm": rule_monitor_arm, "argb_fan_kit": rule_argb_fan_kit, "lcd_watercooler": rule_lcd_watercooler}


def match_product(title: str, spec: dict) -> bool:
    t = " " + norm(title) + " "
    groups = spec.get("match_all") or []
    for g in groups:
        alts = g if isinstance(g, list) else [g]
        def hit(a) -> bool:
            a = norm(str(a))
            if a.isdigit():  # "360" casa "360mm" e "360 mm", mas não "1360"
                return re.search(r"(?<!\d)" + a + r"(?!\d)", t) is not None
            return re.search(r"\b" + re.escape(a) + r"\b", t) is not None
        if not any(hit(a) for a in alts):
            return False
    if spec.get("exclude_any") and any_in(title, spec["exclude_any"]):
        return False
    return bool(groups)


def match_subjects(title: str, desc: str, cfg: dict) -> list[Match]:
    """Devolve todos os assuntos (produtos e categorias) a que o anúncio pertence."""
    out: list[Match] = []
    settings_known = cfg.get("known_lcd_models")
    for p in cfg.get("products", []) or []:
        if match_product(title, p):
            out.append(Match(p["id"], "product", p["name"]))
    for c in cfg.get("categories", []) or []:
        fn = RULES.get(c.get("rule"))
        if not fn:
            continue
        c = dict(c)
        if settings_known:
            c["_known_lcd"] = settings_known
        m = fn(title, desc, c)
        if m:
            m.subject, m.name = c["id"], c["name"]
            out.append(m)
    return out
