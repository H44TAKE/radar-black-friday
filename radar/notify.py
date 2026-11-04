"""Notificações para o celular: Telegram Bot (principal) e ntfy.sh (opcional).
Credenciais SEMPRE via variáveis de ambiente / GitHub Secrets."""
from __future__ import annotations

import html
import os
from typing import Optional

import requests

from .engine import Decision
from .models import Offer
from .util import brl

STORE_NAMES = {"terabyte": "TerabyteShop", "kabum": "KaBuM!", "pichau": "Pichau", "mercadolivre": "Mercado Livre",
               "aliexpress": "AliExpress", "shopee": "Shopee"}


def store_name(store: str) -> str:
    if store.startswith("outro:"):
        return store.split(":", 1)[1]
    return STORE_NAMES.get(store, store)


def _e(s) -> str:
    return html.escape(str(s), quote=False)


def build_message(o: Offer, d: Decision) -> str:
    community = o.origin == "community"
    head = "🔥 <b>OFERTA ENCONTRADA EM COMUNIDADE!</b>" if community else "🔥 <b>BLACK FRIDAY — OFERTA ENCONTRADA!</b>"
    L = [head, "", f"<b>Produto:</b> {_e(o.title[:160])}"]
    if community:
        L.append(f"<b>Preço divulgado:</b> {brl(o.announced_price) if o.announced_price else 'não informado'}")
        L.append(f"<b>Preço verificado agora:</b> {brl(o.price)}")
    else:
        L.append(f"<b>Preço encontrado:</b> {brl(o.price)}")
    if d.reference_price:
        L.append(f"<b>Preço habitual identificado:</b> {brl(d.reference_price)} <i>({_e(d.reference_label)})</i>")
    else:
        L.append("<b>Preço habitual identificado:</b> indisponível (sem histórico suficiente)")
    if d.discount_pct is not None and d.discount_pct > 0:
        tag = "confirmado pelo histórico" if d.discount_confirmed else "declarado pela loja — NÃO confirmado"
        L.append(f"<b>Desconto estimado:</b> {d.discount_pct:.0f}% <i>({tag})</i>")
    else:
        L.append("<b>Desconto estimado:</b> não foi possível confirmar")
    if community:
        L.append(f"<b>Origem:</b> {_e(o.origin_label or 'comunidade')}")
        if o.origin_url:
            L.append(f"<b>Link da publicação:</b> {_e(o.origin_url)}")
    L.append(f"<b>Loja:</b> {_e(store_name(o.store))}" + (f" — vendedor: {_e(o.seller)}" if o.seller and o.store in ("mercadolivre", "aliexpress", "shopee") else ""))
    if o.shipping is not None:
        L.append(f"<b>Frete:</b> {brl(o.shipping) if o.shipping > 0 else 'grátis'}")
    elif o.free_shipping:
        L.append("<b>Frete:</b> grátis")
    else:
        L.append("<b>Frete:</b> não calculado (informe o CEP no carrinho)")
    if o.coupon:
        L.append(f"<b>Cupom:</b> <code>{_e(o.coupon)}</code>")
    L.append("<b>Disponibilidade:</b> Em estoque ✅ <i>(verificado na página do produto agora)</i>")
    if not community:
        L.append(f"<b>Link direto para comprar:</b> {_e(o.url)}")
    else:
        L.append(f"<b>Link da loja:</b> {_e(o.url)}")
        L.append("<b>Status:</b> Preço verificado.")
    analysis = [r.capitalize() if i == 0 else r for i, r in enumerate(d.reasons)]
    L.append("")
    L.append("<b>Análise:</b> " + _e("; ".join(analysis)) + ".")
    if d.notes:
        L.append("✔️ " + _e("; ".join(d.notes)))
    for f in d.flags:
        L.append("⚠️ " + _e(f))
    return "\n".join(L)


class Notifier:
    def __init__(self, token: Optional[str] = None, chat_id: Optional[str] = None, ntfy_topic: Optional[str] = None,
                 dry_run: bool = False):
        self.token = token or os.environ.get("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID")
        self.ntfy = ntfy_topic or os.environ.get("NTFY_TOPIC")
        self.dry_run = dry_run
        self.sent: list[str] = []

    @property
    def configured(self) -> bool:
        return bool((self.token and self.chat_id) or self.ntfy)

    def send(self, text: str, url: Optional[str] = None, button: str = "Abrir oferta") -> bool:
        if self.dry_run:
            print("[notify:dry-run]\n" + text + "\n")
            self.sent.append(text)
            return True
        if not self.configured:
            print("[notify] Telegram/ntfy não configurado — alerta NÃO enviado:\n" + text + "\n")
            return False
        ok = False
        if self.token and self.chat_id:
            payload = {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": False}
            if url:
                payload["reply_markup"] = {"inline_keyboard": [[{"text": button, "url": url}]]}
            try:
                r = requests.post(f"https://api.telegram.org/bot{self.token}/sendMessage", json=payload, timeout=20)
                ok = r.status_code == 200 and r.json().get("ok", False)
                if not ok:
                    print(f"[notify] Telegram recusou: {r.status_code} {r.text[:200]}")
            except requests.RequestException as e:
                print(f"[notify] erro Telegram: {e}")
        if self.ntfy:
            try:
                plain = html.unescape(text.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", "")
                                      .replace("<code>", "").replace("</code>", ""))
                r = requests.post(f"https://ntfy.sh/{self.ntfy}", data=plain.encode("utf-8"),
                                  headers={"Title": "Radar Black Friday", "Priority": "high", "Click": url or ""}, timeout=20)
                ok = ok or r.status_code == 200
            except requests.RequestException as e:
                print(f"[notify] erro ntfy: {e}")
        if ok:
            self.sent.append(text)
        return ok
