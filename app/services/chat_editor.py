"""Édition par conversation : chaque demande produit une PROPOSITION (version « proposed ») avec un diff,
appliquée seulement après validation ; retour arrière possible vers n'importe quelle version."""
import copy
import re

from sqlalchemy import select

from ..models import Product
from . import ai, margin, versions
from .catalog import slugify
from .generator import FONTS, PALETTES

NL_UI = {"Ajouter au panier": "In winkelmandje", "Panier": "Winkelmandje", "Rechercher": "Zoeken", "Pointure": "Maat",
         "Couleur": "Kleur", "Prix": "Prijs", "Livraison": "Levering", "Retours": "Retourneren", "En stock": "Op voorraad",
         "Rupture de stock": "Uitverkocht", "Commander": "Bestellen", "Mon compte": "Mijn account", "Contact": "Contact",
         "Guide des tailles": "Maattabel", "Me prévenir du retour en stock": "Verwittig me bij nieuwe voorraad",
         "Homme": "Heren", "Femme": "Dames", "Enfant": "Kinderen", "Questions fréquentes": "Veelgestelde vragen",
         "Taille": "Maat", "Taille de bracelet": "Bandmaat", "Taille de bague": "Ringmaat", "Contenance": "Inhoud", "Teinte": "Tint",
         "Montres": "Horloges", "Bijoux": "Juwelen", "Accessoires": "Accessoires", "Taille du bracelet": "Bandmaat"}


class EditResult(dict):
    pass


def interpret(db, shop, message: str) -> dict:
    """Renvoie {kind, snapshot, summary, notes}. Ne modifie rien."""
    msg = message.strip()
    low = msg.lower()
    snap = versions.snapshot(db, shop)
    new = copy.deepcopy(snap)
    notes = []
    if re.search(r"\b(style|couleur|palette|thème|theme|look)\b", low):
        target = next((s for s in PALETTES if s in slugify(low).replace("-", " ") or s in low), None)
        if not target:
            names = list(PALETTES)
            cur = snap.get("theme", {}).get("style_name", "minimal")
            target = names[(names.index(cur) + 1) % len(names)] if cur in names else "minimal"
        new["theme"].update({"style_name": target, "palette": dict(PALETTES[target]),
                             "font_heading": FONTS[target][0], "font_body": FONTS[target][1]})
        return {"kind": "style", "snapshot": new, "summary": f"Style « {target} » appliqué (palette et typographies)", "notes": notes}
    m = re.search(r"ajoute(?:r)? (?:une )?collection\s+[«\"']?([^»\"']+)[»\"']?", low)
    if m:
        title = m.group(1).strip().rstrip(".").capitalize()
        handle = slugify(title)
        if any(c["handle"] == handle for c in new["collections"]):
            return {"kind": "noop", "snapshot": snap, "summary": f"La collection « {title} » existe déjà", "notes": notes}
        new["collections"].append({"handle": handle, "title": title, "description": "", "rule": {"field": "tag", "value": handle}})
        new["nav"].insert(0, {"label": title, "href": f"/c/{handle}"})
        notes.append(f"Les produits portant l'étiquette « {handle} » y apparaîtront. Ajoutez l'étiquette aux produits concernés.")
        return {"kind": "collection", "snapshot": new, "summary": f"Collection « {title} » ajoutée", "notes": notes}
    if "pack" in low or "bundle" in low or "offre groupée" in low:
        return propose_bundle(db, shop, snap)
    if re.search(r"tradui[st]|néerlandais|neerlandais|nederlands", low):
        return translate_nl(db, shop, snap)
    m = re.search(r"(?:titre|accroche)[^:]*:\s*(.+)$", msg, re.I)
    if m:
        new["home"]["hero_title"] = m.group(1).strip()[:120]
        return {"kind": "copy", "snapshot": new, "summary": "Accroche d'accueil modifiée", "notes": notes}
    return {"kind": "unknown", "snapshot": snap, "summary": "Demande non comprise", "notes": [
        "Exemples : « Change le style en sombre », « Ajoute une collection Trail », « Propose un pack rentable », "
        "« Traduis la boutique en néerlandais », « Titre d'accueil : … »"]}


def propose_bundle(db, shop, snap) -> dict:
    new = copy.deepcopy(snap)
    prods = db.scalars(select(Product).where(Product.shop_id == shop.id, Product.status == "active")).all()
    cands = []
    for p in prods:
        best = None
        for v in p.variants:
            if v.available > 0 and v.price_cents and v.cost_cents is not None:
                ue = margin.unit_economics(db, shop, v)
                if best is None or ue["contribution"] > best[1]["contribution"]:
                    best = (v, ue)
        if best:
            cands.append((p, *best))
    if len(cands) < 2:
        return {"kind": "noop", "snapshot": snap, "summary": "Pack impossible", "notes": [
            "Il faut au moins deux produits en stock avec prix ET coût d'achat renseignés."]}
    cands.sort(key=lambda x: -x[2]["contribution"])
    (p1, v1, u1), (p2, v2, u2) = cands[0], cands[1]
    cs = shop.cost_settings or {}
    # un seul colis au lieu de deux : économie réelle = coût d'un envoi + un emballage
    saving = int(cs.get("shipping_cost_cents") or 0) + int(cs.get("packaging_cents") or 0)
    combined = u1["contribution"] + u2["contribution"] + saving
    max_disc = max(0, combined - round((u1["price_ht"] + u2["price_ht"]) * 0.10))  # garder ≥ 10 % de marge contributive
    price = v1.price_cents + v2.price_cents
    disc_pct = min(10, int(max_disc / price * 100)) if price else 0
    notes = [f"Remise proposée : {disc_pct} % (plafonnée pour conserver ≥ 10 % de marge contributive)."]
    missing = sorted(set(u1["missing"]) | set(u2["missing"]))
    if missing:
        notes.append("Calcul incomplet, données manquantes : " + ", ".join(missing))
    if disc_pct <= 0:
        notes.append("Aucune remise rentable possible : pack proposé sans remise.")
    new.setdefault("bundles", []).append({"title": f"Pack {p1.title} + {p2.title}", "variant_ids": [v1.id, v2.id],
                                          "discount_pct": disc_pct, "est_contribution_cents": combined - round(price * disc_pct / 100)})
    notes.append("Pour l'appliquer au paiement, créez le code de réduction correspondant (validation de la remise requise).")
    return {"kind": "bundle", "snapshot": new, "summary": f"Pack proposé : {p1.title} + {p2.title}", "notes": notes}


def translate_nl(db, shop, snap) -> dict:
    new = copy.deepcopy(snap)
    tr = {"ui": NL_UI, "pages": {}, "home": {}, "status": "à relire par un locuteur natif"}
    pages = {k: {"title": v["title"], "body": v["body"]} for k, v in snap.get("pages", {}).items()}
    out = ai.complete_json(db, shop, "translate_nl",
                           "Traduis en néerlandais (Belgique) le JSON fourni, en conservant les clés, la mise en forme markdown "
                           "et les marqueurs [À COMPLÉTER …] traduits en [AAN TE VULLEN …]. N'ajoute aucune information.",
                           str({"home": snap.get("home", {}), "pages": pages}),
                           {"type": "object", "additionalProperties": False,
                            "properties": {"home": {"type": "object"}, "pages": {"type": "object"}}, "required": ["home", "pages"]})
    notes = []
    if out:
        tr["pages"], tr["home"], tr["engine"] = out.get("pages", {}), out.get("home", {}), "ia"
        notes.append("Traduction automatique : relecture obligatoire avant publication.")
    else:
        tr["engine"] = "glossaire"
        notes.append("Service de traduction IA non configuré : seuls les libellés d'interface sont traduits (glossaire). "
                     "Les pages restent en français jusqu'à traduction et relecture.")
    new.setdefault("translations", {})["nl"] = tr
    langs = list(shop.languages or ["fr"])
    if "nl" not in langs:
        notes.append("La langue NL sera ajoutée à la boutique lors de l'application.")
    return {"kind": "translation", "snapshot": new, "summary": "Version néerlandaise préparée", "notes": notes, "add_language": "nl"}


def propose(db, shop, message: str, user_id=None):
    res = interpret(db, shop, message)
    if res["kind"] in ("unknown", "noop"):
        return None, res
    v = versions.create(db, shop, res["snapshot"], f"{res['summary']} — « {message[:120]} »", user_id, status="proposed")
    v.snapshot = {**v.snapshot, "_meta": {"notes": res["notes"], "add_language": res.get("add_language")}}
    return v, res
