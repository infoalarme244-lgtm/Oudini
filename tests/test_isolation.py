"""Isolation stricte entre commerçants et entre boutiques."""
import json

from sqlalchemy import select

from app.models import Order, Product, Variant
from app.services import payments
from conftest import Shopper, make_ready, shop_of


def two_shops(merchant, merchant_b, db):
    a = merchant.create_shop(demo=True, name="Boutique A")
    b = merchant_b.create_shop(demo=True, name="Boutique B")
    make_ready(db, a)
    make_ready(db, b)
    return shop_of(db, a), shop_of(db, b)


def test_merchant_cannot_access_other_shop(app, merchant, merchant_b, db):
    a, b = two_shops(merchant, merchant_b, db)
    pa = db.scalars(select(Product).where(Product.shop_id == a.id)).first()
    va = next(v for v in pa.variants if v.stock > 0)
    s = Shopper(app, a.slug, a.preview_token)
    s.buy(va.id)
    oa = db.scalar(select(Order).where(Order.shop_id == a.id))
    for url in [f"/app/shops/{a.id}", f"/app/shops/{a.id}/products", f"/app/shops/{a.id}/orders/{oa.id}",
                f"/app/shops/{a.id}/products/{pa.id}", f"/app/shops/{a.id}/dashboard", f"/app/shops/{a.id}/export.csv",
                f"/app/shops/{a.id}/traces", f"/app/shops/{a.id}/customers"]:
        assert merchant_b.get(url).status_code == 404, url
    # objets de A adressés via la boutique de B : 404 aussi
    for url in [f"/app/shops/{b.id}/orders/{oa.id}", f"/app/shops/{b.id}/products/{pa.id}"]:
        assert merchant_b.get(url).status_code == 404, url
    r = merchant_b.post(f"/app/shops/{b.id}/variants/{va.id}", {"price": "1", "cost": "", "stock": "999"})
    assert r.status_code == 404
    r = merchant_b.post(f"/app/shops/{b.id}/orders/{oa.id}/refund", {"amount": "1"})
    assert r.status_code == 404
    db.expire_all()
    assert db.get(Variant, va.id).stock != 999
    # export de B ne contient aucune donnée de A
    csv = merchant_b.get(f"/app/shops/{b.id}/export.csv").text
    assert all(v.sku not in csv for v in pa.variants) or all(v.sku.startswith("DEMO") for v in pa.variants)
    assert "Boutique A" not in merchant_b.get("/app").text


def test_storefront_cross_shop_objects_rejected(app, merchant, merchant_b, db):
    a, b = two_shops(merchant, merchant_b, db)
    va = db.scalars(select(Variant).where(Variant.shop_id == a.id, Variant.stock > 0)).first()
    sb = Shopper(app, b.slug, b.preview_token)
    assert sb.post("/cart/add", {"variant_id": va.id}).status_code == 404
    sa = Shopper(app, a.slug, a.preview_token)
    sa.buy(va.id)
    oa = db.scalar(select(Order).where(Order.shop_id == a.id))
    assert sb.get(f"/order/{oa.public_token}").status_code == 404
    assert sb.get(f"/pay/sim/{oa.public_token}").status_code == 404
    # un événement signé pour B ne peut pas toucher une commande de A
    body, _ = payments.sim_event(a.id, "payment.succeeded", oa)
    sig_b = payments.sim_sign(b.id, body)
    r = sb.c.post(f"/webhooks/simulator/{b.id}", content=body, headers={"x-sim-signature": sig_b})
    assert r.json()["result"] == "commande inconnue"
    # signature de A rejetée sur l'endpoint de B
    assert sb.c.post(f"/webhooks/simulator/{b.id}", content=body, headers={"x-sim-signature": payments.sim_sign(a.id, body)}).status_code == 400
    db.expire_all()
    assert db.get(Order, oa.id).status == "pending_payment"


def test_unpublished_shop_is_private(app, merchant, db):
    from fastapi.testclient import TestClient
    sid = merchant.create_shop(demo=True)
    shop = shop_of(db, sid)
    anon = TestClient(app)
    assert anon.get(f"/s/{shop.slug}/").status_code == 404
    assert anon.get(f"/s/{shop.slug}/?preview=wrong").status_code == 404
    r = anon.get(f"/s/{shop.slug}/?preview={shop.preview_token}")
    assert r.status_code == 200 and "Prévisualisation privée" in r.text and "noindex" in r.text
    assert anon.get(f"/s/{shop.slug}/search").status_code == 200  # session de prévisualisation mémorisée
    # renouvellement du lien : l'ancien ne marche plus
    merchant.post(f"/app/shops/{sid}/preview-token")
    anon2 = TestClient(app)
    assert anon2.get(f"/s/{shop.slug}/?preview={shop.preview_token}").status_code == 404


def test_roles(app, merchant, db):
    from conftest import Merchant
    sid = merchant.create_shop(demo=False)
    viewer = Merchant(app, "viewer@example.com")
    merchant.post(f"/app/shops/{sid}/members", {"email": "viewer@example.com", "role": "viewer"})
    assert viewer.get(f"/app/shops/{sid}/products").status_code == 200
    assert viewer.post(f"/app/shops/{sid}/products/new", {"title": "X"}).status_code == 403
    staff = Merchant(app, "staff@example.com")
    merchant.post(f"/app/shops/{sid}/members", {"email": "staff@example.com", "role": "staff"})
    assert staff.post(f"/app/shops/{sid}/products/new", {"title": "X"}).status_code == 200
    assert staff.post(f"/app/shops/{sid}/launch/request").status_code == 403
    assert staff.post(f"/app/shops/{sid}/payments/stripe", {"mode": "test", "secret_key": "sk_test_x", "webhook_secret": "whsec_x"}).status_code == 403


def test_csrf_required(app, merchant, db):
    sid = merchant.create_shop(demo=False)
    r = merchant.c.post(f"/app/shops/{sid}/products/new", data={"title": "X"})
    assert r.status_code == 400
    r = merchant.c.post(f"/app/shops/{sid}/products/new", data={"title": "X", "csrf_token": "forged"})
    assert r.status_code == 400
