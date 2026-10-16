"""Commande test complète, paiement échoué, remboursements, doublons, idempotence, cohérence du stock, Stripe signé."""
import hashlib
import hmac
import json
import re
import time
from datetime import timedelta

from sqlalchemy import select

from app.models import EmailOutbox, Order, PaymentAccount, PaymentEvent, Variant, now
from app.services import inventory, orders as ordersvc, payments
from app.services.security import encrypt_secret
from conftest import Shopper, make_ready, shop_of


def setup_shop(merchant, db):
    sid = merchant.create_shop(demo=True)
    make_ready(db, sid)
    shop = shop_of(db, sid)
    v = db.scalars(select(Variant).where(Variant.shop_id == sid, Variant.stock >= 3).order_by(Variant.id)).first()
    return shop, v


def order_from_redirect(db, r):
    tok = re.search(r"/(?:pay/sim|order)/([\w-]+)", str(r.url)).group(1)
    db.expire_all()
    return db.scalar(select(Order).where(Order.public_token == tok))


def test_complete_test_order(app, merchant, db):
    shop, v = setup_shop(merchant, db)
    stock0 = v.stock
    s = Shopper(app, shop.slug, shop.preview_token)
    r = s.buy(v.id, qty=2, country="BE", consent=True)
    assert "/pay/sim/" in str(r.url) and "Simulateur de paiement" in r.text
    o = order_from_redirect(db, r)
    assert o.status == "pending_payment" and o.payment_mode == "test"
    db.refresh(v)
    assert v.reserved == 2 and v.stock == stock0, "réservé mais pas encore décrémenté"
    assert o.total_cents == v.price_cents * 2 + 690
    assert o.tax_cents == o.total_cents - round(o.total_cents * 10000 / 12100), "TVA BE 21 % incluse"
    r = s.post(f"/pay/sim/{o.public_token}", {"outcome": "success"})
    assert "Paiement confirmé" in r.text
    db.expire_all()
    o = db.get(Order, o.id)
    v = db.get(Variant, v.id)
    assert o.status == "paid" and o.paid_at and o.stock_state == "committed"
    assert v.stock == stock0 - 2 and v.reserved == 0
    assert db.scalar(select(EmailOutbox).where(EmailOutbox.to == "client@example.com", EmailOutbox.subject.contains(o.number)))
    # le commerçant voit et expédie
    r = merchant.get(f"/app/shops/{shop.id}/orders/{o.id}")
    assert o.number in r.text and "payment.succeeded" in r.text
    r = merchant.post(f"/app/shops/{shop.id}/orders/{o.id}/fulfil", {"tracking": "BP123"})
    db.expire_all()
    assert db.get(Order, o.id).status == "fulfilled"
    # suivi client
    r = s.post("/track", {"number": o.number, "email": "client@example.com"})
    assert "Expédiée" in r.text and "BP123" in r.text


def test_failed_payment_releases_stock_and_retry(app, merchant, db):
    shop, v = setup_shop(merchant, db)
    stock0 = v.stock
    s = Shopper(app, shop.slug, shop.preview_token)
    o = order_from_redirect(db, s.buy(v.id, qty=1))
    r = s.post(f"/pay/sim/{o.public_token}", {"outcome": "fail"})
    assert "refusé" in r.text
    db.expire_all()
    o, v = db.get(Order, o.id), db.get(Variant, v.id)
    assert o.status == "payment_failed" and o.paid_at is None and o.stock_state == "released"
    assert v.reserved == 0 and v.stock == stock0
    r = s.post(f"/order/{o.public_token}/retry")
    assert "/pay/sim/" in str(r.url)
    db.expire_all()
    assert db.get(Variant, v.id).reserved == 1
    s.post(f"/pay/sim/{o.public_token}", {"outcome": "success"})
    db.expire_all()
    assert db.get(Order, o.id).status == "paid" and db.get(Variant, v.id).stock == stock0 - 1


def test_cancelled_payment(app, merchant, db):
    shop, v = setup_shop(merchant, db)
    s = Shopper(app, shop.slug, shop.preview_token)
    o = order_from_redirect(db, s.buy(v.id))
    s.post(f"/pay/sim/{o.public_token}", {"outcome": "cancel"})
    db.expire_all()
    assert db.get(Order, o.id).status == "cancelled" and db.get(Variant, v.id).reserved == 0


def test_refunds_partial_then_full_with_restock(app, merchant, db):
    shop, v = setup_shop(merchant, db)
    stock0 = v.stock
    s = Shopper(app, shop.slug, shop.preview_token)
    o = order_from_redirect(db, s.buy(v.id, qty=2))
    s.post(f"/pay/sim/{o.public_token}", {"outcome": "success"})
    db.expire_all()
    o = db.get(Order, o.id)
    item = o.items[0]
    r = merchant.post(f"/app/shops/{shop.id}/orders/{o.id}/refund", {"amount": "10,00", "reason": "geste", f"restock_{item.id}": "0"})
    assert "succeeded" in r.text
    db.expire_all()
    o = db.get(Order, o.id)
    assert o.status == "partially_refunded" and o.refunded_cents == 1000
    # dépassement refusé
    r = merchant.post(f"/app/shops/{shop.id}/orders/{o.id}/refund", {"amount": f"{o.total_cents/100:.2f}", "reason": "x"})
    assert "Montant invalide" in r.text
    rest = (o.total_cents - 1000) / 100
    r = merchant.post(f"/app/shops/{shop.id}/orders/{o.id}/refund", {"amount": f"{rest:.2f}", "reason": "retour", f"restock_{item.id}": "2"})
    db.expire_all()
    o, v = db.get(Order, o.id), db.get(Variant, v.id)
    assert o.status == "refunded" and o.refunded_cents == o.total_cents
    assert v.stock == stock0, "les 2 paires retournées sont remises en stock"
    r = merchant.post(f"/app/shops/{shop.id}/orders/{o.id}/refund", {"amount": "1", "reason": "x"})
    assert "impossible" in r.text


def test_refund_unpaid_order_refused(app, merchant, db):
    shop, v = setup_shop(merchant, db)
    s = Shopper(app, shop.slug, shop.preview_token)
    o = order_from_redirect(db, s.buy(v.id))
    r = merchant.post(f"/app/shops/{shop.id}/orders/{o.id}/refund", {"amount": "1", "reason": "x"})
    assert "Remboursement impossible" in r.text


def test_duplicate_events_processed_once(app, merchant, db):
    shop, v = setup_shop(merchant, db)
    stock0 = v.stock
    s = Shopper(app, shop.slug, shop.preview_token)
    o = order_from_redirect(db, s.buy(v.id))
    body, sig = payments.sim_event(shop.id, "payment.succeeded", o)
    r1 = s.c.post(f"/webhooks/simulator/{shop.id}", content=body, headers={"x-sim-signature": sig})
    r2 = s.c.post(f"/webhooks/simulator/{shop.id}", content=body, headers={"x-sim-signature": sig})
    assert r1.json()["result"].startswith("paiement confirmé") and r2.json()["result"] == "duplicate"
    # un second événement « succès » distinct ne re-décrémente pas
    body2, sig2 = payments.sim_event(shop.id, "payment.succeeded", o)
    assert s.c.post(f"/webhooks/simulator/{shop.id}", content=body2, headers={"x-sim-signature": sig2}).json()["result"] == "déjà payée"
    # un échec tardif n'annule pas une commande payée
    body3, sig3 = payments.sim_event(shop.id, "payment.failed", o)
    s.c.post(f"/webhooks/simulator/{shop.id}", content=body3, headers={"x-sim-signature": sig3})
    db.expire_all()
    assert db.get(Variant, v.id).stock == stock0 - 1 and db.get(Order, o.id).status == "paid"
    assert len(db.scalars(select(PaymentEvent).where(PaymentEvent.order_id == o.id)).all()) == 3
    bad = s.c.post(f"/webhooks/simulator/{shop.id}", content=body, headers={"x-sim-signature": "0" * 64})
    assert bad.status_code == 400


def test_checkout_idempotent(app, merchant, db):
    shop, v = setup_shop(merchant, db)
    s = Shopper(app, shop.slug, shop.preview_token)
    s.post("/cart/add", {"variant_id": v.id})
    data = {"email": "c@example.com", "name": "C", "line1": "Rue 1", "postal_code": "75001", "city": "Paris", "country": "FR",
            "rate": "std", "idempotency_key": "same-key-123"}
    r1 = s.post("/checkout", data)
    s.post("/cart/add", {"variant_id": v.id})  # le client revient, panier non vide
    r2 = s.post("/checkout", data)  # double soumission avec la même clé
    db.expire_all()
    assert len(db.scalars(select(Order).where(Order.shop_id == shop.id)).all()) == 1
    assert db.get(Variant, v.id).reserved == 1


def test_amount_mismatch_not_marked_paid(app, merchant, db):
    shop, v = setup_shop(merchant, db)
    s = Shopper(app, shop.slug, shop.preview_token)
    o = order_from_redirect(db, s.buy(v.id))
    body, sig = payments.sim_event(shop.id, "payment.succeeded", o, amount=1)
    r = s.c.post(f"/webhooks/simulator/{shop.id}", content=body, headers={"x-sim-signature": sig})
    assert "incohérent" in r.json()["result"]
    db.expire_all()
    assert db.get(Order, o.id).status == "pending_payment"


def test_stock_never_oversold(app, merchant, db):
    shop, v = setup_shop(merchant, db)
    v.stock, v.reserved = 1, 0
    db.commit()
    a = Shopper(app, shop.slug, shop.preview_token)
    b = Shopper(app, shop.slug, shop.preview_token)
    a.post("/cart/add", {"variant_id": v.id})
    b.post("/cart/add", {"variant_id": v.id})
    ra = a.post("/checkout", {"email": "a@x.co", "name": "A", "line1": "1", "postal_code": "1", "city": "X", "country": "BE", "rate": "std", "idempotency_key": "ka"})
    rb = b.post("/checkout", {"email": "b@x.co", "name": "B", "line1": "1", "postal_code": "1", "city": "X", "country": "BE", "rate": "std", "idempotency_key": "kb"})
    assert "/pay/sim/" in str(ra.url)
    assert "Stock insuffisant" in rb.text
    db.expire_all()
    vv = db.get(Variant, v.id)
    assert vv.stock == 1 and vv.reserved == 1
    # réservation expirée → libérée, commande annulée
    o = db.scalar(select(Order).where(Order.shop_id == shop.id))
    o.reservation_expires_at = now() - timedelta(minutes=1)
    db.commit()
    assert inventory.expire_reservations(db, shop.id) == 1
    db.commit()
    db.expire_all()
    assert db.get(Variant, v.id).reserved == 0 and db.get(Order, o.id).status == "cancelled"
    # contrainte base : jamais de stock négatif
    import pytest
    from sqlalchemy.exc import IntegrityError
    vv = db.get(Variant, v.id)
    vv.stock = -1
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_invalid_transition_rejected():
    import pytest
    o = Order(status="refunded", status_history=[])
    with pytest.raises(ordersvc.InvalidTransition):
        ordersvc.transition(o, "paid")


def _stripe_sig(payload: str, secret: str) -> str:
    t = int(time.time())
    sig = hmac.new(secret.encode(), f"{t}.{payload}".encode(), hashlib.sha256).hexdigest()
    return f"t={t},v1={sig}"


def test_stripe_webhook_signed_flow(app, merchant, db):
    shop, v = setup_shop(merchant, db)
    secret = "whsec_test_secret_123"
    db.add(PaymentAccount(shop_id=shop.id, provider="stripe", mode="test", status="connected",
                          secret_enc=encrypt_secret("sk_test_dummy"), webhook_secret_enc=encrypt_secret(secret)))
    db.commit()
    # commande créée via le simulateur, puis confirmée par un événement Stripe signé (même machine à états)
    s = Shopper(app, shop.slug, shop.preview_token)
    from app.services import payments as p
    orig = p.provider_for
    p.provider_for = lambda db_, shop_: p.SimulatorProvider(shop_)
    try:
        o = order_from_redirect(db, s.buy(v.id))
    finally:
        p.provider_for = orig
    ev = {"id": "evt_1", "type": "checkout.session.completed", "livemode": False,
          "data": {"object": {"id": "cs_test_1", "payment_status": "paid", "amount_total": o.total_cents, "currency": "eur",
                              "metadata": {"order_id": str(o.id), "shop_id": str(shop.id)}}}}
    payload = json.dumps(ev)
    r = s.c.post(f"/webhooks/stripe/{shop.id}", content=payload, headers={"stripe-signature": _stripe_sig(payload, secret)})
    assert r.status_code == 200 and r.json()["result"].startswith("paiement confirmé")
    r = s.c.post(f"/webhooks/stripe/{shop.id}", content=payload, headers={"stripe-signature": _stripe_sig(payload, secret)})
    assert r.json()["result"] == "duplicate"
    r = s.c.post(f"/webhooks/stripe/{shop.id}", content=payload, headers={"stripe-signature": _stripe_sig(payload, "whsec_wrong")})
    assert r.status_code == 400
    # événement live sur commande test : refusé
    ev2 = {**ev, "id": "evt_2", "livemode": True}
    pl2 = json.dumps(ev2)
    assert "incohérent" in s.c.post(f"/webhooks/stripe/{shop.id}", content=pl2, headers={"stripe-signature": _stripe_sig(pl2, secret)}).json()["result"]
    db.expire_all()
    assert db.get(Order, o.id).status == "paid"


def test_stripe_keys_mode_separation():
    import pytest
    with pytest.raises(payments.PaymentError):
        payments.validate_stripe_keys("test", "sk_live_abc", "whsec_x")
    with pytest.raises(payments.PaymentError):
        payments.validate_stripe_keys("live", "sk_test_abc", "whsec_x")
    payments.validate_stripe_keys("test", "rk_test_abc", "whsec_x")


def test_card_data_never_requested(app, merchant, db):
    shop, v = setup_shop(merchant, db)
    s = Shopper(app, shop.slug, shop.preview_token)
    s.post("/cart/add", {"variant_id": v.id})
    for page in (s.get("/checkout?country=BE").text, ):
        assert not re.search(r'name="(card|cc|cvc|cvv|pan|card_number)', page, re.I)
    from app.models import Base
    cols = {c.name for t in Base.metadata.tables.values() for c in t.columns}
    assert not any(k in cols for k in ("card_number", "cvc", "cvv", "pan"))
