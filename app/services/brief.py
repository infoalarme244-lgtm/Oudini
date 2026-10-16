"""Compréhension de la demande : extraction, hypothèses explicites, questions indispensables uniquement."""
import re
import unicodedata

from . import ai, verticals
from .audit import trace

COUNTRIES = {
    "belgique": "BE", "belgium": "BE", "belgie": "BE", "france": "FR", "luxembourg": "LU", "suisse": "CH",
    "switzerland": "CH", "pays-bas": "NL", "pays bas": "NL", "nederland": "NL", "allemagne": "DE",
    "espagne": "ES", "italie": "IT", "canada": "CA", "quebec": "CA", "benelux": "BENELUX",
}
COUNTRY_NAMES = {"BE": "Belgique", "FR": "France", "LU": "Luxembourg", "CH": "Suisse", "NL": "Pays-Bas",
                 "DE": "Allemagne", "ES": "Espagne", "IT": "Italie", "CA": "Canada"}
CURRENCY = {"CH": "CHF", "CA": "CAD"}
LANGS = {"BE": ["fr", "nl"], "FR": ["fr"], "LU": ["fr", "de"], "CH": ["fr", "de"], "NL": ["nl"], "DE": ["de"],
         "ES": ["es"], "IT": ["it"], "CA": ["fr", "en"]}
LANG_WORDS = {"neerlandais": "nl", "flamand": "nl", "anglais": "en", "allemand": "de", "francais": "fr"}
BRANDS = ["Nike", "Adidas", "New Balance", "Asics", "Puma", "Converse", "Vans", "Salomon", "Hoka", "On Running", "Veja", "Reebok",
          "Jordan", "Saucony", "Le Coq Sportif",
          "Casio", "Seiko", "Tissot", "Swatch", "Fossil", "Daniel Wellington", "Citizen", "Festina", "Garmin", "Hamilton", "Longines",
          "Rolex", "Omega", "Cartier", "Tag Heuer", "Michael Kors", "Guess", "Ray-Ban", "Longchamp", "Pandora", "Swarovski",
          "Samsung", "Apple", "Sony", "JBL", "Levi's", "Carhartt", "The North Face", "Eastpak", "Herschel"]
# Marques connues pour une distribution sélective (indicatif : à vérifier au cas par cas)
SELECTIVE = {"Rolex", "Omega", "Cartier", "Tag Heuer", "Longines", "Hamilton", "Tissot", "Apple"}
POSITIONING = {
    "premium accessible": "premium_accessible", "premium": "premium", "luxe": "luxury", "haut de gamme": "premium",
    "petit prix": "value", "discount": "value", "pas cher": "value", "eco": "eco", "durable": "eco",
    "seconde main": "second_hand", "reconditionne": "second_hand",
}
STYLES = ["minimal", "streetwear", "elegant", "colore", "sombre", "sportif", "nature", "editorial"]
AUDIENCE = {"homme": "homme", "femme": "femme", "enfant": "enfant", "junior": "enfant", "runner": "coureurs",
            "etudiant": "étudiants", "jeune": "18-35 ans"}


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFD", s.lower())
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


def parse_prompt(prompt: str) -> dict:
    t = _norm(prompt)
    countries = []
    for word, code in COUNTRIES.items():
        if re.search(rf"\b{re.escape(word)}\b", t):
            for c in (["BE", "NL", "LU"] if code == "BENELUX" else [code]):
                if c not in countries:
                    countries.append(c)
    categories = verticals.detect(prompt)
    category = categories[0] if categories else None
    brands = [b for b in BRANDS if re.search(rf"\b{re.escape(_norm(b))}\b", t)]
    positioning = next((v for k, v in POSITIONING.items() if k in t), None)
    style = [s for s in STYLES if s in t]
    audience = sorted({v for k, v in AUDIENCE.items() if k in t})
    m = re.search(r"(\d{1,2})\s+concurrents", t)
    n_comp = int(m.group(1)) if m else None
    explicit_langs = sorted({v for k, v in LANG_WORDS.items() if k in t})
    urls = re.findall(r"https?://[^\s,;]+", prompt)
    return {"countries": countries, "category": category, "categories": categories, "brands": brands, "positioning": positioning,
            "style": style, "audience": audience, "competitor_count": n_comp, "explicit_languages": explicit_langs,
            "urls": urls}


def build_assumptions(ex: dict) -> tuple[list, list]:
    """Renvoie (hypothèses, questions). Chaque hypothèse indique son origine : 'demande' ou 'défaut'."""
    A, Q = [], []

    def add(key, label, value, origin):
        A.append({"key": key, "label": label, "value": value, "origin": origin, "editable": True})

    countries = ex["countries"]
    if countries:
        add("countries", "Pays ciblés", countries, "demande")
    else:
        Q.append({"key": "countries", "question": "Dans quels pays souhaitez-vous vendre et livrer ?",
                  "why": "Détermine devise, langues, TVA, livraison et concurrents analysés.", "answer": None})
    currencies = sorted({CURRENCY.get(c, "EUR") for c in countries}) or ["EUR"]
    add("currency", "Devise", currencies[0], "déduit des pays" if countries else "défaut")
    langs = []
    for c in countries or ["FR"]:
        for lg in LANGS.get(c, ["fr"]):
            if lg not in langs:
                langs.append(lg)
    if "BE" in countries and "nl" not in ex["explicit_languages"]:
        Q.append({"key": "languages", "question": "Pour la Belgique, voulez-vous la boutique en français seulement ou en français et néerlandais ?",
                  "why": "La Flandre représente une large part du marché belge ; la traduction a un coût de validation.",
                  "answer": None, "options": ["fr", "fr+nl"]})
        langs = ["fr"] + [l for l in langs if l not in ("fr", "nl")]
    add("languages", "Langues", langs, "déduit des pays")
    cats = ex.get("categories") or ([ex["category"]] if ex["category"] else [])
    if cats:
        add("category", "Catégorie principale", cats[0], "demande")
        add("categories", "Catégories vendues", cats, "demande")
    else:
        Q.append({"key": "category", "question": "Quels produits allez-vous vendre (chaussures, montres, bijoux, accessoires, vêtements, beauté, électronique, maison…) ?",
                  "why": "Structure du catalogue, variantes, filtres, informations obligatoires et concurrents analysés.", "answer": None,
                  "options": [verticals.PROFILES[k]["label"] for k in verticals.PROFILES]})
    if ex["brands"]:
        add("brands", "Marques", ex["brands"], "demande")
        sel = [b for b in ex["brands"] if b in SELECTIVE]
        why = "Détermine ce que la boutique peut affirmer (revendeur indépendant vs distributeur agréé) et les preuves d'authenticité à afficher."
        if sel:
            why += (f" {', '.join(sel)} : marque(s) souvent en distribution sélective — la revente de neuf hors réseau agréé peut être "
                    "contestée et la garantie constructeur peut ne pas s'appliquer.")
        Q.append({"key": "supply", "question": f"Comment vous approvisionnez-vous en {', '.join(ex['brands'])} ?",
                  "why": why,
                  "answer": None,
                  "options": ["Contrat de distribution agréée (justificatif requis)", "Grossiste / fournisseur européen avec factures",
                              "Seconde main / revente", "Pas encore d'approvisionnement"]})
    add("positioning", "Positionnement", ex["positioning"] or "premium_accessible", "demande" if ex["positioning"] else "défaut")
    add("audience", "Clientèle", ex["audience"] or ["adultes 18-45 ans, mixte"], "demande" if ex["audience"] else "défaut")
    add("style", "Style visuel", ex["style"] or ["minimal"], "demande" if ex["style"] else "défaut")
    band = verticals.get(ex["category"])["price_bands"].get(ex["positioning"] or "premium_accessible") if ex["category"] else None
    if band:
        add("price_band", "Fourchette de prix envisagée (TTC)", f"{band[0]}–{band[1]} €",
            "hypothèse à valider avec vos prix d'achat")
    add("competitor_count", "Nombre de concurrents à analyser", ex["competitor_count"] or 10,
        "demande" if ex["competitor_count"] else "défaut")
    return A, Q


def understand(db, shop, prompt: str) -> dict:
    ex = parse_prompt(prompt)
    engine = "rules"
    schema = {"type": "object", "additionalProperties": False,
              "properties": {"audience": {"type": "array", "items": {"type": "string"}},
                             "style": {"type": "array", "items": {"type": "string"}},
                             "notes": {"type": "string"}},
              "required": ["audience", "style", "notes"]}
    extra = ai.complete_json(db, shop, "brief_understanding",
                             "Extrais la clientèle visée et le style souhaité de la demande du commerçant. "
                             "Laisse les tableaux vides si non précisé.", prompt, schema)
    if extra:
        engine = "ai+rules"
        ex["audience"] = ex["audience"] or extra.get("audience", [])
        ex["style"] = ex["style"] or extra.get("style", [])
    assumptions, questions = build_assumptions(ex)
    trace(db, shop.id, "brief_understanding", engine=engine, input_summary=prompt,
          assumptions=assumptions, output={"extracted": ex, "questions": questions})
    return {"extracted": ex, "assumptions": assumptions, "questions": questions}


def assumption(brief, key, default=None):
    for a in brief.assumptions or []:
        if a["key"] == key:
            return a["value"]
    return default
