"""Fontes de comunidade: canais públicos do Telegram (visualização web oficial t.me/s/<canal>),
grupos onde o seu bot foi adicionado (Bot API getUpdates) e perfis profissionais do Instagram
(Business Discovery, API oficial da Meta)."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import requests

from .db import DB
from .extract import soup_of
from .http import Fetcher


@dataclass
class Post:
    source: str            # telegram:pxinxa | telegram-bot:<chat> | instagram:<user>
    label: str             # texto exibido na notificação
    post_id: str
    ts: datetime
    text: str
    links: list[str] = field(default_factory=list)
    permalink: str = ""
    extra_hosts: tuple[str, ...] = ()   # domínios de redirecionamento próprios do canal (ex.: pxinxa.com.br)

    @property
    def uid(self) -> str:
        return f"{self.source}:{self.post_id}"


# ------------------------------------------------------------------ Telegram: canais públicos
def parse_channel_html(html: str, handle: str, extra_hosts: tuple[str, ...] = ()) -> list[Post]:
    soup = soup_of(html)
    posts: list[Post] = []
    for w in soup.select("div.tgme_widget_message"):
        dp = w.get("data-post") or ""
        if "/" not in dp:
            continue
        pid = dp.split("/", 1)[1]
        text_el = w.select_one("div.tgme_widget_message_text")
        if text_el is None:
            continue
        for br in text_el.find_all("br"):
            br.replace_with("\n")
        text = text_el.get_text()
        links = []
        for a in text_el.find_all("a", href=True):
            h = a["href"]
            if h.startswith("http") and "t.me/" not in h:
                links.append(h)
        for m in re.findall(r"https?://[^\s<>\"']+", text):
            if m not in links and "t.me/" not in m:
                links.append(m.rstrip(".,)"))
        t_el = w.select_one("a.tgme_widget_message_date time")
        try:
            ts = datetime.fromisoformat(t_el["datetime"]) if t_el and t_el.get("datetime") else datetime.now(timezone.utc)
        except ValueError:
            ts = datetime.now(timezone.utc)
        posts.append(Post(source=f"telegram:{handle}", label=f"Canal do Telegram @{handle}", post_id=pid, ts=ts,
                          text=text.strip(), links=links, permalink=f"https://t.me/{handle}/{pid}", extra_hosts=extra_hosts))
    return posts


def fetch_channel(f: Fetcher, handle: str, extra_hosts: tuple[str, ...] = (), respect_robots: bool = True) -> list[Post]:
    r = f.get(f"https://t.me/s/{handle}", check_robots=respect_robots)
    if r.status_code != 200:
        raise RuntimeError(f"t.me/s/{handle} respondeu {r.status_code} (canal inexistente ou sem visualização pública)")
    posts = parse_channel_html(r.text, handle, extra_hosts)
    if not posts and "tgme_channel_history" not in r.text:
        raise RuntimeError(f"@{handle} não tem visualização web pública")
    return posts


# ------------------------------------------------------------------ Telegram: grupos via bot
def bot_updates(db: DB, token: str, allow_chats: Optional[list] = None, owner_chat: Optional[str] = None) -> list[Post]:
    """Lê mensagens de grupos/canais onde o bot é membro. Requer: bot adicionado ao grupo e, em grupos,
    'Group Privacy' desligado no BotFather (ou bot como admin). Canais de terceiros NÃO são acessíveis assim."""
    offset = int(db.kv_get("tg_offset", "0") or 0)
    r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates",
                     params={"offset": offset, "timeout": 0, "allowed_updates": '["message","channel_post"]'}, timeout=25)
    j = r.json()
    if not j.get("ok"):
        raise RuntimeError(f"getUpdates falhou: {j.get('description')}")
    posts: list[Post] = []
    last = offset
    for u in j.get("result", []):
        last = max(last, u["update_id"] + 1)
        m = u.get("message") or u.get("channel_post")
        if not m:
            continue
        chat = m.get("chat", {})
        if chat.get("type") == "private" or str(chat.get("id")) == str(owner_chat):
            continue
        if allow_chats and str(chat.get("id")) not in [str(x) for x in allow_chats] and chat.get("username") not in allow_chats:
            continue
        text = m.get("text") or m.get("caption") or ""
        ents = (m.get("entities") or []) + (m.get("caption_entities") or [])
        links = [e["url"] for e in ents if e.get("type") == "text_link" and e.get("url")]
        for e in ents:
            if e.get("type") == "url":
                links.append(text[e["offset"]:e["offset"] + e["length"]])
        if not links:
            links = re.findall(r"https?://[^\s]+", text)
        cid = str(chat.get("id"))
        uname = chat.get("username")
        perma = f"https://t.me/{uname}/{m['message_id']}" if uname else f"https://t.me/c/{cid.replace('-100', '')}/{m['message_id']}"
        title = chat.get("title") or uname or cid
        posts.append(Post(source=f"telegram-bot:{cid}", label=f"Grupo do Telegram “{title}”", post_id=str(m["message_id"]),
                          ts=datetime.fromtimestamp(m["date"], tz=timezone.utc), text=text, links=links, permalink=perma))
    if last != offset:
        db.kv_set("tg_offset", str(last))
    return posts


# ------------------------------------------------------------------ Instagram (Business Discovery)
def instagram_posts(f: Fetcher, profiles: list[str], limit: int = 12) -> list[Post]:
    """API oficial. Requer IG_USER_ID (sua conta profissional do Instagram) e IG_ACCESS_TOKEN (token longo do Facebook Login).
    Só enxerga contas Comerciais/Criador públicas. NÃO acessa Stories, contas pessoais ou privadas."""
    uid, tok = os.environ.get("IG_USER_ID"), os.environ.get("IG_ACCESS_TOKEN")
    if not (uid and tok):
        raise RuntimeError("Instagram não configurado (IG_USER_ID / IG_ACCESS_TOKEN)")
    out: list[Post] = []
    for user in profiles:
        fields = f"business_discovery.username({user}){{media.limit({limit}){{id,caption,permalink,timestamp,media_type}}}}"
        r = f.get(f"https://graph.facebook.com/v21.0/{uid}", params={"fields": fields, "access_token": tok}, check_robots=False)
        j = r.json()
        if "error" in j:
            err = j["error"]
            if err.get("code") == 190:
                raise RuntimeError("token do Instagram expirou — gere um novo (IG_ACCESS_TOKEN)")
            raise RuntimeError(f"@{user}: {err.get('message', 'erro')} (perfil precisa ser Comercial/Criador e público)")
        for m in (((j.get("business_discovery") or {}).get("media") or {}).get("data")) or []:
            cap = m.get("caption") or ""
            try:
                ts = datetime.strptime(m["timestamp"], "%Y-%m-%dT%H:%M:%S%z")
            except Exception:
                ts = datetime.now(timezone.utc)
            links = re.findall(r"https?://[^\s]+", cap)
            out.append(Post(source=f"instagram:{user}", label=f"Instagram @{user}", post_id=m["id"], ts=ts, text=cap,
                            links=links, permalink=m.get("permalink", f"https://www.instagram.com/{user}/")))
    return out
