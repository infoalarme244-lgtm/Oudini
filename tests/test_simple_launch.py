"""Parcours simple : une photo → boutique générée → 4 étapes pré-remplies → validation en un clic."""
import hashlib
import hmac
import json
import re
import time
from types import SimpleNamespace as NS

import httpx
import pytest
from sqlalchemy import select

from app.config import settings
from app.models import Order, PaymentAccount, Product, ShippingZone, TaxSetting
from app.routers import launch
from app.services import autopilot, payments, versions
from conftest import Shopper, shop_of
from test_photo import photo_bytes


def simple_create(merchant, product_type="watches", countries=("BE", "FR"), prompt=""):
    r = merchant.post("/app/new", {"mode": "simple", "product_type": product_type, "countries": list(countries),
                                   "photo_rights": "own", "prompt": prompt},
                      files={"photo_file": ("p.jpg", photo_bytes(), "image/jpeg")})
    assert r.status_code == 200, r.text[:300]
    assert "/lancement" in str(r.url), str(r.url)
    return int(re.search(r"/app/shops/(\d+)/lancement", str(r.url)).group(1)), r


def valid_siren():
    for n in range(443061841, 443061941):
        s = str(n)
        try:
            autopilot.normalise_company_number(s)
            return s
        except autopilot.LookupError_:
            continue


def valid_bce():
    base = 4031707
    return f"{base:08d}{97 - base % 97:02d}"


def test_simple_creation_prefills_everything(app, merchant, db):
    sid, r = simple_create(merchant)
    assert "Lancer ma boutique" in r.text and "1. Votre produit" in r.text
    shop = shop_of(db, sid)
    assert shop.countries == ["BE", "FR"] and shop.current_version_id
    rates = {z.countries[0]: z.rates[0] for z in db.scalars(select(ShippingZone).where(ShippingZone.shop_id == sid))}
    assert rates["BE"]["price_cents"] == 490 and rates["FR"]["price_cents"] == 590 and rates["BE"]["suggested"]
    assert shop.legal["email"] == "alice@example.com"
    p = db.scalar(select(Product).where(Product.shop_id == sid))
    assert p.category == "watches" and p.status == "draft"
    assert "Prix suggéré" in r.text, "un prix est proposé, jamais imposé"


def test_step1_product(app, merchant, db):
    sid, _ = simple_create(merchant)
    p = db.scalar(select(Product).where(Product.shop_id == sid))
    r = merchant.post(f"/app/shops/{sid}/lancement/produit/{p.id}", {"title": "Montre Bleu Nuit", "description": "Belle montre.", "price": "", "stock": "3"})
    assert "Indiquez un prix" in r.text
    r = merchant.post(f"/app/shops/{sid}/lancement/produit/{p.id}", {"title": "Montre Bleu Nuit", "description": "Belle montre.",
                                                                    "price": "149,90", "stock": "3", "photo_rights": "1"},
                      files=[("photos", ("b.jpg", photo_bytes((20, 160, 60)), "image/jpeg"))])
    assert "Produit prêt à la vente" in r.text
    db.expire_all()
    p = db.get(Product, p.id)
    assert p.status == "active" and p.variants[0].price_cents == 14990 and p.variants[0].stock == 3 and len(p.images) == 2
    assert autopilot.steps(db, shop_of(db, sid))[0]["done"]


def test_step2_company_lookup_france(app, merchant, db, monkeypatch):
    sid, _ = simple_create(merchant)
    siren = valid_siren()

    def handler(req):
        assert req.url.host == "recherche-entreprises.api.gouv.fr"
        return httpx.Response(200, json={"results": [{"siren": siren, "nom_complet": "POINTURE SAS", "nature_juridique": "5710",
                                                      "siege": {"adresse": "1 RUE DE LA PAIX 75002 PARIS"}}]})
    monkeypatch.setattr(launch, "LOOKUP_TRANSPORT", httpx.MockTransport(handler))
    r = merchant.post(f"/app/shops/{sid}/lancement/entreprise/recherche", {"number": siren[:3] + " " + siren[3:6] + " " + siren[6:]})
    assert "Informations retrouvées" in r.text and "POINTURE SAS" in r.text and "registre public" in r.text
    lg = shop_of(db, sid).legal
    assert lg["legal_form"] == "SAS" and lg["vat_number"] == autopilot.fr_vat(siren) and lg["address"].startswith("1 RUE")
    assert "data.gouv.fr" in lg["company_source"]
    r = merchant.post(f"/app/shops/{sid}/lancement/entreprise/recherche", {"number": "123456789"})
    assert "invalide" in r.text, "clé de contrôle SIREN vérifiée"


def test_step2_company_lookup_belgium(app, merchant, db, monkeypatch):
    sid, _ = simple_create(merchant, countries=("BE",))
    bce = valid_bce()

    def handler(req):
        assert req.url.host == "ec.europa.eu" and req.url.path.endswith(f"/BE/vat/{bce}")
        return httpx.Response(200, json={"isValid": True, "name": "Pointure SRL", "address": "Rue Neuve 1\n1000 Bruxelles"})
    monkeypatch.setattr(launch, "LOOKUP_TRANSPORT", httpx.MockTransport(handler))
    merchant.post(f"/app/shops/{sid}/lancement/entreprise/recherche", {"number": f"BE {bce[:4]}.{bce[4:7]}.{bce[7:]}"})
    lg = shop_of(db, sid).legal
    assert lg["company_name"] == "Pointure SRL" and lg["legal_form"] == "SRL" and lg["vat_number"] == f"BE{bce}"
    assert lg["address"] == "Rue Neuve 1, 1000 Bruxelles" and lg["registration"].startswith("BCE ")
    monkeypatch.setattr(launch, "LOOKUP_TRANSPORT", httpx.MockTransport(lambda r: httpx.Response(200, json={"isValid": False})))
    r = merchant.post(f"/app/shops/{sid}/lancement/entreprise/recherche", {"number": bce})
    assert "non trouvé" in r.text


class FakeStripe:
    def __init__(self):
        self.calls = []
        self.charges_enabled = False
        test = self

        class Accounts:
            def create(self, params=None, options=None):
                test.calls.append(("accounts.create", params, options))
                return NS(id="acct_test_123")

            def retrieve(self, acct, params=None, options=None):
                return NS(id=acct, charges_enabled=test.charges_enabled, details_submitted=True)

        class Links:
            def create(self, params=None, options=None):
                test.calls.append(("account_links.create", params, options))
                return NS(url="https://connect.stripe.com/setup/s/test_onboarding")
        self.v1 = NS(accounts=Accounts(), account_links=Links())


def _sig(payload, secret):
    t = int(time.time())
    return f"t={t},v1={hmac.new(secret.encode(), f'{t}.{payload}'.encode(), hashlib.sha256).hexdigest()}"


def test_step3_stripe_connect(app, merchant, db, monkeypatch):
    sid, _ = simple_create(merchant)
    monkeypatch.setattr(settings, "stripe_platform_key", "sk_test_platform")
    monkeypatch.setattr(settings, "stripe_platform_webhook_secret", "whsec_platform")
    fake = FakeStripe()
    monkeypatch.setattr(launch, "CONNECT_CLIENT", fake)
    r = merchant.post(f"/app/shops/{sid}/lancement/paiements/connecter", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("https://connect.stripe.com/")
    params = fake.calls[0][1]
    assert params["country"] == "BE" and params["controller"]["requirement_collection"] == "stripe"
    link = fake.calls[1][1]
    assert link["return_url"].endswith(f"/app/shops/{sid}/lancement/paiements/retour")
    merchant.post(f"/app/shops/{sid}/lancement/paiements/connecter", follow_redirects=False)
    assert len([c for c in fake.calls if c[0] == "accounts.create"]) == 1, "un seul compte par boutique"
    r = merchant.get(f"/app/shops/{sid}/lancement/paiements/retour")
    assert "vérification en cours" in r.text
    # Stripe confirme l'activation par webhook (endpoint unique de la plateforme, boutique retrouvée par le compte)
    ev = {"id": "evt_acc", "type": "account.updated", "account": "acct_test_123",
          "data": {"object": {"id": "acct_test_123", "charges_enabled": True, "details_submitted": True}}}
    pl = json.dumps(ev)
    res = merchant.c.post("/webhooks/stripe-connect", content=pl, headers={"stripe-signature": _sig(pl, "whsec_platform")}).json()
    assert res["result"] == "compte connected"
    assert autopilot.steps(db, shop_of(db, sid))[2]["done"]
    bad = merchant.c.post("/webhooks/stripe-connect", content=pl, headers={"stripe-signature": _sig(pl, "whsec_autre")})
    assert bad.status_code == 400
    ev2 = {**ev, "id": "evt_x", "account": "acct_inconnu"}
    pl2 = json.dumps(ev2)
    assert merchant.c.post("/webhooks/stripe-connect", content=pl2, headers={"stripe-signature": _sig(pl2, "whsec_platform")}).json()["result"] == "compte inconnu"


def test_connect_checkout_uses_merchant_account(app, merchant, db, monkeypatch):
    sid, _ = simple_create(merchant)
    monkeypatch.setattr(settings, "stripe_platform_key", "sk_test_platform")
    shop = shop_of(db, sid)
    db.add(PaymentAccount(shop_id=sid, provider="stripe_connect", mode="test", status="connected", connected_account_id="acct_m1"))
    p = db.scalar(select(Product).where(Product.shop_id == sid))
    p.status = "active"
    p.variants[0].price_cents, p.variants[0].stock = 10000, 2
    for z in db.scalars(select(ShippingZone).where(ShippingZone.shop_id == sid)):
        z.rates = [{**z.rates[0], "suggested": False}]
    db.commit()
    seen = {}

    class Sessions:
        def create(self, params=None, options=None):
            seen["options"] = options
            return NS(id="cs_test_1", url="https://checkout.stripe.com/c/pay/cs_test_1")
    fake = NS(v1=NS(checkout=NS(sessions=Sessions())))
    import stripe
    monkeypatch.setattr(stripe, "StripeClient", lambda key: fake)
    s = Shopper(app, shop.slug, shop.preview_token)
    r = s.post("/cart/add", {"variant_id": p.variants[0].id})
    r = s.get("/checkout?country=BE")
    idem = re.search(r'name="idempotency_key" value="([^"]+)"', r.text).group(1)
    r = s.post("/checkout", {"email": "c@x.co", "name": "C", "line1": "1", "postal_code": "1000", "city": "Bxl", "country": "BE",
                             "rate": "std", "idempotency_key": idem}, follow_redirects=False)
    assert r.headers["location"].startswith("https://checkout.stripe.com/")
    assert seen["options"]["stripe_account"] == "acct_m1", "paiement encaissé sur le compte du commerçant"
    # webhook Connect : la commande est retrouvée via le compte connecté
    monkeypatch.setattr(settings, "stripe_platform_webhook_secret", "whsec_platform")
    o = db.scalar(select(Order).where(Order.shop_id == sid))
    ev = {"id": "evt_pay", "type": "checkout.session.completed", "account": "acct_m1", "livemode": False,
          "data": {"object": {"id": "cs_test_1", "payment_status": "paid", "amount_total": o.total_cents, "currency": "eur",
                              "metadata": {"order_id": str(o.id), "shop_id": str(sid)}}}}
    pl = json.dumps(ev)
    res = s.c.post("/webhooks/stripe-connect", content=pl, headers={"stripe-signature": _sig(pl, "whsec_platform")}).json()
    assert res["result"].startswith("paiement confirmé")
    db.expire_all()
    assert db.get(Order, o.id).status == "paid"


def _complete(merchant, db, sid):
    shop = shop_of(db, sid)
    merchant.post(f"/app/shops/{sid}/lancement/entreprise", {"company_name": "Pointure SRL", "legal_form": "SRL", "address": "Rue Neuve 1, 1000 Bruxelles",
                                                            "registration": "BCE 0403.170.701", "vat_number": "BE0403170701",
                                                            "email": "contact@pointure.example", "phone": "+32 2 000 00 00", "publisher": "A. Martin",
                                                            "mediator": "Médiateur X"})
    legal = dict(shop_of(db, sid).legal)
    legal["host"] = "Plateforme ShopForge, hébergée chez Hébergeur SA"
    shop = shop_of(db, sid)
    shop.legal = legal
    db.commit()
    p = db.scalar(select(Product).where(Product.shop_id == sid))
    merchant.post(f"/app/shops/{sid}/lancement/produit/{p.id}", {"title": "Montre", "description": "x", "price": "120", "stock": "4"})
    merchant.post(f"/app/shops/{sid}/lancement/entreprise", {"company_name": "Pointure SRL"})  # regénère les pages avec l'hébergeur


def test_step4_one_click_validation_and_publish(app, merchant, db, monkeypatch):
    sid, _ = simple_create(merchant)
    r = merchant.post(f"/app/shops/{sid}/lancement/publier", {})
    assert "Cochez la case" in r.text
    r = merchant.post(f"/app/shops/{sid}/lancement/publier", {"confirm": "1"})
    assert "Informations manquantes" in r.text, "pas de validation de pages légales incomplètes"
    _complete(merchant, db, sid)
    # plateforme en mode test : tout est validé, mais pas de mise en ligne sans paiements réels
    monkeypatch.setattr(settings, "stripe_platform_key", "sk_test_platform")
    db.add(PaymentAccount(shop_id=sid, provider="stripe_connect", mode="test", status="connected", connected_account_id="acct_t"))
    db.commit()
    r = merchant.post(f"/app/shops/{sid}/lancement/publier", {"confirm": "1"})
    assert "Reste avant la mise en ligne" in r.text and "Paiement réel" in r.text
    shop = shop_of(db, sid)
    assert shop.status == "draft"
    assert all(t.validated for t in db.scalars(select(TaxSetting).where(TaxSetting.shop_id == sid)))
    assert not any(r_.get("suggested") for z in db.scalars(select(ShippingZone).where(ShippingZone.shop_id == sid)) for r_ in z.rates)
    snap = versions.snapshot(db, shop)
    assert all(p["validated"] for p in snap["pages"].values() if p.get("legal"))
    # plateforme en paiements réels + compte du commerçant activé : un clic suffit
    monkeypatch.setattr(settings, "stripe_platform_key", "sk_live_platform")
    db.add(PaymentAccount(shop_id=sid, provider="stripe_connect", mode="live", status="connected", connected_account_id="acct_l"))
    db.commit()
    r = merchant.post(f"/app/shops/{sid}/lancement/publier", {"confirm": "1"})
    assert "Votre boutique est en ligne" in r.text, r.text[-2500:]
    shop = shop_of(db, sid)
    assert shop.status == "published" and shop.payment_mode == "live"
    from app.models import ApprovalRequest
    req = db.scalar(select(ApprovalRequest).where(ApprovalRequest.shop_id == sid, ApprovalRequest.action == "publish_store"))
    assert req.status == "executed" and req.decided_by, "validation explicite tracée"


def test_overview_shows_four_steps(app, merchant, db):
    sid, _ = simple_create(merchant)
    page = merchant.get(f"/app/shops/{sid}").text
    assert "Lancement : 0 / 4" in page and "Continuer le lancement" in page and "Mode expert" in page
