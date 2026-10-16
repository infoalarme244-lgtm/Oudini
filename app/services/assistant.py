"""Assistant d'achat : répond UNIQUEMENT à partir du catalogue, des stocks et des politiques réels.
En cas de doute, il le dit et transfère la question au commerçant."""
import re

from sqlalchemy import select

from ..models import ShippingZone, SupportTicket
from . import catalog
from .catalog import slugify

ESCALATE = ("Je n'ai pas d'information fiable pour répondre à cette question. Je l'ai transmise à l'équipe de la boutique, "
            "qui vous répondra par e-mail.")


def answer(db, shop, snapshot: dict, question: str, email: str = "") -> dict:
    q = question.strip()
    low = slugify(q).replace("-", " ")
    products = catalog.sellable(db, shop.id)
    sources = []
    # livraison
    if re.search(r"\b(livraison|livre|delai|expedi|frais de port|shipping|levering)\b", low):
        zones = db.scalars(select(ShippingZone).where(ShippingZone.shop_id == shop.id)).all()
        if not zones:
            return _escalate(db, shop, q, email)
        lines = []
        for z in zones:
            for r in z.rates or []:
                free = f", offerte dès {r['free_over_cents']/100:.0f} €" if r.get("free_over_cents") else ""
                lines.append(f"{z.name} ({', '.join(z.countries)}) — {r['name']} : {int(r['price_cents'])/100:.2f} €{free}, "
                             f"{r.get('min_days', '?')}–{r.get('max_days', '?')} jours ouvrés")
        return {"answer": "Options de livraison configurées :\n" + "\n".join(lines), "sources": ["configuration livraison"], "escalated": False}
    if re.search(r"\b(retour|rembours|echange)\b", low):
        page = snapshot.get("pages", {}).get("retours")
        if not page:
            return _escalate(db, shop, q, email)
        return {"answer": page["body"].split("\n\n")[1] if "\n\n" in page["body"] else page["body"],
                "sources": ["page Retours"], "escalated": False, "link": "/p/retours"}
    # filtres extraits de la question — vocabulaire tiré du catalogue réel (toutes verticales)
    size = (re.search(r"\b(?:taille|pointure|eu|t)\s*(\d{2}(?:[.,]5)?|xxs|xs|s|m|l|xl|xxl)\b", low) or re.search(r"\ben (\d{2})\b", low))
    size = size.group(1).replace(",", ".").upper() if size else ""
    sizes_known = {v.size.upper(): v.size for p in products for v in p.variants if v.size}
    size = sizes_known.get(size, size)
    budget = re.search(r"(?:moins de|max(?:imum)?|budget|sous)\s*(\d{2,4})", low)
    synonyms = {"course": "running", "ville": "lifestyle", "basket": "basketball", "plongee": "plongee", "habille": "habillee",
                "connectee": "connectee", "boucles d oreilles": "boucles"}
    usages = {slugify(p.usage) for p in products if p.usage}
    usage = next((u for u in sorted(usages, key=len, reverse=True) if u and re.search(rf"\b{re.escape(u.replace('-', ' '))}s?\b", low)), "")
    if not usage:
        usage = next((v for k, v in synonyms.items() if re.search(rf"\b{k}s?\b", low) and v in usages), "")
    gender = next((g for g in ("homme", "femme", "enfant") if g in low), "")
    colors = {slugify(v.color) for p in products for v in p.variants if v.color}
    color = next((c for c in colors if c and c.replace("-", " ") in low), "")
    attrs = {}
    for p in products:
        for k, val in (p.attributes or {}).items():
            sv = slugify(str(val))
            if len(sv) >= 4 and not sv.isdigit() and re.search(rf"\b{re.escape(sv.replace('-', ' '))}\b", low):
                attrs[k] = str(val)
    if re.search(r"\b(authenti|contrefa|faux|original|officiel)\b", low):
        notice = snapshot.get("home", {}).get("reseller_notice") or ""
        if not notice:
            return _escalate(db, shop, q, email)
        return {"answer": notice + " La provenance déclarée figure sur chaque fiche produit.", "sources": ["mention revendeur"], "escalated": False}
    if not any([size, budget, usage, gender, color, attrs]) and not re.search(r"\b(stock|dispo|recommand|conseil|cherche|veux|besoin)\b", low):
        return _escalate(db, shop, q, email)
    found = catalog.search(products, size=size, color=color, usage=usage, gender=gender, attrs=attrs,
                           price_max=int(budget.group(1)) * 100 if budget else None, in_stock=True)
    crit = ", ".join(x for x in [f"taille {size}" if size else "", usage, gender, color.replace('-', ' ') if color else "",
                                 *[str(v) for v in attrs.values()],
                                 f"≤ {budget.group(1)} €" if budget else ""] if x) or "vos critères"
    if not found:
        return {"answer": f"Aucun produit en stock ne correspond à {crit} actuellement. Vous pouvez activer une alerte de retour en stock sur la fiche produit.",
                "sources": ["catalogue et stocks en temps réel"], "escalated": False, "products": []}
    items = []
    for p in found[:4]:
        vs = [v for v in p.variants if v.available > 0 and (not size or v.size == size)]
        items.append({"title": p.title, "handle": p.handle, "price": min(v.price_cents for v in vs) / 100,
                      "sizes": sorted({v.size for v in vs}), "is_demo": p.is_demo})
    return {"answer": f"{len(found)} produit(s) en stock pour {crit} :", "products": items,
            "sources": ["catalogue et stocks en temps réel"], "escalated": False}


def _escalate(db, shop, q, email):
    db.add(SupportTicket(shop_id=shop.id, email=email, question=q[:2000], context={"via": "assistant"}))
    return {"answer": ESCALATE, "sources": [], "escalated": True}
