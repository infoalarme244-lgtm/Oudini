"""Multi-catégories : montres, bijoux, accessoires, beauté, générique, et boutiques mixtes."""
import re

from sqlalchemy import select

from app.models import Brief, Product, Variant
from app.services import analysis as an, brief as briefsvc, readiness, size_advisor, verticals
from conftest import Shopper, make_ready, shop_of

WATCH_PROMPT = "Crée une boutique de montres Seiko et Casio pour la France, positionnement premium accessible, style élégant."


def test_detect_verticals():
    assert verticals.detect("boutique de montres automatiques") == ["watches"]
    assert verticals.detect("des bagues et colliers en argent") == ["jewelry"]
    assert verticals.detect("sacs, ceintures et portefeuilles en cuir") == ["accessories"]
    assert verticals.detect("sérums et crèmes vegan") == ["beauty"]
    assert set(verticals.detect("montres et sacs pour femme")) == {"watches", "accessories"}
    assert verticals.detect("des décorations de Noël… je veux découvrir") == []  # « découvrir » ≠ déco
    assert verticals.detect("thé et infusions bio") == []


def test_watch_prompt_assumptions():
    ex = briefsvc.parse_prompt(WATCH_PROMPT)
    assert ex["category"] == "watches" and ex["brands"] == ["Casio", "Seiko"] and ex["countries"] == ["FR"]
    A, Q = briefsvc.build_assumptions(ex)
    a = {x["key"]: x["value"] for x in A}
    assert a["price_band"] == "120–450 €" and a["style"] == ["elegant"]
    assert [q["key"] for q in Q] == ["supply"], "France seule : pas de question de langue"


def test_selective_distribution_warning():
    _, Q = briefsvc.build_assumptions(briefsvc.parse_prompt("Boutique de montres Rolex d'occasion et neuves en Belgique"))
    sup = next(q for q in Q if q["key"] == "supply")
    assert "distribution sélective" in sup["why"] and "garantie constructeur" in sup["why"]


def test_unknown_category_asks_one_question():
    _, Q = briefsvc.build_assumptions(briefsvc.parse_prompt("Je veux lancer une boutique en ligne en France"))
    q = next(q for q in Q if q["key"] == "category")
    assert "Montres" in q["options"] and "Accessoires" in q["options"]


def test_watch_shop_end_to_end(app, merchant, db):
    sid = merchant.create_shop(WATCH_PROMPT, demo=True)
    make_ready(db, sid)
    shop = shop_of(db, sid)
    assert shop.name in verticals.PROFILES["watches"]["names"]
    s = Shopper(app, shop.slug, shop.preview_token)
    home = s.get("/").text
    assert "Automatiques" in home and "Pointure" not in home
    # filtres d'attributs propres aux montres
    r = s.get("/search?attr_movement=automatique")
    listing = r.text.split("résultat(s)")[1]
    assert "Diver démo 200" in listing and "Classic démo 36" not in listing
    assert "Mouvement" in r.text and "Bracelet" in r.text
    assert "Diver démo 200" in s.get("/c/automatiques").text
    # fiche : caractéristiques + aide taille de bracelet
    p = db.scalar(select(Product).where(Product.shop_id == sid, Product.handle == "field-demo-40"))
    page = s.get(f"/product/{p.handle}").text
    assert "Étanchéité (m)" in page and "Tour de poignet (mm)" in page and "Taille de bracelet" in page
    r = s.post("/api/size", {"product_id": p.id, "measure": "150"}).json()
    assert r["ok"] and r["recommended"] == "S" and "maillons" in r["assumption"]
    r = s.post("/api/size", {"product_id": p.id, "measure": "165"}).json()
    assert r["between"] == ["S", "M"] and r["confidence"] == "low"
    # assistant : vocabulaire montres
    r = s.post("/api/assistant", {"question": "je cherche une montre automatique"}).json()
    assert [x["title"] for x in r["products"]] == ["Diver démo 200"]
    # achat
    v = db.scalars(select(Variant).where(Variant.shop_id == sid, Variant.stock > 0)).first()
    o = s.buy(v.id, country="FR")
    assert "/pay/sim/" in str(o.url)
    # pages : conformité montres listée à valider
    from app.services import versions
    cgv = versions.snapshot(db, shop)["pages"]["cgv"]
    assert any("Piles" in x for x in cgv["to_validate"])
    assert "Taille du bracelet" in [n["label"] for n in versions.snapshot(db, shop)["nav"]]


def test_mixed_shop_watches_and_accessories(app, merchant, db):
    sid = merchant.create_shop("Boutique de montres et de sacs en cuir pour la Belgique", demo=True)
    shop = shop_of(db, sid)
    b = db.scalar(select(Brief).where(Brief.shop_id == sid))
    assert set(briefsvc.assumption(b, "categories")) == {"watches", "accessories"}
    from app.services import versions
    snap = versions.snapshot(db, shop)
    handles = [c["handle"] for c in snap["collections"]]
    assert "montres" in handles and "accessoires" in handles
    s = Shopper(app, shop.slug, shop.preview_token)
    r = s.get("/c/accessoires")
    assert "Cabas démo Ville" in r.text and "Diver démo" not in r.text.split("résultat(s)")[1]
    r = s.get("/search?category=watches")
    assert "Catégorie" in r.text and "Cabas démo" not in r.text.split("résultat(s)")[1]
    cands = an.candidates_for(["watches", "accessories"])
    names = [c["name"] for c in cands]
    assert names[0] == "Ocarat" and names[1] == "Zalando" and len(names) == len(set(names))


def test_jewelry_ring_size(app, merchant, db):
    sid = merchant.create_shop("Bijoux : bagues et colliers en argent pour la France", demo=True)
    p = db.scalar(select(Product).where(Product.shop_id == sid, Product.handle == "bague-demo-fil"))
    r = size_advisor.advise(db, sid, p, 51.5)
    assert r["ok"] and r["recommended"] == "52"
    shop = shop_of(db, sid)
    page = Shopper(app, shop.slug, shop.preview_token).get(f"/product/{p.handle}").text
    assert "Tour de doigt (mm)" in page and "argent-925" in page


def test_beauty_requires_inci_before_launch(app, merchant, db):
    sid = merchant.create_shop("Boutique de cosmétiques : sérums et crèmes vegan pour la France", demo=False)
    db.add(Product(shop_id=sid, handle="serum", title="Sérum réel", category="beauty", attributes={}))
    db.commit()
    r = readiness.check(db, shop_of(db, sid))
    item = next(i for i in r["items"] if i["key"] == "required_info")
    assert not item["ok"] and item["blocking"] and "Ingrédients (INCI)" in item["detail"]
    p = db.scalar(select(Product).where(Product.shop_id == sid, Product.handle == "serum"))
    shop = shop_of(db, sid)
    page = Shopper(app, shop.slug, shop.preview_token).get(f"/product/{p.handle}").text
    assert "sizehelp" not in page, "pas d'aide à la taille pour la beauté"


def test_import_with_attributes_and_category(app, merchant, db):
    sid = merchant.create_shop(WATCH_PROMPT, demo=False)
    csv = ("sku;titre;marque;modele;categorie;usage;couleur;taille;prix;cout;stock;attr_movement;attr_water_resistance_m;attr_case_diameter_mm\n"
           "SK-1;Seiko Presage Test;Seiko;Presage;montres;habillee;Bleu / cuir;Unique;450;260;2;automatique;50;40.5\n")
    merchant.post(f"/app/shops/{sid}/import", {}, files={"file": ("w.csv", csv.encode(), "text/csv")})
    p = db.scalar(select(Product).where(Product.shop_id == sid, Product.title == "Seiko Presage Test"))
    assert p.category == "watches" and p.attributes == {"movement": "automatique", "water_resistance_m": "50", "case_diameter_mm": "40.5"}
    out = merchant.get(f"/app/shops/{sid}/export.csv").text
    assert "attr_movement" in out.splitlines()[0] and "automatique" in out
    # édition des caractéristiques dans le back-office
    page = merchant.get(f"/app/shops/{sid}/products/{p.id}").text
    assert "Caractéristiques — Montres" in page and "Distribution sélective" in page
    merchant.post(f"/app/shops/{sid}/products/{p.id}", {"title": p.title, "status": "draft", "category": "watches",
                                                       "attr_movement": "automatique", "attr_glass": "saphir", "attr_water_resistance_m": ""})
    db.expire_all()
    assert db.get(Product, p.id).attributes == {"movement": "automatique", "glass": "saphir", "case_diameter_mm": "40.5"}


def test_size_guide_generic_form(app, merchant, db):
    sid = merchant.create_shop("Accessoires : ceintures en cuir pour la France", demo=False)
    p = Product(shop_id=sid, handle="ceinture", title="Ceinture", brand="Atelier", category="accessories")
    db.add(p)
    db.add(Variant(shop_id=sid, product=p, sku="C-90", size="90", price_cents=5000, stock=2))
    db.commit()
    merchant.post(f"/app/shops/{sid}/size-guides", {"vertical": "accessories", "brand": "Atelier", "rows": "taille;eq;max\n85;100 cm;85\n90;105 cm;90\n95;110 cm;95"})
    r = size_advisor.advise(db, sid, db.get(Product, p.id), 88)
    assert r["recommended"] == "90" and r["in_stock"]
    r = merchant.post(f"/app/shops/{sid}/size-guides", {"vertical": "beauty", "brand": "X", "rows": "S;1"})
    assert "utilise pas de guide" in r.text


def test_generic_shop_without_vertical(app, merchant, db):
    sid = merchant.create_shop("Boutique de thés et infusions bio pour la France", demo=True)
    shop = shop_of(db, sid)
    b = db.scalar(select(Brief).where(Brief.shop_id == sid))
    assert any(q["key"] == "category" for q in b.questions)
    s = Shopper(app, shop.slug, shop.preview_token)
    assert "Produit démo A" in s.get("/c/tout").text
    # réponse à la question → catégorie fixée
    merchant.post(f"/app/shops/{sid}/brief/questions", {"q_category": "Maison & déco"})
    db.expire_all()
    b = db.scalar(select(Brief).where(Brief.shop_id == sid))
    assert briefsvc.assumption(b, "categories") == ["home"]


def test_watch_recommendations_are_vertical_aware(app, merchant, db):
    from app.models import Competitor, CompetitorAnalysis
    sid = merchant.create_shop(WATCH_PROMPT, demo=False)
    b = db.scalar(select(Brief).where(Brief.shop_id == sid))
    a = an.start_analysis(db, shop_of(db, sid), b, [])
    c = Competitor(shop_id=sid, analysis=a, name="X", url="https://x.test-public/", status="analysed", profile={"features": {}})
    db.add(c)
    db.flush()
    an.add_reviews(db, c, "https://avis.example/x", "Le bracelet s'est cassé, boucle de mauvaise qualité.\n\n"
                                                     "Bracelet trop court, pas de maillon fourni, déçu.\n\nParfait, rapide.")
    recs = an.recommend(db, shop_of(db, sid), a, b)
    titles = [r.title for r in recs]
    assert any(t.startswith("Taille du bracelet par modèle") for t in titles)
    assert any("Fiches montres techniquement complètes" == t for t in titles)
    assert any("« bracelet »" in t for t in titles)
    fiche = next(r for r in recs if r.title.startswith("Fiches montres"))
    assert fiche.confidence == "low" and "non dérivée des observations" in fiche.rationale
