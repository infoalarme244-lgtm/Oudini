"""Extraction de faits observables depuis une page HTML publique.

Le contenu est traité comme une DONNÉE : aucune partie n'est exécutée ni interprétée comme consigne.
Les tentatives d'injection d'instructions sont détectées, signalées et neutralisées.
"""
import json
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

INJECTION_PATTERNS = [
    r"ignore (all |any )?(previous|prior|above) (instructions|prompts)",
    r"ignore(z)? (les|toutes les) (instructions|consignes) (précédentes|ci-dessus)",
    r"disregard (the )?(system|previous) (prompt|instructions)",
    r"you are now (a|an|in) ",
    r"tu es maintenant",
    r"system prompt",
    r"<\s*/?\s*(system|assistant|untrusted_data)\s*>",
    r"(rate|note|classe)[^.]{0,40}(10/10|5/5|best|meilleur)[^.]{0,40}(competitor|concurrent)",
    r"(reveal|révèle|print|affiche)[^.]{0,30}(api key|secret|clé api|password|mot de passe)",
]
_INJ = [re.compile(p, re.I) for p in INJECTION_PATTERNS]

KEYWORDS = {
    "free_shipping": [r"livraison (offerte|gratuite)", r"free (delivery|shipping)", r"gratis (verzending|levering)"],
    "free_shipping_threshold": [r"(livraison (offerte|gratuite)|gratis verzending|free shipping)[^.€]{0,40}?(dès|à partir de|vanaf|over|from)\s*(\d+[.,]?\d*)\s*(€|eur)"],
    "free_returns": [r"retours? (gratuits?|offerts?)", r"free returns", r"gratis retour"],
    "return_window": [r"(\d{1,3})\s*jours?[^.]{0,30}(retour|rembours)", r"(retour|return|retourneren)[^.]{0,40}?(\d{1,3})\s*(jours|days|dagen)"],
    "delivery_time": [r"(livr[ée]e?s?|livraison|delivery|levering)[^.]{0,40}?(\d)\s*(?:-|à|a|to|tot)\s*(\d)\s*(jours|days|dagen|j\b)", r"(24|48)\s*h"],
    "authenticity": [r"authenti(que|city|ek)", r"100\s*%\s*(original|authentique)", r"revendeur agréé|authorized retailer|official (store|retailer)"],
    "size_guide": [r"guide des tailles", r"size (guide|chart)", r"maattabel"],
    "click_collect": [r"click\s*(&|and|et)\s*collect", r"retrait en magasin"],
    "loyalty": [r"programme de fidélité", r"loyalty", r"points? fidélité", r"membership"],
    "student_discount": [r"étudiant", r"student"],
    "warranty": [r"garantie", r"warranty", r"garantie légale"],
    "live_chat": [r"chat en direct", r"live chat", r"whatsapp"],
}
PAYMENT_METHODS = {
    "Carte bancaire": [r"\bvisa\b", r"mastercard", r"carte bancaire", r"\bcb\b"],
    "PayPal": [r"paypal"], "Bancontact": [r"bancontact"], "Apple Pay": [r"apple\s*pay"],
    "Google Pay": [r"google\s*pay"], "Klarna": [r"klarna"], "iDEAL": [r"\bideal\b"],
    "Alma": [r"\balma\b"], "Payconiq": [r"payconiq"], "American Express": [r"american express|\bamex\b"],
}
SOCIAL = {"instagram.com": "Instagram", "tiktok.com": "TikTok", "facebook.com": "Facebook",
          "youtube.com": "YouTube", "pinterest.": "Pinterest", "x.com": "X", "twitter.com": "X"}
POLICY_LINK = re.compile(r"(livraison|shipping|delivery|levering|retour|return|faq|aide|help|klantenservice|conditions|cgv)", re.I)
PRICE_RE = re.compile(r"(\d{1,4}(?:[.,]\d{2})?)\s?€|€\s?(\d{1,4}(?:[.,]\d{2})?)")


def detect_injection(text: str) -> list[str]:
    hits = []
    for rx in _INJ:
        m = rx.search(text)
        if m:
            hits.append(m.group(0)[:120])
    return hits


def neutralise(text: str) -> str:
    for rx in _INJ:
        text = rx.sub("[contenu neutralisé]", text)
    return text


def _visible_text(soup: BeautifulSoup) -> tuple[str, list[str]]:
    hidden_flags = []
    for el in soup(["script", "style", "noscript", "template", "svg"]):
        if el.name == "script" and el.get("type") == "application/ld+json":
            continue
        el.decompose()
    for el in soup.select('[style*="display:none"], [style*="display: none"], [hidden], [aria-hidden="true"]'):
        t = el.get_text(" ", strip=True)
        if t and detect_injection(t):
            hidden_flags.append("Texte caché contenant des instructions : " + t[:100])
        el.decompose()
    txt = soup.get_text(" ", strip=True)
    return re.sub(r"\s+", " ", txt), hidden_flags


def _json_ld(soup: BeautifulSoup) -> list[dict]:
    out = []
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(s.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        items = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
        out.extend(i for i in items if isinstance(i, dict))
    return out


def _price(v):
    try:
        return round(float(str(v).replace(",", ".")), 2)
    except (TypeError, ValueError):
        return None


def extract_page(url: str, html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    lang = (soup.html.get("lang") if soup.html else "") or ""
    title = soup.title.get_text(strip=True) if soup.title else ""
    meta = {m.get("name") or m.get("property"): m.get("content", "") for m in soup.find_all("meta") if m.get("content")}
    links = [(a.get_text(" ", strip=True), urljoin(url, a["href"])) for a in soup.find_all("a", href=True)]
    has_search = bool(soup.select('input[type="search"], form[role="search"], input[name="q"], input[name="search"]'))
    filter_hits = len(soup.select('[class*="filter"], [id*="filter"], [class*="facet"], [data-filter]'))
    h1 = [h.get_text(" ", strip=True) for h in soup.find_all("h1")][:3]
    nav_items = len(soup.select("nav a"))
    hreflangs = sorted({l.get("hreflang") for l in soup.find_all("link", hreflang=True)})
    canonical = (soup.find("link", rel="canonical") or {}).get("href", "") if soup.find("link", rel="canonical") else ""
    ld = _json_ld(soup)
    text, hidden_flags = _visible_text(soup)
    flags = hidden_flags + ["Instruction détectée dans le contenu : " + h for h in detect_injection(text)]
    text = neutralise(text)
    low = text.lower()

    features = {}
    for key, pats in KEYWORDS.items():
        for p in pats:
            m = re.search(p, low, re.I)
            if m:
                features[key] = m.group(0)[:160]
                break
    payments = sorted(name for name, pats in PAYMENT_METHODS.items() if any(re.search(p, low) for p in pats))
    socials = sorted({name for _, href in links for dom, name in SOCIAL.items() if dom in href})
    host = urlparse(url).netloc
    policy_links = []
    for label, href in links:
        if urlparse(href).netloc == host and POLICY_LINK.search(label + " " + href) and href not in policy_links:
            policy_links.append(href)
    blog = any(re.search(r"/(blog|magazine|journal|news|actualites)", h) for _, h in links)

    products, rating = [], None
    for item in ld:
        t = item.get("@type")
        types = t if isinstance(t, list) else [t]
        if "Product" in types:
            offers = item.get("offers") or {}
            offers = offers[0] if isinstance(offers, list) and offers else offers
            if isinstance(offers, dict):
                price = _price(offers.get("price") or offers.get("lowPrice"))
                products.append({"name": str(item.get("name", ""))[:160], "brand": (item.get("brand") or {}).get("name", "")
                                 if isinstance(item.get("brand"), dict) else str(item.get("brand", "")),
                                 "price": price, "currency": offers.get("priceCurrency", ""),
                                 "availability": str(offers.get("availability", "")).rsplit("/", 1)[-1]})
            ar = item.get("aggregateRating")
            if isinstance(ar, dict):
                rating = {"value": ar.get("ratingValue"), "count": ar.get("reviewCount") or ar.get("ratingCount")}
    text_prices = sorted({p for p in (_price(a or b) for a, b in PRICE_RE.findall(text)) if p and 5 <= p <= 2000})

    return {
        "url": url, "title": title[:200], "lang": lang, "description": meta.get("description", "")[:300],
        "og_title": meta.get("og:title", "")[:200], "h1": h1, "viewport": "viewport" in meta,
        "has_search": has_search, "filter_elements": filter_hits, "nav_items": nav_items,
        "hreflang": [h for h in hreflangs if h], "canonical": canonical, "blog": blog,
        "features": features, "payments": payments, "socials": socials, "policy_links": policy_links[:6],
        "jsonld_products": products[:50], "displayed_rating": rating, "text_prices": text_prices[:200],
        "security_flags": flags, "text_excerpt": text[:3000], "word_count": len(text.split()),
    }
