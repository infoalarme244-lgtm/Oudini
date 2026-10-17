"""Pilote automatique du lancement : tout est pré-rempli, le commerçant relit et valide.

Restent à sa charge uniquement : les photos du produit, la connexion du compte bancaire (chez Stripe), et la
validation de ce qui a été proposé. Rien n'est inventé : chaque valeur pré-remplie indique d'où elle vient
(registre public, analyse des concurrents, valeur de marché proposée) et reste modifiable.
"""
import re
import statistics
from datetime import datetime, timezone

import httpx
from sqlalchemy import select

from ..config import settings
from ..models import CompetitorAnalysis, Product, ShippingZone, SourcedFact, TaxSetting
from . import payments, readiness, verticals, versions
from .audit import audit, trace

# Tarifs proposés par défaut (ordre de grandeur du marché pour un colis standard) — à ajuster par le commerçant.
SHIPPING_DEFAULTS = {"BE": (490, 1, 3), "FR": (590, 2, 4), "LU": (690, 2, 4), "NL": (590, 2, 4), "DE": (690, 2, 5)}
SHIPPING_FALLBACK = (890, 3, 7)
FR_FORMS = {"1000": "Entreprise individuelle", "5499": "SARL", "5498": "EURL", "5710": "SAS", "5720": "SASU", "5599": "SA",
            "5202": "SNC", "6540": "SCI"}
BE_FORMS = ("SRL", "BV", "SA", "NV", "SC", "CV", "SNC", "VOF", "ASBL", "VZW", "SPRL", "BVBA")


# ---------------------------------------------------------------- 1. pré-remplissage à la génération
def autofill(db, shop, user) -> dict:
    """Appelé après la génération : livraison proposée, coordonnées connues, hébergeur de la plateforme."""
    done = {}
    for z in db.scalars(select(ShippingZone).where(ShippingZone.shop_id == shop.id)):
        if not z.rates:
            price, mn, mx = SHIPPING_DEFAULTS.get((z.countries or [""])[0], SHIPPING_FALLBACK)
            z.rates = [{"id": "std", "name": "Livraison standard", "carrier": "", "price_cents": price, "free_over_cents": None,
                        "min_days": mn, "max_days": mx, "cost_cents": None, "suggested": True}]
            done.setdefault("shipping", []).append(z.name)
    legal = dict(shop.legal or {})
    for key, value in (("email", user.email), ("publisher", user.name), ("host", settings.platform_legal_host)):
        if value and not legal.get(key):
            legal[key] = value
            done.setdefault("legal", []).append(key)
    shop.legal = legal
    trace(db, shop.id, "autopilot", input_summary="pré-remplissage automatique", output=done,
          assumptions=[{"livraison": "tarifs de marché proposés, à confirmer"}, {"coordonnées": "email et nom du compte"}])
    return done


# ---------------------------------------------------------------- 2. entreprise depuis les registres publics
class LookupError_(Exception):
    pass


def normalise_company_number(raw: str) -> tuple[str, str]:
    s = re.sub(r"[\s.\-/]", "", (raw or "").upper())
    if s.startswith("BE"):
        s = s[2:]
        country = "BE"
    elif s.startswith("FR") and len(s) == 13:
        return "FR", s[4:]
    else:
        country = ""
    if not s.isdigit():
        raise LookupError_("Numéro invalide : chiffres uniquement (TVA, BCE/KBO ou SIREN)")
    if country == "BE" or len(s) == 10 or (len(s) == 9 and country == "BE"):
        s = s.zfill(10)
        if 97 - int(s[:8]) % 97 != int(s[8:]):
            raise LookupError_("Numéro d'entreprise belge invalide (clé de contrôle)")
        return "BE", s
    if len(s) == 14:
        s = s[:9]  # SIRET → SIREN
    if len(s) == 9:
        digits = [int(c) for c in s]
        total = sum(d if i % 2 == 0 else (d * 2 - 9 if d * 2 > 9 else d * 2) for i, d in enumerate(digits))
        if total % 10:
            raise LookupError_("Numéro SIREN invalide (clé de contrôle)")
        return "FR", s
    raise LookupError_("Format non reconnu : 10 chiffres (Belgique) ou 9 chiffres SIREN (France)")


def fr_vat(siren: str) -> str:
    return f"FR{(12 + 3 * (int(siren) % 97)) % 97:02d}{siren}"


def lookup_company(raw: str, transport=None) -> dict:
    """Interroge les registres publics (gratuit, sans clé) : VIES (Belgique) ou Recherche d'entreprises (France)."""
    country, num = normalise_company_number(raw)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    with httpx.Client(timeout=10, transport=transport, headers={"Accept": "application/json"}) as c:
        if country == "BE":
            url = f"https://ec.europa.eu/taxation_customs/vies/rest-api/ms/BE/vat/{num}"
            try:
                r = c.get(url)
                d = r.json() if r.status_code == 200 else {}
            except (httpx.HTTPError, ValueError) as exc:
                raise LookupError_("Registre européen (VIES) injoignable : réessayez ou remplissez à la main") from exc
            if not d.get("isValid"):
                raise LookupError_("Numéro non trouvé ou non assujetti à la TVA dans le registre européen")
            name = (d.get("name") or "").strip()
            form = next((f for f in BE_FORMS if re.search(rf"\b{f}\b", name.upper())), "")
            return {"company_name": name, "legal_form": form, "address": re.sub(r"\s*\n\s*", ", ", (d.get("address") or "").strip()),
                    "registration": f"BCE {num[:4]}.{num[4:7]}.{num[7:]}", "vat_number": f"BE{num}",
                    "source": f"Registre européen VIES — {url}", "retrieved_at": now}
        url = f"https://recherche-entreprises.api.gouv.fr/search?q={num}&page=1&per_page=1"
        try:
            r = c.get(url)
            res = (r.json() or {}).get("results") or [] if r.status_code == 200 else []
        except (httpx.HTTPError, ValueError) as exc:
            raise LookupError_("Annuaire des entreprises injoignable : réessayez ou remplissez à la main") from exc
        e = next((x for x in res if x.get("siren") == num), None)
        if not e:
            raise LookupError_("SIREN introuvable dans l'annuaire des entreprises")
        siege = e.get("siege") or {}
        return {"company_name": e.get("nom_complet") or e.get("nom_raison_sociale") or "",
                "legal_form": FR_FORMS.get(str(e.get("nature_juridique", "")), ""),
                "address": siege.get("adresse") or "", "registration": f"SIREN {num[:3]} {num[3:6]} {num[6:]}",
                "vat_number": fr_vat(num), "source": f"Annuaire des entreprises (data.gouv.fr) — {url}", "retrieved_at": now}


# ---------------------------------------------------------------- 3. prix suggéré
def price_suggestion(db, shop, product) -> dict:
    """Prix observé sur la page source si le produit vient d'un lien ; sinon médiane des concurrents analysés ;
    à défaut, milieu de la fourchette du positionnement."""
    from ..models import Brief
    b = db.scalar(select(Brief).where(Brief.shop_id == shop.id).order_by(Brief.id.desc()))
    src = ((b.extracted or {}).get("source_product") or {}) if b else {}
    if src.get("price") and (src.get("currency") or "EUR").upper() == "EUR":
        return {"cents": int(round(float(src["price"]) * 100)),
                "basis": f"prix observé sur {src['domain']} le {src['retrieved_at']} — à ajuster selon vos coûts"}
    a = db.scalar(select(CompetitorAnalysis).where(CompetitorAnalysis.shop_id == shop.id).order_by(CompetitorAnalysis.id.desc()))
    medians = []
    if a:
        for f in db.scalars(select(SourcedFact).where(SourcedFact.shop_id == shop.id, SourcedFact.method == "json_ld",
                                                      SourcedFact.label.like("Prix observés%"))):
            m = re.search(r"médiane (\d+)", f.value)
            if m:
                medians.append(int(m.group(1)))
    if len(medians) >= 2:
        v = statistics.median(medians)
        basis = f"médiane des prix observés chez {len(medians)} concurrents analysés"
    else:
        lo, hi = verticals.get(product.category or None)["price_bands"]["premium_accessible"]
        v = (lo + hi) / 2
        basis = "milieu de la fourchette de prix du positionnement (aucun prix concurrent fiable) — à confirmer"
    cents = max(100, int(round(v)) * 100 - 10)  # 129 → 128,90 €
    return {"cents": cents, "basis": basis}


# ---------------------------------------------------------------- 4. étapes
def steps(db, shop) -> list[dict]:
    real = [p for p in db.scalars(select(Product).where(Product.shop_id == shop.id)) if not p.is_demo]
    sellable = [p for p in real if p.status == "active" and any(v.price_cents and v.stock > 0 for v in p.variants)]
    lg = shop.legal or {}
    need = ["company_name", "address", "registration", "vat_number", "email", "phone"] + (["mediator"] if "FR" in shop.countries else [])
    acc = payments.connect_account(db, shop) if payments.platform_mode() else None
    legacy = any(i["key"] == "payment_test" and i["ok"] for i in readiness.check(db, shop)["items"]) and not acc
    pay_status = acc.status if acc else ("legacy" if legacy else "none")
    return [
        {"n": 1, "key": "produit", "title": "Votre produit", "done": bool(sellable),
         "hint": f"{len(sellable)} produit(s) prêt(s) à la vente" if sellable else "Photo, prix et stock à confirmer"},
        {"n": 2, "key": "entreprise", "title": "Votre entreprise", "done": all(lg.get(k) for k in need),
         "hint": lg.get("company_name") or "Votre numéro d'entreprise suffit"},
        {"n": 3, "key": "paiements", "title": "Recevoir les paiements", "done": pay_status == "connected",
         "hint": {"connected": "Compte bancaire connecté", "pending_verification": "Vérification en cours chez Stripe",
                  "onboarding": "Inscription Stripe à terminer", "legacy": "Mode test (simulateur ou clés)",
                  "none": "Connectez votre compte bancaire"}[pay_status], "status": pay_status},
        {"n": 4, "key": "publier", "title": "Vérifier et publier", "done": shop.status == "published",
         "hint": "En ligne" if shop.status == "published" else "Relire ce qui a été préparé, puis publier"},
    ]


def recap(db, shop) -> list[dict]:
    """Ce qui a été préparé automatiquement, présenté pour une validation en un clic."""
    out = []
    for z in db.scalars(select(ShippingZone).where(ShippingZone.shop_id == shop.id)):
        for r in z.rates or []:
            out.append({"kind": "Livraison", "label": f"{z.name} — {r['name']} : {r['price_cents']/100:.2f} €, {r['min_days']}–{r['max_days']} jours",
                        "pending": bool(r.get("suggested")), "fix": "/shipping"})
    for t in db.scalars(select(TaxSetting).where(TaxSetting.shop_id == shop.id)):
        out.append({"kind": "TVA", "label": f"{t.country} : {t.rate_bp/100:g} % (taux normal)", "pending": not t.validated, "fix": "/taxes"})
    snap = versions.snapshot(db, shop)
    for k, p in snap.get("pages", {}).items():
        if p.get("legal"):
            missing = "[À COMPLÉTER" in p.get("body", "")
            out.append({"kind": "Page légale", "label": p["title"] + (" — informations manquantes" if missing else ""),
                        "pending": not p.get("validated"), "blocked": missing, "fix": f"/content/pages/{k}"})
    out.append({"kind": "Langues", "label": ", ".join(l.upper() for l in shop.languages), "pending": False, "fix": "/brief"})
    return out


def confirm_all(db, shop, user) -> list[str]:
    """Validation en un clic de tout ce qui a été proposé. Renvoie les blocages restants (rien n'est validé s'il en reste)."""
    snap = versions.snapshot(db, shop)
    snap["pages"].update(_legal_pages(shop, snap))
    about = snap["pages"].get("a-propos")
    if about and "[À COMPLÉTER" in about.get("body", ""):
        about["body"] = about_text(shop, snap)
        about["auto"] = True
    blockers = [p["title"] for p in snap["pages"].values() if p.get("legal") and "[À COMPLÉTER" in p.get("body", "")]
    if blockers:
        return [f"Informations manquantes dans : {', '.join(blockers)} (étape 2)"]
    for p in snap["pages"].values():
        if p.get("legal"):
            p["validated"], p["validated_by"] = True, user.email
    versions.create(db, shop, snap, "Validation du lancement (pages légales relues)", user.id)
    for z in db.scalars(select(ShippingZone).where(ShippingZone.shop_id == shop.id)):
        z.rates = [{**r, "suggested": False} for r in z.rates or []]
    for t in db.scalars(select(TaxSetting).where(TaxSetting.shop_id == shop.id)):
        t.validated = True
    audit(db, shop.id, user.id, "launch.confirmed", "shop", shop.id,
          after={"recap": [r["label"] for r in recap(db, shop)]})
    return []


def about_text(shop, snap) -> str:
    """Page « À propos » factuelle (aucune histoire inventée), à enrichir par le commerçant."""
    lg = shop.legal or {}
    labels = [verticals.get(k)["label"].lower() for k in snap.get("verticals") or [] if k in verticals.PROFILES]
    lines = [f"## {shop.name}",
             f"{shop.name} est une boutique en ligne éditée par {lg.get('company_name', '')}"
             + (f", {lg['address']}" if lg.get("address") else "") + "."]
    if labels:
        lines.append(f"Nous proposons : {', '.join(labels)}.")
    lines.append("Livraison : " + ", ".join(shop.countries) + ". Une question ? Écrivez-nous depuis la page Contact.")
    notice = snap.get("home", {}).get("reseller_notice")
    if notice:
        lines.append(notice)
    return "\n\n".join(lines) + "\n"


def _legal_pages(shop, snap):
    from .generator import legal_pages
    return legal_pages(shop, snap.get("verticals"))
