"""Parcours idée → boutique, catalogue, rentabilité, édition conversationnelle, assistant, tailles, A/B, veille."""
import io
from datetime import timedelta

import httpx
from sqlalchemy import select

from app.models import (Brief, EmailOutbox, Experiment, Product, ShopVersion, SizeGuide, StockAlert, StoreEvent, SupportTicket,
                        Variant, WatchAlert, WatchTarget, now)
from app.services import brief as briefsvc, experiments, margin, readiness, watch
from app.services.url_guard import SafeFetcher
from conftest import PROMPT, Shopper, fake_resolver, make_ready, shop_of


# ---------------------------------------------------------------- idée → boutique
def test_prompt_understanding_nike_example():
    ex = briefsvc.parse_prompt(PROMPT)
    assert ex["countries"] == ["BE", "FR"] and ex["category"] == "footwear" and ex["brands"] == ["Nike"]
    assert ex["positioning"] == "premium_accessible" and ex["competitor_count"] == 10
    A, Q = briefsvc.build_assumptions(ex)
    a = {x["key"]: x for x in A}
    assert a["currency"]["value"] == "EUR" and a["audience"]["origin"] == "défaut"
    keys = [q["key"] for q in Q]
    assert keys == ["languages", "supply"], "seules les questions indispensables"


def test_generated_store_is_honest_reseller(app, merchant, db):
    sid = merchant.create_shop(demo=False)
    shop = shop_of(db, sid)
    assert "nike" not in shop.name.lower() and "nike" not in shop.slug
    home = Shopper(app, shop.slug, shop.preview_token).get("/").text
    assert "revendeur indépendant" in home.lower() and "ni la boutique officielle ni un distributeur agréé" in home
    for forbidden in ("boutique officielle Nike", "distributeur officiel", "avis clients", "★★★★★", "Plus que quelques heures"):
        assert forbidden not in home
    b = db.scalar(select(Brief).where(Brief.shop_id == sid))
    page = merchant.get(f"/app/shops/{sid}/brief").text
    for label in ("Automatique", "Connexion externe", "Validation du commerçant"):
        assert label in page
    ov = merchant.get(f"/app/shops/{sid}").text
    assert "Connecter Stripe" in ov and "Prévisualisation privée" in ov


def test_assumption_edit_and_answers(app, merchant, db):
    sid = merchant.create_shop(demo=False)
    merchant.post(f"/app/shops/{sid}/brief/questions", {"q_languages": "fr+nl", "q_supply": "Grossiste / fournisseur européen avec factures"})
    shop = shop_of(db, sid)
    assert shop.languages[:2] == ["fr", "nl"] and shop.legal["supply_mode"].startswith("Grossiste")
    merchant.post(f"/app/shops/{sid}/brief/assumptions", {"a_countries": "BE, FR, LU", "a_style": "sombre"})
    shop = shop_of(db, sid)
    assert shop.countries == ["BE", "FR", "LU"]
    b = db.scalar(select(Brief).where(Brief.shop_id == sid))
    assert next(a for a in b.assumptions if a["key"] == "countries")["origin"] == "modifié par le commerçant"


def test_legal_pages_use_real_info_only(app, merchant, db):
    sid = merchant.create_shop(demo=False)
    from app.services import versions
    snap = versions.snapshot(db, shop_of(db, sid))
    ml = snap["pages"]["mentions-legales"]
    assert "[À COMPLÉTER : raison sociale]" in ml["body"] and ml["to_validate"]
    merchant.post(f"/app/shops/{sid}/settings", {"legal_company_name": "Pointure SRL", "name": "Pointure"})
    merchant.post(f"/app/shops/{sid}/content/regenerate-legal")
    snap = versions.snapshot(db, shop_of(db, sid))
    assert "Pointure SRL" in snap["pages"]["mentions-legales"]["body"]
    r = merchant.post(f"/app/shops/{sid}/content/pages/mentions-legales", {"title": "Mentions légales", "body": snap["pages"]["mentions-legales"]["body"], "validated": "1"})
    assert "Impossible de valider" in r.text


# ---------------------------------------------------------------- catalogue
CSV = """handle;titre;marque;modele;usage;genre;couleur;pointure;sku;prix;cout;stock;image_url;licence_image;fournisseur;provenance
air-zoom;Air Zoom Test;Nike;Air Zoom;running;homme;Noir;42;AZ-42;139,99;68,00;4;https://img.example/az.jpg;photo fournisseur autorisée;Grossiste BE;Facture grossiste BE
air-zoom;Air Zoom Test;Nike;Air Zoom;running;homme;Noir;43;AZ-43;139,99;68,00;0;;;Grossiste BE;Facture grossiste BE
air-zoom;Air Zoom Test;Nike;Air Zoom;running;homme;Noir;44;AZ-44;;68,00;2;;;Grossiste BE;
court;Court Test;Nike;Court;lifestyle;femme;Blanc;38;CT-38;99;;3;http://insecure.example/x.jpg;;;
court;Court Test;Nike;Court;lifestyle;femme;Blanc;39;AZ-42;99;50;3;;;;
;;Nike;;;;;;;99;;3;;;;
"""


def test_csv_import_validation_and_export(app, merchant, db):
    sid = merchant.create_shop(demo=False)
    r = merchant.post(f"/app/shops/{sid}/import", {"dry_run": "1"}, files={"file": ("cat.csv", CSV.encode(), "text/csv")})
    assert "erreur(s) bloquante(s)" in r.text
    assert not db.scalars(select(Variant).where(Variant.shop_id == sid)).all(), "vérification sans import"
    merchant.post(f"/app/shops/{sid}/import", {}, files={"file": ("cat.csv", CSV.encode(), "text/csv")})
    from app.models import Job
    job = db.scalars(select(Job).where(Job.shop_id == sid, Job.kind == "catalog_import")).all()[-1]
    rep = job.result
    assert job.status == "done" and rep["variants_created"] == 3
    msgs = {(e["row"], e["field"]) for e in rep["errors"]}
    assert (5, "image_url") in msgs and (6, "sku") in msgs and (7, "sku") in msgs
    assert any(w["field"] == "price" for w in rep["warnings"])
    v = db.scalar(select(Variant).where(Variant.shop_id == sid, Variant.sku == "AZ-42"))
    assert v.price_cents == 13999 and v.cost_cents == 6800 and v.stock == 4 and v.product.supplier.name == "Grossiste BE"
    csv_out = merchant.get(f"/app/shops/{sid}/export.csv").text
    assert "AZ-42" in csv_out and "139.99" in csv_out
    # ré-import idempotent (mise à jour, pas de doublon)
    merchant.post(f"/app/shops/{sid}/import", {}, files={"file": ("cat.csv", CSV.encode(), "text/csv")})
    assert len(db.scalars(select(Variant).where(Variant.shop_id == sid)).all()) == 3


def test_xlsx_import(app, merchant, db):
    from openpyxl import Workbook
    sid = merchant.create_shop(demo=False)
    wb = Workbook()
    ws = wb.active
    ws.append(["sku", "title", "brand", "size", "color", "price", "cost", "stock"])
    ws.append(["X-1", "Produit X", "Marque", "41", "Gris", 120, 55, 2])
    buf = io.BytesIO()
    wb.save(buf)
    merchant.post(f"/app/shops/{sid}/import", {}, files={"file": ("cat.xlsx", buf.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
    v = db.scalar(select(Variant).where(Variant.shop_id == sid, Variant.sku == "X-1"))
    assert v and v.price_cents == 12000 and v.stock == 2


def test_storefront_search_filters_and_footwear_features(app, merchant, db):
    sid = merchant.create_shop(demo=True)
    make_ready(db, sid)
    shop = shop_of(db, sid)
    s = Shopper(app, shop.slug, shop.preview_token)
    assert "DÉMONSTRATION" in s.get("/").text
    r = s.get("/c/running?size=41&in_stock=1")
    assert r.status_code == 200 and "Runner démo Aéro" in r.text and "Trail démo" not in r.text.split("résultat(s)")[1]
    r = s.get("/c/running?size=40&in_stock=1")  # pointure 40 épuisée sur les deux modèles running
    assert "Aucun produit ne correspond" in r.text
    r = s.get("/search?q=hoop&price_max=200")
    assert "Hoop démo Pro" in r.text and "Court démo" not in r.text
    r = s.get("/search?usage=lifestyle&gender=enfant")
    assert "Junior démo Swift" in r.text and "Runner" not in r.text.split("résultat(s)")[1]
    p = db.scalar(select(Product).where(Product.shop_id == sid, Product.handle == "runner-demo-aero"))
    r = s.get(f"/product/{p.handle}")
    assert "Guide des tailles" in r.text and "Me prévenir du retour en stock" in r.text and "jours ouvrés" in r.text
    assert "épuisé" in r.text


def test_back_in_stock_alert(app, merchant, db):
    sid = merchant.create_shop(demo=True)
    shop = shop_of(db, sid)
    v = db.scalars(select(Variant).where(Variant.shop_id == sid, Variant.stock == 0)).first()
    s = Shopper(app, shop.slug, shop.preview_token)
    s.post("/stock-alert", {"variant_id": v.id, "email": "attente@example.com"})
    merchant.post(f"/app/shops/{sid}/variants/{v.id}", {"price": f"{v.price_cents/100:.2f}", "cost": "", "stock": "3", "reorder_point": "2"})
    mail = db.scalar(select(EmailOutbox).where(EmailOutbox.to == "attente@example.com"))
    assert mail and "De retour en stock" in mail.subject
    assert db.scalar(select(StockAlert).where(StockAlert.variant_id == v.id)).notified_at


def test_stock_cannot_go_below_reservations(app, merchant, db):
    sid = merchant.create_shop(demo=True)
    make_ready(db, sid)
    shop = shop_of(db, sid)
    v = db.scalars(select(Variant).where(Variant.shop_id == sid, Variant.stock >= 2)).first()
    Shopper(app, shop.slug, shop.preview_token).buy(v.id, qty=2)
    r = merchant.post(f"/app/shops/{sid}/variants/{v.id}", {"price": "100", "cost": "", "stock": "1", "reorder_point": "2"})
    assert "réservations" in r.text


# ---------------------------------------------------------------- rentabilité
def test_unit_economics_and_missing_data(app, merchant, db):
    sid = merchant.create_shop(demo=True)
    make_ready(db, sid)
    shop = shop_of(db, sid)
    v = db.scalars(select(Variant).where(Variant.shop_id == sid)).first()  # 129 € TTC, coût 62 €
    ue = margin.unit_economics(db, shop, v)
    assert ue["missing"] and not ue["reliable"]
    merchant.post(f"/app/shops/{sid}/settings", {"name": shop.name, "cost_shipping_cost_cents": "5,20", "cost_packaging_cents": "1,00",
                                                 "cost_payment_fee_bp": "1,5", "cost_payment_fee_fixed_cents": "0,25", "cost_cac_cents": "12",
                                                 "cost_return_rate_bp": "15", "cost_return_cost_cents": "6", "cost_min_contribution_pct": "10"})
    shop = shop_of(db, sid)
    ue = margin.unit_economics(db, shop, v)
    assert ue["reliable"]
    ht = round(12900 * 10000 / 12100)  # TVA BE (premier pays)
    assert ue["price_ht"] == ht and ue["gross_margin"] == ht - 6200
    expected = ht - 6200 - 520 - 100 - (round(12900 * 0.015) + 25) - 1200 - round(0.15 * (600 + 520))
    assert ue["contribution"] == expected
    deficit = margin.promo_check(db, shop, [v], 40)
    assert deficit[0]["deficit"], "-40 % rend la vente déficitaire"
    r = merchant.post(f"/app/shops/{sid}/discounts", {"code": "MEGA40", "kind": "percent", "value": "40"})
    assert "deviendraient déficitaires" in r.text


def test_dashboard_separates_metrics(app, merchant, db):
    sid = merchant.create_shop(demo=True)
    make_ready(db, sid)
    shop = shop_of(db, sid)
    v = db.scalars(select(Variant).where(Variant.shop_id == sid, Variant.stock >= 1)).first()
    s = Shopper(app, shop.slug, shop.preview_token)
    r = s.buy(v.id)
    tok = str(r.url).rsplit("/", 1)[1]
    s.post(f"/pay/sim/{tok}", {"outcome": "success"})
    d = margin.dashboard(db, shop_of(db, sid), "test", 30)
    assert d["orders"] == 1 and d["revenue_ttc"] > d["revenue_ht"] > d["gross_margin"]
    assert d["net_result"] is None and d["net_result_reason"]
    assert d["cac"] is None and any("publicitaires" in m for m in d["missing"])
    assert d["conversion"] is not None
    assert margin.dashboard(db, shop_of(db, sid), "live", 30)["orders"] == 0, "test et réel séparés"
    page = merchant.get(f"/app/shops/{sid}/dashboard?mode=test").text
    assert "Mode test" in page and "non disponible" in page and "Marge contributive" in page


# ---------------------------------------------------------------- readiness
def test_readiness_lists_real_blockers(app, merchant, db):
    sid = merchant.create_shop(demo=True)
    r = readiness.check(db, shop_of(db, sid))
    keys = {i["key"] for i in r["blocking"]}
    assert {"payment_live", "shipping_confirmed", "taxes", "catalog", "no_demo", "legal_info", "legal_pages", "placeholders"} <= keys
    assert not r["can_publish"]
    from app.services import versions
    shop = shop_of(db, sid)
    snap = versions.snapshot(db, shop)
    snap["nav"].append({"label": "Cassé", "href": "/c/inexistante"})
    versions.create(db, shop, snap, "lien cassé")
    db.commit()
    r = readiness.check(db, shop_of(db, sid))
    assert next(i for i in r["items"] if i["key"] == "links")["detail"] == "/c/inexistante"


# ---------------------------------------------------------------- édition par conversation
def test_chat_edit_preview_apply_rollback(app, merchant, db):
    sid = merchant.create_shop(demo=True)
    shop = shop_of(db, sid)
    v0 = shop.current_version_id
    r = merchant.post(f"/app/shops/{sid}/content/chat", {"message": "Change le style en sombre"})
    assert "Proposition" in r.text and "theme.palette.bg" in r.text
    prop = db.scalars(select(ShopVersion).where(ShopVersion.shop_id == sid, ShopVersion.status == "proposed")).first()
    assert shop_of(db, sid).current_version_id == v0, "rien n'est appliqué avant validation"
    s = Shopper(app, shop.slug, shop.preview_token)
    assert "#111214" in s.get(f"/?version={prop.id}").text, "prévisualisation de la proposition"
    assert "#111214" not in s.get("/").text
    merchant.post(f"/app/shops/{sid}/content/versions/{prop.id}/apply")
    assert "#111214" in s.get("/").text
    old = db.get(ShopVersion, v0)
    merchant.post(f"/app/shops/{sid}/content/versions/{old.id}/rollback")
    assert "#111214" not in s.get("/").text
    # collection
    r = merchant.post(f"/app/shops/{sid}/content/chat", {"message": "Ajoute une collection Trail"})
    p2 = db.scalars(select(ShopVersion).where(ShopVersion.shop_id == sid, ShopVersion.status == "proposed").order_by(ShopVersion.id.desc())).first()
    merchant.post(f"/app/shops/{sid}/content/versions/{p2.id}/apply")
    assert s.get("/c/trail").status_code == 200
    # traduction NL (sans IA : glossaire honnête)
    r = merchant.post(f"/app/shops/{sid}/content/chat", {"message": "Traduis la boutique en néerlandais"})
    assert "glossaire" in r.text
    p3 = db.scalars(select(ShopVersion).where(ShopVersion.shop_id == sid, ShopVersion.status == "proposed").order_by(ShopVersion.id.desc())).first()
    merchant.post(f"/app/shops/{sid}/content/versions/{p3.id}/apply")
    assert "nl" in shop_of(db, sid).languages
    assert "Winkelmandje" in s.get("/?lang=nl").text
    # pack rentable
    make_ready(db, sid)
    r = merchant.post(f"/app/shops/{sid}/content/chat", {"message": "Propose un pack rentable"})
    assert "Pack" in r.text and ("Remise proposée" in r.text or "Pack impossible" in r.text)
    r = merchant.post(f"/app/shops/{sid}/content/chat", {"message": "fais un truc"})
    assert "Demande non comprise" in r.text


# ---------------------------------------------------------------- assistant & tailles
def test_shopping_assistant_grounded(app, merchant, db):
    sid = merchant.create_shop(demo=True)
    make_ready(db, sid)
    shop = shop_of(db, sid)
    s = Shopper(app, shop.slug, shop.preview_token)
    r = s.post("/api/assistant", {"question": "Je cherche du running femme en 39 à moins de 150 €"}).json()
    assert not r["escalated"] and r["products"] and all("39" in p["sizes"] for p in r["products"])
    assert all(p["price"] <= 150 for p in r["products"])
    r = s.post("/api/assistant", {"question": "Quels sont les délais de livraison ?"}).json()
    assert "2–4 jours" in r["answer"]
    r = s.post("/api/assistant", {"question": "Est-ce que cette semelle convient à une fasciite plantaire ?"}).json()
    assert r["escalated"] and db.scalar(select(SupportTicket).where(SupportTicket.shop_id == sid))
    r = s.post("/api/assistant", {"question": "running homme pointure 52"}).json()
    assert r["products"] == [] and "Aucun produit" in r["answer"]


def test_size_advisor_uncertainty(app, merchant, db):
    sid = merchant.create_shop(demo=True)
    shop = shop_of(db, sid)
    p = db.scalar(select(Product).where(Product.shop_id == sid, Product.handle == "runner-demo-aero"))
    s = Shopper(app, shop.slug, shop.preview_token)
    r = s.post("/api/size", {"product_id": p.id, "foot_mm": "260"}).json()
    assert r["ok"] and r["confidence"] == "low" and "générique" in r["basis"] and r["assumption"]
    r = s.post("/api/size", {"product_id": p.id, "foot_mm": "20"}).json()
    assert not r["ok"]
    # sans guide : refus explicite
    for g in db.scalars(select(SizeGuide).where(SizeGuide.shop_id == sid)):
        db.delete(g)
    db.commit()
    r = s.post("/api/size", {"product_id": p.id, "foot_mm": "260"}).json()
    assert not r["ok"] and "ne pouvons pas recommander" in r["message"]


def test_model_specific_guide_increases_confidence(app, merchant, db):
    sid = merchant.create_shop(demo=True)
    p = db.scalar(select(Product).where(Product.shop_id == sid, Product.handle == "runner-demo-aero"))
    merchant.post(f"/app/shops/{sid}/size-guides", {"brand": p.brand, "model": p.model, "source_url": "https://fournisseur.example/guide",
                                                    "rows": "EU;UK;US;mm\n41;7;8;258\n42;8;9;266\n43;9;10;275"})
    from app.services import size_advisor
    r = size_advisor.advise(db, sid, p, 250)
    assert r["recommended"] == "41" and r["confidence"] == "medium" and "fournisseur.example" in r["basis"]
    r = size_advisor.advise(db, sid, p, 260)  # 265 visé, 266 dispo : marge 1 mm → entre deux tailles
    assert r["between"] == ["42", "43"] and r["confidence"] == "low"


# ---------------------------------------------------------------- A/B
def test_experiment_no_winner_without_data(app, merchant, db):
    sid = merchant.create_shop(demo=True)
    p = db.scalars(select(Product).where(Product.shop_id == sid)).first()
    r = merchant.post(f"/app/shops/{sid}/experiments", {"name": "Titre", "hypothesis": "", "product_id": p.id, "variant_b": "B"})
    assert "Hypothèse obligatoire" in r.text
    merchant.post(f"/app/shops/{sid}/experiments", {"name": "Titre", "hypothesis": "Usage dans le titre", "product_id": p.id, "variant_b": "Runner B"})
    e = db.scalar(select(Experiment).where(Experiment.shop_id == sid))
    merchant.post(f"/app/shops/{sid}/experiments/{e.id}/start")
    shop = shop_of(db, sid)
    for _ in range(4):
        Shopper(app, shop.slug, shop.preview_token).get(f"/product/{p.handle}")
    db.expire_all()
    res = experiments.results(db, db.get(Experiment, e.id))
    assert res["verdict"] == "insufficient_data" and res["reasons"]
    assert sum(a["n"] for a in res["arms"].values()) == 4


def test_experiment_winner_requires_significance(db, app):
    from app.models import Shop
    shop = Shop(slug="exp", name="exp")
    db.add(shop)
    db.flush()
    e = Experiment(shop_id=shop.id, name="t", hypothesis="h", arms=[{"key": "A", "value": "a"}, {"key": "B", "value": "b"}],
                   min_sample_per_arm=400, min_days=7, status="running", started_at=now() - timedelta(days=8))
    db.add(e)
    db.flush()
    for arm, n, conv in (("A", 500, 50), ("B", 500, 90)):
        for i in range(n):
            sidk = f"{arm}{i}"
            db.add(StoreEvent(shop_id=shop.id, session_id=sidk, type="exposure", experiment_id=e.id, arm=arm))
            if i < conv:
                db.add(StoreEvent(shop_id=shop.id, session_id=sidk, type="add_to_cart", experiment_id=e.id, arm=arm))
    db.flush()
    res = experiments.results(db, e)
    assert res["verdict"] == "winner:B" and res["p_value"] < 0.05
    e.started_at = now() - timedelta(days=2)
    assert experiments.results(db, e)["verdict"] == "insufficient_data", "durée minimale non atteinte"


# ---------------------------------------------------------------- veille
def test_watch_alert_and_margin_safe_suggestion(app, merchant, db, monkeypatch):
    sid = merchant.create_shop(demo=True)
    make_ready(db, sid)
    p = db.scalar(select(Product).where(Product.shop_id == sid, Product.handle == "runner-demo-aero"))
    price = {"v": "129.00"}

    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        html = ('<html><head><script type="application/ld+json">{"@type":"Product","name":"R","offers":{"price":"%s","priceCurrency":"EUR",'
                '"availability":"https://schema.org/InStock"}}</script></head><body>Livraison gratuite</body></html>') % price["v"]
        return httpx.Response(200, text=html, headers={"content-type": "text/html"})
    monkeypatch.setattr(watch, "FETCHER_FACTORY", lambda: SafeFetcher(transport=httpx.MockTransport(handler), resolver=fake_resolver))
    monkeypatch.setattr("app.services.url_guard.resolve", fake_resolver)
    merchant.post(f"/app/shops/{sid}/watch", {"label": "Alpha Runner", "url": "https://alpha.test-public/p/runner", "my_product_id": p.id})
    t = db.scalar(select(WatchTarget).where(WatchTarget.shop_id == sid))
    merchant.post(f"/app/shops/{sid}/watch/{t.id}/check")
    price["v"] = "79.00"
    merchant.post(f"/app/shops/{sid}/watch/{t.id}/check")
    a = db.scalar(select(WatchAlert).where(WatchAlert.shop_id == sid, WatchAlert.kind == "price_change"))
    assert a and a.old_value == "129.0" and a.new_value == "79.0" and a.source_url.startswith("https://alpha")
    assert a.suggestion["auto_applied"] is False
    assert all("ne pas s'aligner" in v["recommendation"] for v in a.suggestion["variants"])
    db.expire_all()
    assert all(v.price_cents == 12900 for v in db.get(Product, p.id).variants), "aucune baisse automatique"
    r = merchant.post(f"/app/shops/{sid}/watch", {"label": "x", "url": "http://169.254.169.254/"})
    assert "URL refusée" in r.text


def test_reorder_suggestions(app, merchant, db):
    sid = merchant.create_shop(demo=True)
    from app.services import inventory
    sug = inventory.reorder_suggestions(db, sid)
    assert sug and all(s["basis"] for s in sug)
    assert any(s["available"] == 0 for s in sug)
