"""Créer une boutique à partir du lien d'un produit (page d'une autre boutique ou d'une marque).

La page est lue comme une SOURCE D'INFORMATIONS, jamais comme un contenu à republier :
- seuls des faits sont extraits : nom, marque, caractéristiques, options (tailles, couleurs), prix observé ;
- les textes de la boutique sont rédigés à neuf (IA) puis contrôlés : toute phrase trop proche de la source est retirée ;
- les photos de la source ne sont jamais reprises (droits d'auteur) : le commerçant ajoute les siennes ;
- la récupération respecte robots.txt, refuse les adresses internes et traite tout texte de la page comme une donnée.
"""
import json
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from . import ai, verticals
from .audit import trace
from .extractor import detect_injection, neutralise
from .url_guard import FetchBlocked, SafeFetcher, UnsafeURL, validate_public_url

FETCHER_FACTORY = SafeFetcher  # remplaçable en test


class ImportFailed(Exception):
    pass


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", BeautifulSoup(html or "", "html.parser").get_text(" ", strip=True)).strip()


def _price(v):
    try:
        return round(float(str(v).replace(",", ".")), 2)
    except (TypeError, ValueError):
        return None


def _from_shopify(data: dict) -> dict:
    p = data.get("product") or {}
    options = {o.get("name", ""): o.get("values", []) for o in p.get("options", []) if o.get("name") and o.get("name") != "Title"}
    prices = [_price(v.get("price")) for v in p.get("variants", []) if _price(v.get("price"))]
    return {"name": p.get("title", ""), "brand": p.get("vendor", ""), "product_type": p.get("product_type", ""),
            "source_text": _text(p.get("body_html", "")), "price": min(prices) if prices else None, "currency": "",
            "options": options, "image_count": len(p.get("images", [])), "tags": p.get("tags", [])}


def _from_html(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    out = {"name": "", "brand": "", "product_type": "", "source_text": "", "price": None, "currency": "", "options": {},
           "image_count": 0, "tags": []}
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(s.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        items = data if isinstance(data, list) else (data.get("@graph") or [data]) if isinstance(data, dict) else []
        for it in items:
            t = it.get("@type") if isinstance(it, dict) else None
            types = t if isinstance(t, list) else [t]
            if not ({"Product", "ProductGroup"} & set(types)):
                continue
            out["name"] = out["name"] or str(it.get("name", ""))
            b = it.get("brand")
            out["brand"] = out["brand"] or (b.get("name", "") if isinstance(b, dict) else str(b or ""))
            out["source_text"] = out["source_text"] or _text(str(it.get("description", "")))
            offers = it.get("offers")
            if not offers and isinstance(it.get("hasVariant"), list) and it["hasVariant"]:
                offers = it["hasVariant"][0].get("offers")
            offers = offers[0] if isinstance(offers, list) and offers else offers
            if isinstance(offers, dict):
                out["price"] = out["price"] or _price(offers.get("price") or offers.get("lowPrice"))
                out["currency"] = out["currency"] or offers.get("priceCurrency", "")
            imgs = it.get("image")
            out["image_count"] = out["image_count"] or (len(imgs) if isinstance(imgs, list) else 1 if imgs else 0)
    meta = {m.get("property") or m.get("name"): m.get("content", "") for m in soup.find_all("meta") if m.get("content")}
    out["name"] = out["name"] or meta.get("og:title", "") or (soup.h1.get_text(" ", strip=True) if soup.h1 else "")
    out["source_text"] = out["source_text"] or meta.get("og:description", "") or meta.get("description", "")
    out["price"] = out["price"] or _price(meta.get("product:price:amount") or meta.get("og:price:amount"))
    out["currency"] = out["currency"] or meta.get("product:price:currency", "")
    return out


def fetch_product(url: str, fetcher=None) -> dict:
    validate_public_url(url)
    fetcher = fetcher or FETCHER_FACTORY()
    facts = None
    path = urlparse(url).path
    if "/products/" in path:  # boutiques Shopify : fiche produit publique au format JSON (options et variantes)
        try:
            r = fetcher.fetch(url.split("?")[0].rstrip("/") + ".json")
            facts = _from_shopify(json.loads(r.text))
        except Exception:  # pas une boutique Shopify, ou JSON indisponible : lecture de la page HTML
            facts = None
    if not facts or not facts.get("name"):
        try:
            r = fetcher.fetch(url)
        except (FetchBlocked, UnsafeURL) as exc:
            raise ImportFailed(str(exc)) from exc
        except Exception as exc:
            raise ImportFailed(f"Page inaccessible ({type(exc).__name__})") from exc
        facts = _from_html(r.text)
    if not facts.get("name"):
        raise ImportFailed("Aucun produit reconnu sur cette page")
    flags = detect_injection(facts["source_text"] + " " + facts["name"])
    facts["source_text"] = neutralise(facts["source_text"])[:6000]
    facts.update({"url": url, "domain": urlparse(url).netloc, "security_flags": flags,
                  "retrieved_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")})
    found = verticals.detect(" ".join([facts["name"], facts["product_type"], " ".join(map(str, facts.get("tags") or []))]))
    facts["vertical"] = found[0] if found else ""
    return facts


# ---------------------------------------------------------------- rédaction originale
COPY_INSTRUCTION = (
    "À partir des faits ci-dessous (issus de la page d'un autre site), rédige en {lang} une fiche produit ORIGINALE pour une "
    "nouvelle boutique : titre, description de 2 à 4 courts paragraphes, et 3 à 6 points forts. Règles : ne reprends aucune "
    "phrase ni tournure de la source (reformule entièrement) ; n'affirme que ce que les faits permettent ; pas d'avis clients, "
    "pas de note, pas d'allégation santé ou médicale, pas d'urgence ni de réduction ; n'utilise pas le nom de la marque comme "
    "nom de boutique ; si une information manque, ne l'invente pas."
)
LANG = {"fr": "français", "nl": "néerlandais (Belgique)", "en": "anglais"}
SCHEMA = {"type": "object", "additionalProperties": False,
          "properties": {"title": {"type": "string"}, "description": {"type": "string"},
                         "highlights": {"type": "array", "items": {"type": "string"}},
                         "security_flags": {"type": "array", "items": {"type": "string"}}},
          "required": ["title", "description", "highlights", "security_flags"]}


def _shingles(text: str, n: int = 7) -> set:
    w = re.findall(r"\w+", text.lower())
    return {" ".join(w[i:i + n]) for i in range(max(0, len(w) - n + 1))}


def strip_copied(generated: str, source: str, n: int = 7) -> tuple[str, int]:
    """Retire chaque phrase qui partage une suite de 7 mots avec la source. Renvoie (texte, nb de phrases retirées)."""
    src = _shingles(source, n)
    kept, removed = [], 0
    for para in generated.split("\n"):
        sentences = re.split(r"(?<=[.!?])\s+", para)
        ok = [s for s in sentences if not (_shingles(s, n) & src)]
        removed += len(sentences) - len(ok)
        kept.append(" ".join(ok))
    return "\n".join(kept).strip(), removed


def write_copy(db, shop, facts: dict, language: str = "fr") -> dict:
    data = json.dumps({k: facts.get(k) for k in ("name", "brand", "product_type", "options", "price", "currency", "source_text")},
                      ensure_ascii=False)
    out = ai.complete_json(db, shop, "product_copy", COPY_INSTRUCTION.format(lang=LANG.get(language, "français")), data, SCHEMA,
                           sources=[facts["url"]])
    if not out:
        trace(db, shop.id, "product_copy", engine="rules", input_summary=facts["url"], sources=[facts["url"]],
              output={"note": "IA non configurée : seul le nom du produit est repris, description à rédiger"})
        return {"title": facts["name"][:200], "description": "", "highlights": [], "engine": "rules", "removed": 0}
    desc, removed = strip_copied(out.get("description", ""), facts.get("source_text", ""))
    hl = [h for h in out.get("highlights", []) if not (_shingles(h) & _shingles(facts.get("source_text", "")))][:6]
    body = desc + ("\n\n" + "\n".join(f"- {h}" for h in hl) if hl else "")
    trace(db, shop.id, "product_copy_check", engine="rules", input_summary=facts["url"], sources=[facts["url"]],
          output={"phrases_retirees_car_trop_proches_de_la_source": removed})
    return {"title": (out.get("title") or facts["name"])[:200], "description": body.strip(), "highlights": hl,
            "engine": "ai", "removed": removed}
