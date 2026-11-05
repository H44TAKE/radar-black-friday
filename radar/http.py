"""Cliente HTTP educado: intervalo por host, retries curtos, respeito ao robots.txt e
detecção de bloqueio. Quando uma loja bloqueia (403/429/desafio anti-bot) o sistema
NÃO tenta burlar: levanta Blocked, registra e faz backoff."""
from __future__ import annotations

import time
import urllib.robotparser as robotparser
from typing import Optional
from urllib.parse import urlparse

import requests

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0 Safari/537.36 RadarBF/1.0 (monitor pessoal de precos)"
)

CHALLENGE_MARKERS = (
    "just a moment...", "cf-chl", "challenge-platform", "attention required! | cloudflare",
    "px-captcha", "_incapsula_resource", "access denied", "verify you are human",
    "captcha-delivery", "geo.captcha-delivery.com", "unusual traffic",
    "suspicious-traffic",  # Mercado Livre: página de verificação com HTTP 200
)


class Blocked(Exception):
    """A fonte recusou/limitou o acesso automatizado."""

    def __init__(self, url: str, reason: str, status: Optional[int] = None):
        super().__init__(f"{reason} ({url})")
        self.url, self.reason, self.status = url, reason, status


class RobotsDisallow(Exception):
    pass


class Fetcher:
    def __init__(self, timeout: int = 20, min_interval: float = 1.5, respect_robots: bool = True,
                 session: Optional[requests.Session] = None):
        self.timeout = timeout
        self.min_interval = min_interval
        self.respect_robots = respect_robots
        self.s = session or requests.Session()
        self.s.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.5",
        })
        self._last: dict[str, float] = {}
        self._robots: dict[str, Optional[robotparser.RobotFileParser]] = {}

    # -- robots ---------------------------------------------------------
    def _allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        p = urlparse(url)
        base = f"{p.scheme}://{p.netloc}"
        if base not in self._robots:
            rp = robotparser.RobotFileParser()
            try:
                r = self.s.get(base + "/robots.txt", timeout=10)
                if r.status_code == 200 and "user-agent" in r.text.lower():
                    rp.parse(r.text.splitlines())
                    self._robots[base] = rp
                else:
                    self._robots[base] = None  # sem robots utilizável -> permitido
            except requests.RequestException:
                self._robots[base] = None
        rp = self._robots[base]
        return True if rp is None else rp.can_fetch("RadarBF", url)

    def _wait(self, host: str):
        last = self._last.get(host, 0.0)
        delta = time.time() - last
        if delta < self.min_interval:
            time.sleep(self.min_interval - delta)
        self._last[host] = time.time()

    # -- request --------------------------------------------------------
    def request(self, method: str, url: str, *, retries: int = 2, check_robots: bool = True,
                **kw) -> requests.Response:
        if check_robots and not self._allowed(url):
            raise RobotsDisallow(f"robots.txt não permite {url}")
        host = urlparse(url).netloc
        kw.setdefault("timeout", self.timeout)
        last_exc: Optional[Exception] = None
        for attempt in range(retries + 1):
            self._wait(host)
            try:
                r = self.s.request(method, url, **kw)
            except requests.RequestException as e:
                last_exc = e
                time.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code in (403, 407, 429):
                raise Blocked(url, f"HTTP {r.status_code}", r.status_code)
            if r.status_code >= 500:
                last_exc = RuntimeError(f"HTTP {r.status_code}")
                time.sleep(2 * (attempt + 1))
                continue
            ctype = r.headers.get("content-type", "")
            if "html" in ctype or not ctype:
                head = r.text[:6000].lower() if r.content else ""
                if any(m in head for m in CHALLENGE_MARKERS) and len(r.content) < 60000:
                    raise Blocked(url, "desafio anti-bot detectado", r.status_code)
            return r
        raise RuntimeError(f"falha de rede em {url}: {last_exc}")

    def get(self, url: str, **kw) -> requests.Response:
        return self.request("GET", url, **kw)

    def resolve(self, url: str, max_bytes: int = 30000) -> str:
        """Segue redirecionamentos de encurtadores (meli.la, amzn.to...) sem baixar a página inteira.
        Também trata redirecionamento por <meta refresh>/JS simples."""
        import re
        self._wait(urlparse(url).netloc)
        try:
            r = self.s.get(url, allow_redirects=True, stream=True, timeout=self.timeout)
        except requests.RequestException as e:
            raise RuntimeError(f"falha ao resolver {url}: {e}")
        try:
            if r.status_code in (403, 407, 429):
                raise Blocked(url, f"HTTP {r.status_code}", r.status_code)
            final = r.url
            ctype = r.headers.get("content-type", "")
            if "html" in ctype:
                body = next(r.iter_content(max_bytes), b"")
                m = re.search(rb"(?:url=|location(?:\.href)?\s*=\s*[\"'])(https?://[^\"'>\s]+)", body, re.I)
                if m and urlparse(m.group(1).decode(errors="ignore")).netloc != urlparse(final).netloc:
                    return m.group(1).decode(errors="ignore")
            return final
        finally:
            r.close()

    def post(self, url: str, **kw) -> requests.Response:
        return self.request("POST", url, check_robots=False, **kw)
