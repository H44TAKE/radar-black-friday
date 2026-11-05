"""Testes da lógica (classificação, motor de ofertas, parsers, comunidade).
ATENÇÃO: os HTMLs abaixo são FIXTURES SINTÉTICAS, escritas a partir do que as páginas públicas mostram
(texto/preços do Terabyte e marcação pública da visualização web do Telegram). Elas testam a nossa
lógica; a validação contra o HTML real das lojas é feita pelo comando `diagnose` na nuvem."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from radar.classify import match_subjects
from radar.collectors.terabyte import Terabyte
from radar.community import Ctx, extract_candidate, post_to_offer
from radar.config import load_config
from radar.db import DB
from radar.engine import evaluate
from radar.extract import parse_product_generic
from radar.http import Fetcher
from radar.models import Offer
from radar.notify import Notifier, build_message
from radar.runner import App
from radar.sources import Post, parse_channel_html
from radar.util import clean_url, parse_brl, strip_installments

CFG = load_config()


# --------------------------------------------------------------------- util
def test_parse_brl():
    assert parse_brl("R$ 1.299,90") == 1299.90
    assert parse_brl("629,90") == 629.90
    assert parse_brl(479.9) == 479.9
    assert parse_brl("1.299") == 1299.0
    assert parse_brl("abc") is None


def test_strip_installments_does_not_eat_price():
    t = strip_installments("R$ 6.199 em 10x sem juros ou 10x de R$ 315")
    assert "6.199" in t and "315" not in t


def test_clean_url_removes_tracking():
    assert clean_url("https://x.com/p/1?utm_source=a&id=7#frag") == "https://x.com/p/1?id=7"


# --------------------------------------------------------------------- classificação
def names(title, desc=""):
    return {m.subject: m for m in match_subjects(title, desc, CFG)}


def test_lcd_requires_screen_not_just_rgb():
    assert "watercooler-lcd-ate-600" not in names("Water Cooler Gamer 360mm ARGB Intel AMD Preto")
    assert "watercooler-lcd-ate-600" not in names("Water Cooler Rise Mode Aqua 240mm RGB")


def test_lcd_confirmed_with_customizable_text():
    m = names("Water Cooler 360mm Display LCD Customizável ARGB Intel AMD")
    assert m["watercooler-lcd-ate-600"].confidence == "confirmed"


def test_lcd_screen_without_customization_is_unconfirmed():
    m = names("Water Cooler 240mm com Display de Temperatura LCD")
    assert m["watercooler-lcd-ate-600"].confidence == "unconfirmed"


def test_isengard_matches_product_and_category():
    m = names("Water Cooler SuperFrame ISENGARD MAGIC, 360mm, Bomba Infinit Display LCD Customizável, ARGB, Intel-AMD, White")
    assert "isengard-magic-360" in m and m["watercooler-lcd-ate-600"].confidence == "confirmed"


def test_elg_arm_and_generic_arm():
    m = names("Suporte Articulado Para Monitor ELG Braço Pistão a Gás Vesa 75/100 até 9kg")
    assert "elg-braco-articulado" in m and "bracos-ate-85" in m
    assert "bracos-ate-85" not in names("Suporte de Mesa para Notebook Elevado Dobrável")
    assert "bracos-ate-85" not in names("Suporte articulado para TV 32 a 55 polegadas")


def test_fan_kit_needs_three_and_argb():
    assert "kit-ventoinhas-argb" in names("Kit 3 Fans Cooler ARGB 120mm Com Controladora Redragon")
    assert "kit-ventoinhas-argb" not in names("Ventoinha Cooler Fan ARGB 120mm Unitária")
    assert "kit-ventoinhas-argb" not in names("Kit 3 Fans 120mm Preto Sem Led")
    assert "kit-ventoinhas-argb" in names("Kit 6x Fan ARGB 120mm Pichau Gamer")


# --------------------------------------------------------------------- parsers (fixtures sintéticas)
TERABYTE_HTML = """<html><head><title>Water Cooler SuperFrame ISENGARD MAGIC</title></head><body>
<h1>Water Cooler SuperFrame ISENGARD MAGIC, 360mm, Bomba Infinit, Display LCD Customizável, ARGB, Intel-AMD, White</h1>
<div class="price"><del>R$ 883,90</del> <span>29% OFF</span></div>
<p>R$ 629,90 com 15% de desconto no boleto ou pix</p>
<p>R$ 741,06 em até 12x de R$ 61,75 sem juros no cartão</p>
<p>Quase acabando - Restam 6</p><button>Comprar agora</button>
<h2>DISPLAY PERSONALIZÁVEL</h2><p>O display aceita GIFs carregados pelo usuário com software dedicado.</p>
</body></html>"""


def test_terabyte_page_parse():
    o = Terabyte(Fetcher(), {}).offer_from_page(TERABYTE_HTML, "https://www.terabyteshop.com.br/produto/28263/isengard")
    assert o.price == 629.90 and o.old_price == 883.90 and o.in_stock is True
    assert o.ext_id == "28263"
    m = names(o.title, o.description)
    assert m["watercooler-lcd-ate-600"].confidence == "confirmed"


def test_out_of_stock_button_disabled():
    html = "<html><body><h1>Produto X 360mm</h1><p>R$ 100,00 à vista</p><button disabled>Comprar</button><p>Produto esgotado</p></body></html>"
    assert parse_product_generic(html, "https://x")["in_stock"] is False


JSONLD = """<html><head><script type="application/ld+json">{"@type":"Product","name":"Braço Articulado ELG HZ01","offers":{"@type":"Offer","price":"79.90","availability":"https://schema.org/InStock"},"aggregateRating":{"ratingValue":"4.7","reviewCount":"212"}}</script></head><body><h1>Braço Articulado ELG HZ01</h1></body></html>"""


def test_jsonld_product():
    d = parse_product_generic(JSONLD, "https://x")
    assert d["price"] == 79.90 and d["in_stock"] is True and d["rating"] == 4.7 and d["reviews"] == 212


TG_HTML = """<div class="tgme_widget_message_wrap"><div class="tgme_widget_message" data-post="pxinxa/32541">
<div class="tgme_widget_message_text">🔥Water Cooler LCD 360mm Customizável <br/>👉 De R$ 899 por ✅ R$ 529,90 em 10x sem juros<br/>➡ <a href="https://meli.la/abc">https://meli.la/abc</a> 🤑 Cupom: BF2026</div>
<a class="tgme_widget_message_date" href="https://t.me/pxinxa/32541"><time datetime="2026-11-04T17:55:00+00:00"></time></a></div></div>"""


def test_telegram_channel_parse():
    posts = parse_channel_html(TG_HTML, "pxinxa")
    assert len(posts) == 1 and posts[0].post_id == "32541"
    assert posts[0].links == ["https://meli.la/abc"] and posts[0].ts.year == 2026


def test_candidate_extraction():
    c = extract_candidate(parse_channel_html(TG_HTML, "pxinxa")[0].text)
    assert c.price == 529.90 and c.old_price == 899.0 and c.coupon == "BF2026"
    assert c.title.startswith("Water Cooler LCD 360mm")


def test_candidate_ignores_installments_as_price():
    c = extract_candidate("Smart TV Philips 65\" 4K R$ 3094 10x de R$ 315")
    assert c.price == 3094


# --------------------------------------------------------------------- motor
@pytest.fixture
def db(tmp_path):
    d = DB(tmp_path / "t.sqlite")
    yield d
    d.close()


def offer(price=499.9, **kw):
    base = dict(store="terabyte", title="Water Cooler 360mm Display LCD Customizável ARGB Intel AMD Preto",
                url="https://www.terabyteshop.com.br/produto/1/x", price=price, old_price=799.9, in_stock=True,
                condition="new", verified=True, ext_id="1", rating=4.6)
    base.update(kw)
    return Offer(**base)


def lcd_match(o):
    return [m for m in match_subjects(o.title, o.description, CFG) if m.subject == "watercooler-lcd-ate-600"][0]


def test_cap_triggers_alert_and_dedupes(db):
    o = offer(499.9)
    m = lcd_match(o)
    d = evaluate(db, o, m, CFG)
    assert d.notify and any("preço-alvo" in r for r in d.reasons)
    db.log_alert(o.key, m.subject, o.price, "x", "loja", d.sig, True, "msg")
    d2 = evaluate(db, o, m, CFG)
    assert not d2.notify and any("já avisado" in b for b in d2.blocked)
    # nova queda >5% reenvia
    o2 = offer(450.0)
    assert evaluate(db, o2, lcd_match(o2), CFG).notify


def test_above_cap_without_history_and_small_claim_is_silent(db):
    o = offer(650.0, old_price=700.0)
    d = evaluate(db, o, lcd_match(o), CFG)
    assert not d.notify and "nenhum critério de preço atingido" in d.blocked


def test_unverified_or_out_of_stock_never_notifies(db):
    for kw in ({"in_stock": False}, {"in_stock": None}, {"verified": False}, {"condition": "used"}):
        o = offer(300.0, **kw)
        assert not evaluate(db, o, lcd_match(o), CFG).notify


def test_marketplace_requires_trusted_seller(db):
    o = offer(300.0, store="mercadolivre", seller_ok=None)
    assert not evaluate(db, o, lcd_match(o), CFG).notify
    o = offer(300.0, store="mercadolivre", seller_ok=True)
    assert evaluate(db, o, lcd_match(o), CFG).notify


def test_unconfirmed_lcd_is_blocked(db):
    o = offer(300.0, title="Water Cooler 240mm com Display de Temperatura LCD Intel AMD")
    m = lcd_match(o)
    assert m.confidence == "unconfirmed" and not evaluate(db, o, m, CFG).notify


def _seed_history(db, o, prices, days=6):
    now = datetime.now(timezone.utc)
    db.upsert_offer(o, [])
    for i, p in enumerate(prices):
        ts = (now - timedelta(days=days) + timedelta(hours=i * (days * 24 / len(prices)))).isoformat()
        db.c.execute("INSERT INTO prices(key,ts,price,old_price,in_stock) VALUES(?,?,?,?,1)", (o.key, ts, p, None))
    db.c.commit()


def test_history_based_drop_confirms_discount_and_flags_inflated_old_price(db):
    base = offer(700.0, old_price=1200.0)  # acima do teto de R$600 -> só histórico pode disparar
    _seed_history(db, base, [700, 705, 700, 698, 702, 700, 701, 699])
    now_offer = offer(640.0, old_price=1200.0)
    m = [x for x in match_subjects(now_offer.title, "", CFG) if x.subject == "watercooler-lcd-ate-600"][0]
    d = evaluate(db, now_offer, m, CFG)
    assert d.discount_confirmed and 7 < d.discount_pct < 10
    assert any("inflado" in f for f in d.flags)
    # queda de ~9% < 10% (mínimo padrão): NÃO vale como "abaixo do habitual", mas é a menor marca já vista
    assert not any("abaixo do preço habitual" in r for r in d.reasons)
    assert d.notify and "menor preço já registrado" in d.reasons


def test_big_drop_vs_history_notifies(db):
    base = offer(900.0, old_price=None)
    _seed_history(db, base, [900, 905, 895, 900, 902, 898, 900, 901])
    o = offer(700.0, old_price=None)
    m = lcd_match(o)
    d = evaluate(db, o, m, CFG)
    assert d.notify and d.discount_confirmed and any("abaixo do preço habitual" in r for r in d.reasons)


def test_claimed_discount_without_history_is_marked_unconfirmed(db):
    spec_offer = Offer(store="kabum", title="Kit 3 Fans Cooler ARGB 120mm Com Controladora Redragon", url="https://www.kabum.com.br/produto/9/x",
                       price=89.9, old_price=189.9, in_stock=True, verified=True, condition="new", ext_id="9", rating=4.8, reviews=300)
    m = [x for x in match_subjects(spec_offer.title, "", CFG) if x.subject == "kit-ventoinhas-argb"][0]
    d = evaluate(db, spec_offer, m, CFG)
    assert d.notify and not d.discount_confirmed
    msg = build_message(spec_offer, d)
    assert "NÃO confirmado" in msg and "sem histórico" in msg


def test_message_for_community_offer_has_origin(db):
    o = offer(529.9, origin="community", origin_label="Canal do Telegram @pxinxa", origin_url="https://t.me/pxinxa/1", announced_price=529.9)
    d = evaluate(db, o, lcd_match(o), CFG)
    msg = build_message(o, d)
    assert "OFERTA ENCONTRADA EM COMUNIDADE" in msg and "@pxinxa" in msg and "Preço verificado" in msg


# --------------------------------------------------------------------- integração com coletor falso
class FakeStore(Terabyte):
    def __init__(self, page_offer=None):
        super().__init__(Fetcher(), {})
        self.page_offer = page_offer
        self.calls = 0

    def fetch_product(self, url):
        self.calls += 1
        return self.page_offer


def test_listing_offer_gets_verified_before_notify(db):
    verified = offer(499.9)
    store = FakeStore(verified)
    n = Notifier(dry_run=True)
    app = App(CFG, db, Fetcher(), n, {"terabyte": store})
    listing = offer(499.9, verified=False)
    assert app.handle_offer(listing) == 1 and store.calls == 1
    assert len(n.sent) == 1


def test_listing_that_fails_verification_is_silent(db):
    store = FakeStore(None)
    n = Notifier(dry_run=True)
    app = App(CFG, db, Fetcher(), n, {"terabyte": store})
    assert app.handle_offer(offer(499.9, verified=False)) == 0 and not n.sent


def test_irrelevant_listing_costs_no_request(db):
    store = FakeStore(None)
    app = App(CFG, db, Fetcher(), Notifier(dry_run=True), {"terabyte": store})
    app.handle_offer(offer(10.0, title="Mouse Gamer RGB 7200dpi", verified=False))
    assert store.calls == 0


def test_community_post_flows_through_store_verification(db):
    page = offer(529.9, title="Water Cooler 360mm Display LCD Customizável ARGB Intel AMD Preto")
    store = FakeStore(page)
    post = parse_channel_html(TG_HTML, "pxinxa")[0]
    post.links = ["https://www.terabyteshop.com.br/produto/1/x"]
    ctx = Ctx(db, Fetcher(), {"terabyte": store}, CFG, set(CFG["trusted_domains"]))
    o, status = post_to_offer(post, ctx)
    assert status == "ok" and o.origin == "community" and o.announced_price == 529.90
    n = Notifier(dry_run=True)
    app = App(CFG, db, Fetcher(), n, {"terabyte": store})
    assert app.handle_offer(o) == 1
    assert "@pxinxa" in n.sent[0]


def test_community_irrelevant_and_untrusted_links(db):
    ctx = Ctx(db, Fetcher(), {"terabyte": FakeStore(None)}, CFG, set(CFG["trusted_domains"]))
    p = Post("telegram:x", "Canal", "1", datetime.now(timezone.utc), "Fone Bluetooth JBL R$ 99 https://golpe.xyz/a", ["https://golpe.xyz/a"])
    assert post_to_offer(p, ctx) == (None, "irrelevante")
    p2 = Post("telegram:x", "Canal", "2", datetime.now(timezone.utc), "Water Cooler LCD 360mm Customizável R$ 400 https://golpe.xyz/a", ["https://golpe.xyz/a"])
    o, status = post_to_offer(p2, ctx)
    assert o is None and "fora da lista de confiança" in status


class BlockedStore(Terabyte):
    def fetch_product(self, url):
        from radar.http import Blocked
        raise Blocked(url, "HTTP 403", 403)


def test_blocked_store_uses_community_price_when_enabled(db):
    import copy
    cfg = copy.deepcopy(CFG)
    cfg["telegram"]["accept_unconfirmed_when_blocked"] = True
    post = parse_channel_html(TG_HTML, "pxinxa")[0]
    post.links = ["https://www.terabyteshop.com.br/produto/1/x"]
    ctx = Ctx(db, Fetcher(), {"terabyte": BlockedStore(Fetcher(), {})}, cfg, set(cfg["trusted_domains"]))
    o, status = post_to_offer(post, ctx)
    assert status == "ok-nao-verificado" and o.unconfirmed and o.price == 529.90 and o.verified is False
    n = Notifier(dry_run=True)
    app = App(cfg, db, Fetcher(), n, {"terabyte": BlockedStore(Fetcher(), {})})
    assert app.handle_offer(o) == 1
    assert "NÃO verificado" in n.sent[0]
    # não polui o histórico de preços
    assert db.c.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 0


def test_blocked_store_is_discarded_when_disabled(db):
    import copy
    cfg = copy.deepcopy(CFG)
    cfg["telegram"]["accept_unconfirmed_when_blocked"] = False
    post = parse_channel_html(TG_HTML, "pxinxa")[0]
    post.links = ["https://www.terabyteshop.com.br/produto/1/x"]
    ctx = Ctx(db, Fetcher(), {"terabyte": BlockedStore(Fetcher(), {})}, cfg, set(cfg["trusted_domains"]))
    o, status = post_to_offer(post, ctx)
    assert o is None and "bloqueado" in status
