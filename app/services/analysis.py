"""Analyse concurrentielle sourcée.

- Sélection : concurrents fournis par le commerçant, ou liste indicative de candidats visibles et pertinents
  (jamais présentée comme « ceux qui vendent le plus »).
- Chaque fait porte une URL source, une date de collecte, une méthode et un niveau de confiance.
- L'absence d'une information sur les pages analysées est formulée « non observé », jamais « absent ».
- Les chiffres financiers sont « non disponibles » tant qu'une source vérifiable n'est pas saisie.
- Les simulations sont stockées et affichées séparément des données factuelles.
"""
import re
import statistics
from collections import Counter
from urllib.parse import quote, urlparse

from sqlalchemy import select

from ..models import (Competitor, CompetitorAnalysis, FinancialFigure, Recommendation, ReviewInsight, Shop, Simulation,
                      SourcedFact, now)
from . import ai, verticals
from .audit import trace
from .extractor import detect_injection, extract_page, neutralise
from .jobs import handler
from .url_guard import FetchBlocked, SafeFetcher, UnsafeURL

SELECTION_CRITERIA = [
    "Vend la même catégorie de produits (et, si précisé, les mêmes marques)",
    "Livre dans au moins un des pays ciblés, site disponible dans une langue ciblée",
    "Visible publiquement (site accessible, présence observable) — aucun classement par ventes n'est affirmé",
    "Diversité de modèles : spécialistes, généralistes, marque en direct, acteurs locaux",
]

PROMO_RE = re.compile(r"(soldes|black friday|promo(tion)?s?|code promo|-\s?\d{1,2}\s?%|jusqu'à\s?-?\d{1,2}\s?%|outlet|déstockage)", re.I)
BUNDLE_RE = re.compile(r"(\bpack\b|\blot de\b|2 pour|deux pour|bundle|offre groupée)", re.I)
PRODUCT_LINK = re.compile(r"/(p|product|products|produit|produits|pd|sneakers?|chaussures?|baskets?|schoenen|t)/", re.I)

REVIEW_THEMES = {
    "taille": ["taille", "pointure", "trop petit", "trop grand", "taille petit", "taille grand", "size", "maat"],
    "description": ["pas conforme", "non conforme", "différent de la photo", "pas comme sur la photo", "description"],
    "livraison": ["livraison", "livré", "délai", "colis", "retard", "delivery", "levering"],
    "retours": ["retour", "rembours", "refund", "return", "terugbetaling"],
    "service_client": ["service client", "sav", "réponse", "support", "joindre", "klantenservice"],
    "authenticite": ["faux", "contrefaçon", "authentique", "fake", "original", "namaak"],
    "qualite": ["qualité", "défaut", "abîmé", "usé", "colle", "quality", "kwaliteit"],
    "prix": ["prix", "cher", "promo", "price"],
    "stock": ["rupture", "annulé", "annulée", "plus en stock", "indisponible", "out of stock"],
    "emballage": ["boîte", "emballage", "carton", "box"],
}
NEG = ["pas ", "jamais", "déçu", "problème", "lent", "retard", "horrible", "arnaque", "cassé", "mauvais", "trop petit",
       "trop grand", "aucune", "toujours pas", "inadmissible", "fuir", "nul", "abîmé", "faux", "annulé", "never", "bad", "slecht"]
POS = ["rapide", "parfait", "top", "super", "conforme", "excellent", "merci", "génial", "satisfait", "recommande", "great", "snel"]


def start_analysis(db, shop, brief, competitors: list[dict]) -> CompetitorAnalysis:
    a = CompetitorAnalysis(shop_id=shop.id, brief_id=brief.id if brief else None, selection_criteria=SELECTION_CRITERIA)
    db.add(a)
    db.flush()
    for c in competitors:
        db.add(Competitor(shop_id=shop.id, analysis=a, name=c["name"][:160], url=c["url"], origin=c.get("origin", "merchant"),
                          selection_reason=c.get("reason", "")))
    db.flush()
    return a


def candidates_for(categories) -> list[dict]:
    """Candidats indicatifs, alternés entre les verticales vendues, sans doublon de domaine."""
    cats = [categories] if isinstance(categories, str) or categories is None else list(categories)
    lists = [verticals.get(c)["candidates"] for c in cats if c]
    out, seen = [], set()
    for i in range(max((len(l) for l in lists), default=0)):
        for l in lists:
            if i < len(l):
                n, u, r = l[i]
                dom = urlparse(u).netloc.replace("www.", "").split(".")[0]
                if dom not in seen:
                    seen.add(dom)
                    out.append({"name": n, "url": u, "reason": r, "origin": "candidate_list"})
    return out


def _fact(db, comp, dimension, label, value, url, method="html_extraction", status="observed", confidence="medium"):
    f = SourcedFact(shop_id=comp.shop_id, competitor_id=comp.id, dimension=dimension, label=label, value=str(value)[:1000],
                    source_url=url, retrieved_at=now(), method=method, status=status, confidence=confidence)
    db.add(f)
    return f


def analyse_competitor(db, shop, comp: Competitor, fetcher: SafeFetcher, max_pages: int = 6):
    try:
        home = fetcher.fetch(comp.url)
    except (UnsafeURL, FetchBlocked) as exc:
        comp.status, comp.error = "blocked", str(exc)
        return
    except Exception as exc:  # réseau
        comp.status, comp.error = "failed", f"Inaccessible : {type(exc).__name__}"
        return
    pages = [extract_page(home.url, home.text)]
    queue = list(pages[0]["policy_links"])[:3]
    host = urlparse(home.url).netloc
    from bs4 import BeautifulSoup
    prod_links = [a["href"] for a in BeautifulSoup(home.text, "html.parser").find_all("a", href=True)
                  if PRODUCT_LINK.search(a["href"])]
    for href in prod_links[:2]:
        full = href if href.startswith("http") else f"{urlparse(home.url).scheme}://{host}{href if href.startswith('/') else '/' + href}"
        if full not in queue:
            queue.append(full)
    for url in queue[: max_pages - 1]:
        if urlparse(url).netloc != host:
            continue
        try:
            r = fetcher.fetch(url)
            pages.append(extract_page(r.url, r.text))
        except Exception:
            continue
    flags = sorted({f for p in pages for f in p["security_flags"]})
    comp.security_flags = flags
    hp = pages[0]

    def first(key):  # première page où une caractéristique est observée
        for p in pages:
            if key in p["features"]:
                return p["features"][key], p["url"]
        return None, None

    _fact(db, comp, "positioning", "Titre de la page d'accueil", hp["title"] or "non observé", hp["url"])
    if hp["description"]:
        _fact(db, comp, "positioning", "Méta-description (proposition de valeur déclarée)", hp["description"], hp["url"])
    if hp["h1"]:
        _fact(db, comp, "ads", "Accroche principale (H1)", " | ".join(hp["h1"]), hp["url"])
    ld_prices = [(p["price"], pg["url"]) for pg in pages for p in pg["jsonld_products"] if p.get("price")]
    if ld_prices:
        vals = [v for v, _ in ld_prices]
        _fact(db, comp, "pricing", "Prix observés (données structurées)",
              f"{min(vals):.0f}–{max(vals):.0f} € (médiane {statistics.median(vals):.0f} €, n={len(vals)})", ld_prices[0][1],
              method="json_ld", confidence="high" if len(vals) >= 5 else "medium")
    else:
        tp = [v for pg in pages for v in pg["text_prices"]]
        if tp:
            _fact(db, comp, "pricing", "Prix repérés dans le texte", f"{min(tp):.0f}–{max(tp):.0f} € (n={len(tp)}, extraction textuelle)",
                  hp["url"], method="text_regex", confidence="low")
        else:
            _fact(db, comp, "pricing", "Prix", "non observé sur les pages analysées", hp["url"], status="unavailable", confidence="low")
    for pg in pages:
        m = PROMO_RE.search(pg["text_excerpt"])
        if m:
            _fact(db, comp, "pricing", "Promotion affichée", m.group(0), pg["url"])
            break
    for pg in pages:
        m = BUNDLE_RE.search(pg["text_excerpt"])
        if m:
            _fact(db, comp, "pricing", "Offre groupée mentionnée", m.group(0), pg["url"])
            break
    for key, dim, label in [("free_shipping", "shipping", "Livraison gratuite"), ("free_shipping_threshold", "shipping", "Seuil de livraison gratuite"),
                            ("delivery_time", "shipping", "Délai de livraison affiché"), ("click_collect", "shipping", "Retrait en magasin"),
                            ("free_returns", "returns", "Retours gratuits"), ("return_window", "returns", "Délai de retour"),
                            ("warranty", "returns", "Garantie mentionnée"), ("authenticity", "product", "Mention d'authenticité"),
                            ("size_guide", "product", "Guide des tailles"), ("loyalty", "positioning", "Programme de fidélité"),
                            ("live_chat", "ux", "Chat / messagerie")]:
        val, url = first(key)
        _fact(db, comp, dim, label, val if val else "non observé sur les pages analysées", url or hp["url"],
              status="observed" if val else "unavailable", confidence="medium" if val else "low")
    pays = sorted({x for p in pages for x in p["payments"]})
    _fact(db, comp, "payment", "Moyens de paiement mentionnés", ", ".join(pays) if pays else "non observé", hp["url"],
          status="observed" if pays else "unavailable")
    _fact(db, comp, "mobile", "Balise viewport (adaptation mobile)", "oui" if hp["viewport"] else "non observée", hp["url"], confidence="high")
    _fact(db, comp, "ux", "Recherche", "champ de recherche présent" if any(p["has_search"] for p in pages) else "non observé", hp["url"])
    _fact(db, comp, "ux", "Filtres", f"{max(p['filter_elements'] for p in pages)} éléments de filtre repérés", hp["url"], confidence="low")
    _fact(db, comp, "seo", "SEO technique", f"title {len(hp['title'])} car. ; méta-description {'oui' if hp['description'] else 'non'} ; "
          f"canonical {'oui' if hp['canonical'] else 'non'} ; hreflang {', '.join(hp['hreflang']) or 'aucun'} ; "
          f"données structurées produit {'oui' if ld_prices else 'non observées'}", hp["url"], confidence="high")
    _fact(db, comp, "seo", "Contenu éditorial (blog/magazine)", "lien observé" if any(p["blog"] for p in pages) else "non observé", hp["url"])
    socials = sorted({s for p in pages for s in p["socials"]})
    _fact(db, comp, "social", "Réseaux sociaux liés", ", ".join(socials) if socials else "non observé", hp["url"])
    domain = urlparse(comp.url).netloc.replace("www.", "")
    _fact(db, comp, "ads", "Publicités en cours", "à consulter dans les bibliothèques publiques (non collecté automatiquement)",
          f"https://www.facebook.com/ads/library/?active_status=active&ad_type=all&q={quote(domain)}", method="lien", status="unavailable", confidence="low")
    if hp["displayed_rating"]:
        r = hp["displayed_rating"]
        _fact(db, comp, "reviews", "Note affichée par le site lui-même", f"{r['value']} ({r['count']} avis) — déclaratif, non vérifié",
              hp["url"], method="json_ld", status="observed", confidence="low")
    # synthèse IA optionnelle, contenu passé comme donnée
    excerpt = "\n\n".join(f"[{p['url']}]\n{p['text_excerpt'][:1500]}" for p in pages[:3])
    out = ai.complete_json(db, shop, "competitor_positioning",
                           "Résume le positionnement, la cible et la proposition de valeur observables de ce site, en citant "
                           "uniquement ce qui figure dans les données. Pas de chiffre d'affaires, pas de supposition sur les ventes.",
                           excerpt, {"type": "object", "additionalProperties": False,
                                     "properties": {"positioning": {"type": "string"}, "target": {"type": "string"},
                                                    "value_proposition": {"type": "string"},
                                                    "security_flags": {"type": "array", "items": {"type": "string"}}},
                                     "required": ["positioning", "target", "value_proposition", "security_flags"]},
                           sources=[p["url"] for p in pages])
    if out:
        for k, lbl in (("positioning", "Positionnement (synthèse IA)"), ("target", "Cible (synthèse IA)"),
                       ("value_proposition", "Proposition de valeur (synthèse IA)")):
            _fact(db, comp, "positioning", lbl, neutralise(out[k]), hp["url"], method="ai_summary", status="estimated", confidence="medium")
        comp.security_flags = sorted(set(comp.security_flags) | set(out.get("security_flags", [])))
    comp.profile = {"pages_analysed": [p["url"] for p in pages], "features": {k: bool(first(k)[0]) for k in
                    ("free_shipping", "free_returns", "delivery_time", "size_guide", "authenticity", "live_chat", "loyalty")},
                    "payments": pays, "hreflang": hp["hreflang"], "search": any(p["has_search"] for p in pages),
                    "threshold": _threshold(first("free_shipping_threshold")[0]),
                    "prices": [v for v, _ in ld_prices]}
    for metric in ("revenue", "online_revenue", "net_income"):
        db.add(FinancialFigure(shop_id=shop.id, competitor_id=comp.id, metric=metric, status="unavailable",
                               method="Aucune source vérifiable saisie. Ne pas déduire des performances de l'apparence du site."))
    comp.status = "analysed"
    db.flush()


def _threshold(text):
    if not text:
        return None
    m = re.search(r"(\d+[.,]?\d*)\s*(€|eur)", text)
    return float(m.group(1).replace(",", ".")) if m else None


def analyse_reviews(texts: list[str], vertical_keys=None) -> dict:
    lexicon = dict(REVIEW_THEMES)
    for k in vertical_keys or []:
        for th, words in verticals.get(k)["review_themes"].items():
            lexicon[th] = sorted(set(lexicon.get(th, [])) | set(words))
    themes = {}
    flagged = 0
    for t in texts:
        if detect_injection(t):
            flagged += 1
            continue
        low = t.lower()
        neg = any(w in low for w in NEG)
        pos = any(w in low for w in POS)
        for theme, words in lexicon.items():
            if any(w in low for w in words):
                d = themes.setdefault(theme, {"neg": 0, "pos": 0, "examples": []})
                if neg:
                    d["neg"] += 1
                if pos and not neg:
                    d["pos"] += 1
                if len(d["examples"]) < 3:
                    d["examples"].append(t[:160])
    return {"themes": themes, "flagged": flagged, "n": len(texts)}


def add_reviews(db, comp: Competitor, source_url: str, raw: str) -> ReviewInsight:
    texts = [x.strip() for x in re.split(r"\n\s*\n|\n-{3,}\n|\n(?=\d+/5|★)", raw) if len(x.strip()) > 10]
    snap_v = (comp.analysis.summary or {}).get("verticals") or []
    from ..models import Brief
    b = db.get(Brief, comp.analysis.brief_id) if comp.analysis.brief_id else None
    res = analyse_reviews(texts, (b.extracted or {}).get("categories") if b else snap_v)
    ri = ReviewInsight(shop_id=comp.shop_id, competitor_id=comp.id, source_url=source_url, sample_size=res["n"], themes=res["themes"])
    db.add(ri)
    db.flush()
    return ri


IMPACT_W = {"high": 3, "medium": 2, "low": 1}
EFFORT_W = {"low": 1, "medium": 2, "high": 3}


def recommend(db, shop, analysis: CompetitorAnalysis, brief=None) -> list[Recommendation]:
    for r in db.scalars(select(Recommendation).where(Recommendation.analysis_id == analysis.id)):
        db.delete(r)
    comps = [c for c in analysis.competitors if c.status == "analysed"]
    n = len(comps)
    conf = "high" if n >= 7 else "medium" if n >= 4 else "low"
    cov = Counter()
    for c in comps:
        for k, v in (c.profile.get("features") or {}).items():
            if v:
                cov[k] += 1
    reviews = db.scalars(select(ReviewInsight).where(ReviewInsight.competitor_id.in_([c.id for c in comps] or [0]))).all()
    neg = Counter()
    for ri in reviews:
        for th, d in ri.themes.items():
            neg[th] += d["neg"]
    total_reviews = sum(ri.sample_size for ri in reviews)

    def ev(feature):
        out = []
        for c in comps:
            f = next((f for f in c.facts if f.label.lower().startswith(feature)), None)
            if f:
                out.append({"competitor": c.name, "url": f.source_url, "fact": f"{f.label} : {f.value}"})
        return out[:10]

    recs = []

    def add(title, rationale, action, impact, effort, confidence, evidence):
        score = IMPACT_W[impact] * IMPACT_W[confidence] / EFFORT_W[effort]
        recs.append(Recommendation(shop_id=shop.id, analysis_id=analysis.id, title=title, rationale=rationale, action=action,
                                   impact=impact, effort=effort, confidence=confidence, score=round(score, 2), evidence=evidence))

    if n == 0:
        add("Compléter la sélection de concurrents", "Aucun concurrent n'a pu être analysé (accès refusé, robots.txt ou erreurs).",
            "Ajouter des URL accessibles ou importer des observations manuelles sourcées.", "high", "low", "high", [])
    else:
        r = lambda k: f"{cov[k]}/{n} concurrents analysés"
        cats = ((brief.extracted or {}).get("categories") if brief else None) or []
        profs = [verticals.get(c) for c in cats] or [verticals.get(None)]
        fit = next((vp["fit"] for vp in profs if vp.get("fit")), None)
        if fit and (cov["size_guide"] < n * 0.7 or neg["taille"]):
            add(f"{fit['label']} par modèle + conseil à partir de la mesure",
                f"Guide des tailles observé chez {r('size_guide')}" + (f" ; {neg['taille']} avis négatifs liés à la taille sur {total_reviews} avis importés." if neg["taille"] else "."),
                f"Renseigner les guides par marque/modèle ({fit['measure_label'].lower()}) et activer l'outil d'aide au choix avec affichage de l'incertitude.",
                "high", "medium", conf, ev("guide des tailles"))
        for vp in profs:
            rec_attrs = vp.get("recommended_attributes") or []
            if rec_attrs:
                labels = ", ".join(verticals.attribute_label(vp, k) for k in rec_attrs)
                add(f"Fiches {vp['label'].lower()} techniquement complètes",
                    f"Bonne pratique de la verticale {vp['label'].lower()} (non dérivée des observations) : les critères de comparaison usuels sont {labels}.",
                    f"Renseigner systématiquement {labels} ; la boutique signale les fiches incomplètes avant publication.",
                    "medium", "low", "low", [])
            for th, words in vp["review_themes"].items():
                if neg[th] >= 2:
                    add(f"Répondre au problème récurrent « {th} » ({vp['label'].lower()})",
                        f"{neg[th]} avis négatifs sur ce thème parmi {total_reviews} avis importés.",
                        "Rendre l'information visible sur la fiche et dans la FAQ ; préparer une réponse type du service client.",
                        "medium", "low", conf, [])
        if neg["description"]:
            add("Fiches fidèles au produit reçu", f"{neg['description']} avis signalent un écart entre la description et le produit.",
                "Photos réelles, mesures et matières vérifiées ; aucune mention non prouvée.", "high", "medium", conf, [])
        if cov["delivery_time"] < n * 0.6 or neg["livraison"]:
            add("Délais de livraison affichés par pays avant l'achat",
                f"Délai explicite observé chez {r('delivery_time')}" + (f" ; {neg['livraison']} avis négatifs sur la livraison." if neg["livraison"] else "."),
                "Saisir les délais min/max par zone : ils s'affichent sur chaque fiche et au paiement.", "high", "low", conf, ev("délai de livraison"))
        if cov["free_returns"] >= n * 0.5:
            add("Aligner la politique de retours (attente du marché)",
                f"Retours gratuits observés chez {r('free_returns')} : probablement une attente de base.",
                "Simuler le coût des retours gratuits dans le copilote avant de l'annoncer.", "high", "medium", conf, ev("retours gratuits"))
        elif neg["retours"]:
            add("Simplifier les retours", f"{neg['retours']} avis négatifs portent sur les retours.",
                "Formulaire de retour en ligne et échange de taille prioritaire.", "medium", "low", conf, [])
        ths = [c.profile.get("threshold") for c in comps if c.profile.get("threshold")]
        if ths:
            med = statistics.median(ths)
            add(f"Seuil de livraison gratuite autour de {med:.0f} € à tester",
                f"Seuils observés : {', '.join(f'{t:.0f} €' for t in ths)} (n={len(ths)}).",
                "Vérifier dans le copilote que la marge contributive reste positive au seuil choisi.", "medium", "low",
                "medium" if len(ths) >= 3 else "low", ev("seuil de livraison"))
        if "BE" in (shop.countries or []):
            bc = sum(1 for c in comps if "Bancontact" in c.profile.get("payments", []))
            add("Proposer Bancontact pour la Belgique", f"Bancontact mentionné chez {bc}/{n} concurrents analysés.",
                "Activer Bancontact dans le tableau de bord Stripe (Checkout l'affichera automatiquement).", "high", "low", conf, ev("moyens de paiement"))
            nl = sum(1 for c in comps if any(h.lower().startswith("nl") for h in c.profile.get("hreflang", [])))
            add("Version néerlandaise pour la Flandre", f"Version NL déclarée (hreflang) chez {nl}/{n} concurrents analysés.",
                "Utiliser « Traduis la boutique en néerlandais » puis faire relire les textes.", "medium", "medium", conf, [])
        if (brief and (brief.extracted or {}).get("brands")) or neg["authenticite"]:
            add("Provenance et authenticité démontrées sur chaque fiche",
                f"Mention d'authenticité observée chez {r('authenticity')}" + (f" ; {neg['authenticite']} avis évoquent des doutes d'authenticité." if neg["authenticite"] else "."),
                "Renseigner la provenance (fournisseur, facture) et afficher clairement le statut de revendeur indépendant.",
                "high", "medium", conf, ev("mention d'authenticité"))
        if neg["stock"]:
            add("Stock réel par variante et alerte de retour en stock", f"{neg['stock']} avis négatifs liés aux ruptures/annulations.",
                "Stock par variante affiché, réservation au paiement, alertes e-mail.", "medium", "low", conf, [])
        if neg["service_client"] or cov["live_chat"] < n * 0.3:
            add("Assistance rapide et assistant d'achat factuel",
                f"Chat observé chez {r('live_chat')}" + (f" ; {neg['service_client']} avis négatifs sur le service client." if neg["service_client"] else "."),
                "Activer l'assistant d'achat (réponses issues du catalogue uniquement, transfert au commerçant sinon).", "medium", "low", conf, ev("chat"))
    recs.sort(key=lambda x: -x.score)
    for r in recs:
        db.add(r)
    db.flush()
    trace(db, shop.id, "recommendations", engine="rules", input_summary=f"analyse {analysis.id}, {n} concurrents analysés",
          sources=[c.url for c in comps], assumptions=[{"confidence_rule": "high ≥7 concurrents, medium 4-6, low <4"}],
          output={"titles": [r.title for r in recs]})
    return recs


def run(db, analysis: CompetitorAnalysis, fetcher: SafeFetcher | None = None):
    shop = db.get(Shop, analysis.shop_id)
    fetcher = fetcher or SafeFetcher()
    analysis.status = "running"
    db.flush()
    for c in analysis.competitors:
        if c.status == "pending":
            analyse_competitor(db, shop, c, fetcher)
            db.commit()
    from ..models import Brief
    brief = db.get(Brief, analysis.brief_id) if analysis.brief_id else None
    recommend(db, shop, analysis, brief)
    analysis.status, analysis.completed_at = "done", now()
    analysis.summary = {"analysed": sum(c.status == "analysed" for c in analysis.competitors),
                        "blocked": sum(c.status == "blocked" for c in analysis.competitors),
                        "failed": sum(c.status == "failed" for c in analysis.competitors)}
    trace(db, shop.id, "competitor_analysis", engine="rules" + ("+ai" if ai.settings.ai_enabled else ""),
          input_summary=f"{len(analysis.competitors)} concurrents", sources=[c.url for c in analysis.competitors],
          assumptions=SELECTION_CRITERIA, output=analysis.summary)
    db.commit()
    return analysis.summary


FETCHER_FACTORY = SafeFetcher  # remplaçable en test


@handler("competitor_analysis")
def _job(db, job):
    a = db.get(CompetitorAnalysis, job.payload["analysis_id"])
    return run(db, a, FETCHER_FACTORY())


# ---------------------------------------------------------------- finances & simulations
REQUIRED_FOR_VERIFIED = ("source_url", "published_on", "period", "legal_entity", "scope")


def validate_financial(data: dict) -> list[str]:
    errs = []
    if data.get("status") not in ("verified", "estimated", "unavailable"):
        errs.append("Statut invalide")
    if data.get("status") in ("verified", "estimated"):
        if data.get("value") in (None, ""):
            errs.append("Valeur manquante")
        for f in REQUIRED_FOR_VERIFIED:
            if not data.get(f):
                errs.append(f"Champ obligatoire pour un chiffre {data['status']} : {f}")
        if data.get("source_url") and not str(data["source_url"]).startswith(("http://", "https://")):
            errs.append("La source doit être un lien")
        if not data.get("method"):
            errs.append("Méthode obligatoire")
    if data.get("metric") == "online_revenue" and "groupe" in str(data.get("scope", "")).lower():
        errs.append("Un CA de groupe ne peut pas être enregistré comme CA de boutique en ligne")
    if data.get("metric") == "net_income" and any(w in str(data.get("scope", "")).lower() for w in ("gamme", "produit", "rayon")):
        errs.append("Le résultat d'une entreprise ne peut pas être attribué à une gamme de produits")
    return errs


def simulate(traffic: float, scenarios: list[dict]) -> list[dict]:
    out = []
    for s in scenarios:
        conv, aov = float(s["conversion_pct"]), float(s["aov"])
        out.append({**s, "traffic": traffic, "orders": round(traffic * conv / 100), "revenue": round(traffic * conv / 100 * aov),
                    "formula": f"{traffic:,.0f} visites × {conv} % × {aov:.0f} €".replace(",", " ")})
    return out


def create_simulation(db, shop, comp: Competitor | None, name: str, traffic: float, traffic_source: str, scenarios: list[dict]) -> Simulation:
    sim = Simulation(shop_id=shop.id, competitor_id=comp.id if comp else None, name=name,
                     traffic_source=traffic_source or "Hypothèse saisie par le commerçant (aucune source)",
                     scenarios=simulate(traffic, scenarios))
    db.add(sim)
    db.flush()
    trace(db, shop.id, "simulation", input_summary=name, assumptions=[{"traffic": traffic, "source": sim.traffic_source}] + scenarios,
          output={"scenarios": sim.scenarios, "warning": "Simulation : ne constitue pas une donnée sur les revenus réels"})
    return sim
