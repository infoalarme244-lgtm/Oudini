"""Score de préparation au lancement : uniquement des blocages réels et vérifiables."""
from sqlalchemy import select

from ..models import PaymentAccount, Product, ShippingZone, TaxSetting
from . import versions

REQUIRED_LEGAL = {"company_name": "Raison sociale", "address": "Adresse", "registration": "Numéro d'entreprise",
                  "vat_number": "Numéro de TVA", "email": "Email de contact", "host": "Hébergeur"}


def check(db, shop) -> dict:
    items = []

    def add(key, label, ok, blocking=True, detail="", fix=""):
        items.append({"key": key, "label": label, "ok": bool(ok), "blocking": blocking, "detail": detail, "fix": fix})

    accs = db.scalars(select(PaymentAccount).where(PaymentAccount.shop_id == shop.id)).all()
    live = [a for a in accs if a.mode == "live" and a.status == "connected"]
    test = [a for a in accs if a.mode == "test" and a.status == "connected"]
    add("payment_test", "Paiement de test connecté", test, False, fix="/payments")
    add("payment_live", "Paiement réel connecté (Stripe live) et activé", live and shop.payment_mode == "live",
        detail="Compte Stripe live à finaliser par vous (identité, IBAN) puis activation validée", fix="/payments")
    zones = db.scalars(select(ShippingZone).where(ShippingZone.shop_id == shop.id)).all()
    covered = {c for z in zones if z.rates for c in z.countries}
    missing_c = [c for c in shop.countries if c not in covered]
    add("shipping", "Livraison configurée pour chaque pays ciblé", not missing_c,
        detail=("Pays sans tarif : " + ", ".join(missing_c)) if missing_c else "", fix="/shipping")
    suggested = [f"{z.name} ({r.get('name')})" for z in zones for r in z.rates or [] if r.get("suggested")]
    add("shipping_confirmed", "Tarifs de livraison proposés confirmés", not suggested, detail=", ".join(suggested), fix="/lancement")
    incomplete_rates = [r.get("name") for z in zones for r in z.rates or [] if r.get("min_days") in (None, "") or r.get("max_days") in (None, "")]
    add("shipping_delays", "Délais de livraison renseignés", zones and not incomplete_rates, True,
        detail=", ".join(incomplete_rates), fix="/shipping")
    taxes = db.scalars(select(TaxSetting).where(TaxSetting.shop_id == shop.id)).all()
    unvalidated = [t.country for t in taxes if not t.validated]
    no_tax = [c for c in shop.countries if c not in {t.country for t in taxes}]
    add("taxes", "TVA validée pour chaque pays", not unvalidated and not no_tax,
        detail="À valider : " + ", ".join(unvalidated + no_tax) if (unvalidated or no_tax) else "", fix="/taxes")
    products = db.scalars(select(Product).where(Product.shop_id == shop.id, Product.status == "active")).all()
    real = [p for p in products if not p.is_demo]
    demo = [p for p in products if p.is_demo]
    add("catalog", "Au moins un produit réel actif", real, fix="/products")
    add("no_demo", "Aucun produit de démonstration publié", not demo, detail=f"{len(demo)} produit(s) de démo", fix="/products")
    no_price = [p.title for p in real if any(v.price_cents is None for v in p.variants) or not p.variants]
    add("prices", "Tous les produits ont un prix", not no_price, detail=", ".join(no_price[:5]), fix="/products")
    no_stock = [p.title for p in real if sum(v.available for v in p.variants) == 0]
    add("stock", "Stock disponible", not no_stock, False, detail="Sans stock : " + ", ".join(no_stock[:5]) if no_stock else "")
    no_img = [p.title for p in real if not [i for i in p.images or [] if i.get("license")]]
    add("images", "Images avec droits d'utilisation déclarés", not no_img, False, detail=", ".join(no_img[:5]), fix="/products")
    from . import verticals
    missing_req, missing_rec = [], []
    for p in real:
        vp = verticals.for_product(p)
        a = p.attributes or {}
        req = [verticals.attribute_label(vp, k) for k in vp.get("required_attributes", []) if not a.get(k)]
        rec = [verticals.attribute_label(vp, k) for k in vp.get("recommended_attributes", []) if not a.get(k)]
        if req:
            missing_req.append(f"{p.title} ({', '.join(req)})")
        if rec:
            missing_rec.append(f"{p.title} ({', '.join(rec)})")
    add("required_info", "Informations obligatoires selon le type de produit", not missing_req,
        detail="; ".join(missing_req[:5]), fix="/products")
    add("specs", "Caractéristiques techniques complètes", not missing_rec, False, detail="; ".join(missing_rec[:5]), fix="/products")
    no_cost = [p.title for p in real if any(v.cost_cents is None for v in p.variants)]
    add("costs", "Coûts d'achat renseignés (marges)", not no_cost, False, detail=", ".join(no_cost[:5]), fix="/products")
    branded = [p for p in real if p.brand]
    unproven = [p.title for p in branded if not p.authenticity and not p.authenticity_evidence]
    add("authenticity", "Provenance / authenticité étayée pour les produits de marque", not unproven, True,
        detail=", ".join(unproven[:5]), fix="/products")
    lg = shop.legal or {}
    miss = [lbl for k, lbl in REQUIRED_LEGAL.items() if not lg.get(k)]
    if "FR" in shop.countries and not lg.get("mediator"):
        miss.append("Médiateur de la consommation (France)")
    add("legal_info", "Informations légales du commerçant", not miss, detail=", ".join(miss), fix="/settings")
    snap = versions.snapshot(db, shop)
    pages = snap.get("pages", {})
    legal_pending = [p["title"] for p in pages.values() if p.get("legal") and not p.get("validated")]
    add("legal_pages", "Pages légales relues et validées", not legal_pending, detail=", ".join(legal_pending), fix="/content")
    placeholders = [p["title"] for p in pages.values() if "[À COMPLÉTER" in p.get("body", "")]
    add("placeholders", "Aucun texte « À COMPLÉTER » restant", not placeholders, detail=", ".join(placeholders), fix="/content")
    # liens internes : navigation et pied de page
    handles = {c["handle"] for c in snap.get("collections", [])}
    broken = []
    for n in snap.get("nav", []):
        h = n["href"]
        if h.startswith("/c/") and h[3:] not in handles:
            broken.append(h)
        if h.startswith("/p/") and h[3:] not in pages:
            broken.append(h)
    broken += [f"/p/{k}" for k in snap.get("footer_links", []) if k not in pages]
    add("links", "Aucun lien interne cassé", not broken, detail=", ".join(broken))
    add("contact", "Coordonnées de contact affichées", lg.get("email"), detail="Email de contact manquant" if not lg.get("email") else "",
        fix="/settings")
    blocking = [i for i in items if i["blocking"] and not i["ok"]]
    passed = sum(1 for i in items if i["ok"])
    return {"items": items, "score": round(passed / len(items) * 100), "blocking": blocking, "can_publish": not blocking}
