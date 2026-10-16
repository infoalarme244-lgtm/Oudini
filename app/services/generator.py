"""Génération de la boutique : positionnement, identité visuelle, structure de catalogue, pages.

Tout le contenu produit est original. Aucun texte, image ou identité de concurrent n'est réutilisé.
Les pages légales sont préparées à partir des informations RÉELLES du commerçant ; les manques sont
marqués « [À COMPLÉTER] » et listés dans « to_validate ».
"""
import hashlib

from . import verticals
from .brief import COUNTRY_NAMES, assumption

PALETTES = {
    "minimal": {"bg": "#FAFAF7", "surface": "#FFFFFF", "text": "#16161A", "muted": "#5C5C66", "accent": "#1F4DFF", "accent_text": "#FFFFFF", "line": "#E4E4DF"},
    "streetwear": {"bg": "#F4F1EA", "surface": "#FFFFFF", "text": "#111111", "muted": "#555048", "accent": "#E8461E", "accent_text": "#FFFFFF", "line": "#DED8CC"},
    "elegant": {"bg": "#F7F5F2", "surface": "#FFFFFF", "text": "#1C1A17", "muted": "#6B645B", "accent": "#7A5C3E", "accent_text": "#FFFFFF", "line": "#E6E0D8"},
    "sombre": {"bg": "#111214", "surface": "#1B1C20", "text": "#F2F2F0", "muted": "#A6A7AD", "accent": "#C6F432", "accent_text": "#111214", "line": "#2C2D33"},
    "colore": {"bg": "#FFF8EE", "surface": "#FFFFFF", "text": "#1A1423", "muted": "#5D5468", "accent": "#7B2CBF", "accent_text": "#FFFFFF", "line": "#F0E2CC"},
    "sportif": {"bg": "#F3F6F9", "surface": "#FFFFFF", "text": "#0E1A26", "muted": "#4F5D6B", "accent": "#00875A", "accent_text": "#FFFFFF", "line": "#DCE3EA"},
    "nature": {"bg": "#F5F3EC", "surface": "#FFFFFF", "text": "#1E2A1F", "muted": "#5A6658", "accent": "#3F6B3A", "accent_text": "#FFFFFF", "line": "#E1DDCF"},
}
FONTS = {
    "minimal": ("Inter", "Inter"), "streetwear": ("Archivo Black", "Archivo"), "elegant": ("Fraunces", "Inter"),
    "sombre": ("Space Grotesk", "Inter"), "colore": ("Bricolage Grotesque", "Inter"), "sportif": ("Barlow Condensed", "Barlow"),
    "nature": ("Fraunces", "Source Sans 3"),
}
VAT_STANDARD = {"FR": 2000, "BE": 2100, "LU": 1700, "NL": 2100, "DE": 1900, "ES": 2100, "IT": 2200}


def _profiles(brief):
    cats = assumption(brief, "categories") or ([assumption(brief, "category")] if assumption(brief, "category") else [])
    return [verticals.get(c) for c in cats] or [verticals.get(None)]


def positioning_options(brief) -> list[dict]:
    """Trois positionnements : conseil/adéquation (propre à la verticale), transparence, proximité."""
    vp = _profiles(brief)[0]
    countries = assumption(brief, "countries", ["FR"])
    brands = assumption(brief, "brands", [])
    multi = len(countries) > 1
    local = "Bancontact, " if "BE" in countries else ""
    name, promise = vp["fit_promise"]
    proofs = {"footwear": ["Guide des tailles par marque et modèle (sources fournisseurs)", "Outil de pointure basé sur la longueur du pied", "Échange de taille facilité"],
              "watches": ["Fiche technique complète (mouvement, diamètre, étanchéité, verre)", "Taille de bracelet selon le tour de poignet", "Garantie réellement applicable indiquée"],
              "jewelry": ["Métal et poinçon affichés", "Baguier : taille selon le tour de doigt", "Gravure / mise à taille expliquées"],
              "accessories": ["Dimensions et contenance réelles", "Matière exacte", "Photos à l'échelle"],
              "apparel": ["Mesures de chaque pièce", "Coupe expliquée", "Échange de taille facilité"],
              "beauty": ["Liste INCI complète", "Type de peau indiqué", "Aucune allégation non prouvée"],
              "electronics": ["Compatibilité vérifiée", "Fiche technique complète", "Garantie et SAV expliqués"],
              "home": ["Dimensions réelles", "Photos en situation", "Livraison des objets fragiles expliquée"]}.get(vp["key"], ["Fiches complètes", "Conseils d'usage", "Réponses rapides"])
    unit = vp["axes"]["size"].lower()
    opts = [
        {"key": "fit", "name": name, "promise": promise, "target": "Acheteurs en ligne qui hésitent et redoutent les retours.",
         "proofs": proofs, "risk": "Exige des données produit fiables (fournisseurs, mesures)."},
        {"key": "transparency", "name": "Sélection claire, délais honnêtes",
         "promise": f"Une sélection resserrée, le stock réel par {unit}, un délai de livraison affiché avant l'achat et la provenance de chaque article.",
         "target": "Clients qui comparent et se méfient des contrefaçons et des délais flous.",
         "proofs": [f"Stock affiché par {unit}", "Délai par pays visible sur la fiche", "Provenance et justificatif d'authenticité"],
         "risk": "Demande une gestion de stock rigoureuse."},
        {"key": "local", "name": "Le spécialiste proche de chez vous" if not multi else "Rapide et local en " + " & ".join(COUNTRY_NAMES.get(c, c) for c in countries),
         "promise": f"Expédition depuis l'Europe, paiements locaux ({local}carte, PayPal), service client dans votre langue.",
         "target": "Clients qui privilégient la rapidité et les moyens de paiement familiers.",
         "proofs": ["Moyens de paiement locaux", "Service client FR" + ("/NL" if "BE" in countries else ""), "Délais par pays"],
         "risk": "Avantage copiable si les concurrents proposent déjà ces options."},
    ]
    if brands:
        for o in opts:
            o["reseller_note"] = (f"Revendeur indépendant : la boutique n'est ni la boutique officielle ni un distributeur agréé "
                                  f"{', '.join(brands)} (sauf justificatif fourni).")
    for o in opts:
        o["status"] = "hypothèse — à confronter à l'analyse concurrentielle"
    return opts


def visual_identity(brief, seed: str) -> dict:
    styles = assumption(brief, "style", ["minimal"]) or ["minimal"]
    style = next((s for s in styles if s in PALETTES), "minimal")
    ideas = _profiles(brief)[0]["names"]
    idx = int(hashlib.sha1(seed.encode()).hexdigest(), 16) % len(ideas)
    return {"style_name": style, "palette": dict(PALETTES[style]), "font_heading": FONTS[style][0],
            "font_body": FONTS[style][1], "radius": "14px" if style in ("colore", "nature") else "8px",
            "name_suggestions": ideas, "suggested_name": ideas[idx]}


def _rule(field, value):
    return {"field": field, "value": value}


def catalog_structure(brief) -> tuple[list, list]:
    """Collections et filtres. Plusieurs verticales : une collection par verticale + les principales de chacune."""
    profs = _profiles(brief)
    cols, filters = [], []
    if len(profs) > 1:
        for vp in profs:
            cols.append({"handle": catalog_slug(vp["label"]), "title": vp["label"], "description": "", "rule": _rule("category", vp["key"])})
    for vp in profs:
        per = vp["collections"] if len(profs) == 1 else vp["collections"][:3]
        for handle, title, field, value in per:
            h = handle if len(profs) == 1 else f"{handle}-{vp['key']}"
            title = title if len(profs) == 1 else f"{vp['label']} — {title}"
            rule = _rule(field, value) if len(profs) == 1 or field == "all" else {"field": field, "value": value, "category": vp["key"]}
            if not any(c["handle"] == h for c in cols):
                cols.append({"handle": h, "title": title, "description": "", "rule": rule})
        for f in vp["filters"]:
            if f not in filters:
                filters.append(f)
    return cols, filters


def catalog_slug(s):
    from .catalog import slugify
    return slugify(s)


def _l(legal: dict, key: str, label: str, missing: list) -> str:
    v = (legal or {}).get(key)
    if v:
        return str(v)
    missing.append(label)
    return f"[À COMPLÉTER : {label}]"


def legal_pages(shop, vertical_keys=None) -> dict:
    lg = shop.legal or {}
    countries = shop.countries or ["FR"]
    pages = {}
    m = []
    company = _l(lg, "company_name", "raison sociale", m)
    body = (f"## Éditeur\n{company}, {_l(lg, 'legal_form', 'forme juridique', m)}\n\n"
            f"Siège : {_l(lg, 'address', 'adresse du siège', m)}\n\n"
            f"Numéro d'entreprise : {_l(lg, 'registration', 'n° RCS/SIREN (FR) ou BCE/KBO (BE)', m)}\n\n"
            f"TVA intracommunautaire : {_l(lg, 'vat_number', 'n° de TVA', m)}\n\n"
            f"Contact : {_l(lg, 'email', 'email de contact', m)} — {_l(lg, 'phone', 'téléphone', m)}\n\n"
            f"Directeur de la publication : {_l(lg, 'publisher', 'directeur de la publication', m)}\n\n"
            f"## Hébergeur\n{_l(lg, 'host', 'hébergeur (nom, adresse, téléphone)', m)}\n")
    pages["mentions-legales"] = {"title": "Mentions légales", "body": body, "legal": True,
                                 "to_validate": m + ["Relecture par un professionnel du droit"]}
    m2 = []
    company2 = _l(lg, "company_name", "raison sociale", m2)
    mediator = _l(lg, "mediator", "médiateur de la consommation (obligatoire pour la France)", m2) if "FR" in countries else ""
    body = (f"## Objet\nLes présentes conditions régissent les ventes conclues sur cette boutique, éditée par {company2}.\n\n"
            "## Prix\nLes prix sont indiqués en euros, toutes taxes comprises. Les frais de livraison sont affichés avant la validation de la commande.\n\n"
            "## Droit de rétractation\nVous disposez de 14 jours à compter de la réception pour vous rétracter, sans avoir à vous justifier. "
            "Les modalités et le formulaire type sont décrits sur la page Retours.\n\n"
            "## Garanties légales\nLes produits bénéficient de la garantie légale de conformité (2 ans) et de la garantie des vices cachés.\n\n"
            + (f"## Médiation\nEn cas de litige : {mediator}\n\n" if mediator else "")
            + "## Statut de revendeur\n" + (lg.get("reseller_statement") or
              "Cette boutique est un revendeur indépendant. Elle n'est pas la boutique officielle des marques présentées, sauf mention contraire justifiée.") + "\n")
    pages["cgv"] = {"title": "Conditions générales de vente", "body": body, "legal": True,
                    "to_validate": m2 + ["Conditions spécifiques par pays (FR / BE)", "Relecture par un professionnel du droit"]
                    + [c for k in (vertical_keys or []) for c in verticals.get(k)["compliance"]]}
    m3 = []
    body = (f"## Responsable du traitement\n{_l(lg, 'company_name', 'raison sociale', m3)} — {_l(lg, 'email', 'email de contact', m3)}\n\n"
            "## Données collectées\nIdentité, coordonnées, adresse de livraison, historique de commandes. Les données de carte bancaire sont "
            "traitées exclusivement par notre prestataire de paiement et ne sont jamais stockées par la boutique.\n\n"
            "## Finalités et bases légales\nExécution des commandes (contrat), obligations comptables (obligation légale), "
            "emails marketing (consentement, révocable à tout moment via le lien de désabonnement).\n\n"
            "## Vos droits\nAccès, rectification, effacement, portabilité, opposition. Réclamation possible auprès de l'autorité de "
            "protection des données (CNIL en France, APD/GBA en Belgique).\n")
    pages["confidentialite"] = {"title": "Politique de confidentialité", "body": body, "legal": True,
                                "to_validate": m3 + ["Durées de conservation", "Liste des sous-traitants (hébergeur, paiement, email)"]}
    pages["cookies"] = {"title": "Cookies", "legal": True, "to_validate": ["Liste exacte des cookies utilisés"],
                        "body": "## Cookies utilisés\nCette boutique n'utilise que des cookies strictement nécessaires (panier, session, "
                                "sécurité). Aucun cookie publicitaire n'est déposé sans votre consentement.\n"}
    return pages


def service_pages(shop, brands: list, fit=None) -> dict:
    reseller = ("Nous sommes un revendeur indépendant. Nous ne sommes ni la boutique officielle ni un distributeur agréé "
                + ", ".join(brands) + ". Chaque fiche produit indique la provenance déclarée de la paire.") if brands else ""
    return {
        "faq": {"title": "Questions fréquentes", "to_validate": ["Réponses conformes à vos pratiques réelles"],
                "body": (f"## {fit['label']} : comment choisir ?\nChaque fiche propose le guide disponible pour la marque ou le modèle, "
                         f"et un conseil à partir de votre mesure ({fit['measure_label'].lower()}). Le niveau de certitude est toujours affiché.\n\n"
                         if fit else "## Comment choisir ?\nChaque fiche détaille les caractéristiques réelles du produit. En cas de doute, écrivez-nous.\n\n")
                        + "## Quand vais-je recevoir ma commande ?\nLe délai estimé pour votre pays est indiqué sur la fiche produit et au paiement.\n\n"
                        "## Puis-je échanger un article ?\nOui, voir la page Retours.\n\n"
                        + (f"## Les produits sont-ils authentiques ?\n{reseller}\n" if reseller else "")},
        "livraison": {"title": "Livraison", "dynamic": "shipping", "to_validate": ["Tarifs et délais saisis dans Livraison"],
                      "body": "Les zones, tarifs, seuils de gratuité et délais ci-dessous sont ceux configurés par la boutique.\n"},
        "retours": {"title": "Retours et échanges", "to_validate": ["Frais de retour à la charge de qui ?", "Adresse de retour"],
                    "body": "## Délai\nVous disposez de 14 jours après réception pour nous retourner un article non porté, dans son emballage d'origine.\n\n"
                            "## Échange de taille\nDemandez un échange depuis votre compte ou via la page Contact.\n\n"
                            "## Remboursement\nLe remboursement est effectué sur le moyen de paiement utilisé, au plus tard 14 jours après réception du retour.\n"},
        "contact": {"title": "Contact", "dynamic": "contact", "to_validate": ["Coordonnées réelles"],
                    "body": "Une question sur un produit, une commande ou un retour ? Écrivez-nous.\n"},
        "a-propos": {"title": "À propos", "to_validate": ["Histoire et engagements réels de la boutique"],
                     "body": "[À COMPLÉTER : qui vous êtes, pourquoi cette boutique, vos engagements vérifiables.]\n\n" + reseller + "\n"},
    }


def initial_snapshot(shop, brief) -> dict:
    opts = brief.positioning_options or positioning_options(brief)
    chosen = opts[brief.chosen_positioning or 0]
    identity = visual_identity(brief, shop.slug)
    collections, filters = catalog_structure(brief)
    brands = assumption(brief, "brands", []) or []
    profs = _profiles(brief)
    fit = next((vp["fit"] for vp in profs if vp.get("fit")), None)
    pages = {**service_pages(shop, brands, fit), **legal_pages(shop, [vp["key"] for vp in profs])}
    nav = [{"label": c["title"], "href": f"/c/{c['handle']}"} for c in collections[:5]]
    nav += ([{"label": fit["label"], "href": "/p/faq"}] if fit else []) + [{"label": "Contact", "href": "/p/contact"}]
    return {
        "theme": {k: identity[k] for k in ("style_name", "palette", "font_heading", "font_body", "radius")},
        "brand": {"name": shop.name, "tagline": chosen["promise"].split(":")[0].split(".")[0], "tone": "direct, chaleureux, précis"},
        "positioning": chosen,
        "home": {"hero_title": chosen["name"], "hero_subtitle": chosen["promise"],
                 "value_props": chosen["proofs"],
                 "featured_collections": [c["handle"] for c in collections[:3]],
                 "reseller_notice": chosen.get("reseller_note", "")},
        "collections": collections, "filters": filters, "nav": nav, "pages": pages,
        "footer_links": ["livraison", "retours", "faq", "contact", "a-propos", "cgv", "mentions-legales", "confidentialite", "cookies"],
        "translations": {}, "bundles": [], "verticals": [vp["key"] for vp in profs],
    }


def default_tax_settings(countries):
    return [{"country": c, "rate_bp": VAT_STANDARD[c],
             "note": "Taux normal de TVA pré-rempli : à valider (taux réduits, régime OSS au-delà de 10 000 € de ventes UE à distance)."}
            for c in countries if c in VAT_STANDARD]
