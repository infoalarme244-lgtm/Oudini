"""Récupération sûre d'URL publiques : anti-SSRF, robots.txt, limites de taille/type, redirections revalidées."""
import ipaddress
import socket
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx

from ..config import settings


class UnsafeURL(ValueError):
    pass


class FetchBlocked(Exception):
    pass


BLOCKED_HOSTS = {"localhost", "metadata.google.internal", "metadata", "instance-data"}


def _ip_is_public(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return not (addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_multicast
                or addr.is_reserved or addr.is_unspecified or (hasattr(addr, "is_site_local") and addr.is_site_local))


def resolve(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise UnsafeURL(f"Nom d'hôte introuvable : {host}") from exc
    return sorted({i[4][0] for i in infos})


def validate_public_url(url: str, resolver=None) -> str:
    """Lève UnsafeURL si l'URL vise une ressource interne ou un schéma non autorisé."""
    resolver = resolver or (lambda h: resolve(h))
    url = (url or "").strip()
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        raise UnsafeURL("Seuls les schémas http et https sont autorisés")
    if not p.hostname:
        raise UnsafeURL("URL sans hôte")
    if p.username or p.password:
        raise UnsafeURL("Les identifiants dans l'URL sont interdits")
    host = p.hostname.lower().rstrip(".")
    if host in BLOCKED_HOSTS or host.endswith(".local") or host.endswith(".internal") or host.endswith(".localhost"):
        raise UnsafeURL("Hôte interne interdit")
    port = p.port or (443 if p.scheme == "https" else 80)
    if port not in settings.fetch_allowed_ports:
        raise UnsafeURL(f"Port {port} non autorisé")
    try:
        ipaddress.ip_address(host)
        ips = [host]
    except ValueError:
        ips = resolver(host)
    if not ips or not all(_ip_is_public(ip) for ip in ips):
        raise UnsafeURL("L'hôte résout vers une adresse privée ou réservée")
    return url


@dataclass
class FetchResult:
    url: str
    status: int
    content_type: str
    text: str
    redirects: list = field(default_factory=list)


class SafeFetcher:
    """Fetcher de production. Les tests injectent un transport httpx factice."""

    def __init__(self, transport: httpx.BaseTransport | None = None, resolver=None, respect_robots: bool = True):
        self.client = httpx.Client(transport=transport, timeout=settings.fetch_timeout, follow_redirects=False,
                                   headers={"User-Agent": settings.user_agent, "Accept": "text/html,application/xhtml+xml"})
        self.resolver = resolver or (lambda h: resolve(h))
        self.respect_robots = respect_robots
        self._robots: dict[str, RobotFileParser | None] = {}

    def allowed_by_robots(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        p = urlparse(url)
        base = f"{p.scheme}://{p.netloc}"
        if base not in self._robots:
            rp = RobotFileParser()
            try:
                r = self._get(base + "/robots.txt")
                if r.status_code == 200:
                    rp.parse(r.text.splitlines())
                else:
                    rp.parse([])  # pas de robots.txt : pas de restriction déclarée
                self._robots[base] = rp
            except Exception:
                self._robots[base] = None
        rp = self._robots[base]
        return True if rp is None else rp.can_fetch(settings.user_agent, url)

    def _get(self, url: str) -> httpx.Response:
        validate_public_url(url, self.resolver)
        with self.client.stream("GET", url) as r:
            body = b""
            for chunk in r.iter_bytes():
                body += chunk
                if len(body) > settings.fetch_max_bytes:
                    raise FetchBlocked("Réponse trop volumineuse")
            r._content = body
            return r

    def fetch(self, url: str, max_redirects: int = 4) -> FetchResult:
        validate_public_url(url, self.resolver)
        if not self.allowed_by_robots(url):
            raise FetchBlocked("Accès refusé par robots.txt : page non analysée")
        redirects = []
        current = url
        for _ in range(max_redirects + 1):
            r = self._get(current)
            if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                nxt = urljoin(current, r.headers["location"])
                validate_public_url(nxt, self.resolver)  # chaque saut est revalidé
                redirects.append(nxt)
                current = nxt
                continue
            if r.status_code in (401, 403, 429):
                raise FetchBlocked(f"Accès restreint (HTTP {r.status_code}) : non contourné")
            ctype = r.headers.get("content-type", "")
            if r.status_code == 200 and not any(t in ctype for t in ("text/html", "application/xhtml", "text/plain", "application/ld+json")):
                raise FetchBlocked(f"Type de contenu non pris en charge : {ctype}")
            return FetchResult(url=current, status=r.status_code, content_type=ctype,
                               text=r.content.decode(r.encoding or "utf-8", errors="replace"), redirects=redirects)
        raise FetchBlocked("Trop de redirections")
