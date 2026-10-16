"""Aide au choix de la taille, toutes verticales : mesure du client → taille, à partir des seuls guides disponibles.

Chaque ligne de guide porte une borne haute `measure` (ex. longueur de pied max., tour de poignet max.).
La recommandation est la plus petite taille dont la borne couvre « mesure + aisance ». L'incertitude est affichée.
"""
from . import verticals
from .catalog import size_guide_for


def _rows(guide):
    out = []
    for r in guide.rows or []:  # compatibilité : anciens guides chaussures {eu, uk, us, foot_mm}
        m = r.get("measure", r.get("foot_mm"))
        size = r.get("size", r.get("eu"))
        if m not in (None, "") and size not in (None, ""):
            eq = r.get("eq") or (f"UK {r['uk']} / US {r['us']}" if r.get("uk") else "")
            out.append({"size": str(size), "measure": float(m), "eq": eq})
    return sorted(out, key=lambda r: r["measure"])


def advise(db, shop_id, product, measure: float) -> dict:
    vp = verticals.for_product(product)
    fit = vp.get("fit")
    if not fit:
        return {"ok": False, "confidence": "none", "message": "Pas d'aide à la taille pour ce type de produit."}
    guide, level = size_guide_for(db, shop_id, product)
    if guide is None or not _rows(guide):
        return {"ok": False, "confidence": "none",
                "message": "Aucun guide des tailles disponible pour ce produit : nous ne pouvons pas recommander de taille. Contactez-nous pour un conseil."}
    lo, hi = fit["range"]
    if not lo <= measure <= hi:
        return {"ok": False, "confidence": "none", "message": f"Mesure hors plage ({lo}–{hi} {fit['unit']}). {fit['measure_label']}."}
    rows = _rows(guide)
    allowance = guide.allowance if guide.allowance is not None else fit["allowance"]
    target = measure + allowance
    pick = next((r for r in rows if r["measure"] >= target), None)
    if pick is None:
        return {"ok": False, "confidence": "none", "message": "Votre mesure dépasse le guide disponible."}
    idx = rows.index(pick)
    margin = pick["measure"] - target
    available = {v.size for v in product.variants if v.available > 0}
    prefix = vp.get("size_prefix", "")
    conf = "medium" if level == "model" else "low"
    tolerance = 2 if fit["unit"] == "mm" else 1
    if margin < tolerance and idx + 1 < len(rows):
        between = [pick["size"], rows[idx + 1]["size"]]
        msg = f"Entre {prefix}{between[0]} et {prefix}{between[1]} : votre mesure est proche de la limite."
        conf = "low"
    else:
        between = None
        msg = f"{prefix}{pick['size']} recommandé."
    return {"ok": True, "recommended": pick["size"], "between": between, "confidence": conf, "message": msg,
            "basis": f"Guide {'du modèle' if level == 'model' else 'générique de la marque'} {guide.brand} {guide.model}".strip()
                     + (f" (source : {guide.source_url})" if guide.source_url else " (source non renseignée)"),
            "assumption": fit["allowance_note"], "in_stock": pick["size"] in available}
