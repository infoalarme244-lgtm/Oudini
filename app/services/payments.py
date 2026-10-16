"""Paiements : simulateur de test intégré et Stripe (Checkout hébergé + webhooks signés + remboursements).

Principes :
- aucune donnée de carte ne transite ni n'est stockée par la plateforme (page de paiement hébergée) ;
- une commande n'est « payée » que sur confirmation signée du prestataire (webhook), jamais sur simple retour navigateur ;
- chaque événement est enregistré une seule fois (unicité provider + event_id) : les doublons sont ignorés ;
- modes test et production strictement séparés (clés distinctes, activation du live soumise à validation).
"""
import hashlib
import hmac
import json
import logging
import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..config import settings
from ..models import Order, PaymentAccount, PaymentEvent, Refund
from . import inventory, orders
from .audit import audit
from .security import decrypt_secret

log = logging.getLogger("shopforge.payments")


class PaymentError(Exception):
    pass


def account(db, shop, mode=None) -> PaymentAccount | None:
    mode = mode or shop.payment_mode
    return db.scalar(select(PaymentAccount).where(PaymentAccount.shop_id == shop.id, PaymentAccount.mode == mode,
                                                  PaymentAccount.status == "connected")
                     .order_by(PaymentAccount.provider.desc()))  # stripe avant simulator


def validate_stripe_keys(mode: str, secret: str, webhook_secret: str):
    prefixes = ("sk_test_", "rk_test_") if mode == "test" else ("sk_live_", "rk_live_")
    if not secret.startswith(prefixes):
        raise PaymentError(f"Clé {mode} attendue (préfixe {' ou '.join(prefixes)}) : les modes test et production ne se mélangent pas")
    if not webhook_secret.startswith("whsec_"):
        raise PaymentError("Secret de webhook Stripe attendu (préfixe whsec_)")


# ---------------------------------------------------------------- simulateur (mode test uniquement)
def sim_secret(shop_id: int) -> str:
    return hmac.new(settings.secret_key.encode(), f"simulator:{shop_id}".encode(), hashlib.sha256).hexdigest()


def sim_sign(shop_id: int, body: bytes) -> str:
    return hmac.new(sim_secret(shop_id).encode(), body, hashlib.sha256).hexdigest()


def sim_event(shop_id: int, type_: str, order: Order, amount: int | None = None, refund_ref: str = "") -> tuple[bytes, str]:
    body = json.dumps({"id": f"sim_evt_{uuid.uuid4().hex}", "type": type_,
                       "data": {"order_token": order.public_token, "amount": order.total_cents if amount is None else amount,
                                "currency": order.currency, "payment_ref": f"sim_pi_{order.id}", "refund_ref": refund_ref}}).encode()
    return body, sim_sign(shop_id, body)


class SimulatorProvider:
    name = "simulator"

    def __init__(self, shop):
        if shop.payment_mode != "test":
            raise PaymentError("Le simulateur n'est disponible qu'en mode test")
        self.shop = shop

    def create_checkout(self, order: Order, base_url: str) -> str:
        order.provider = self.name
        return f"{base_url}/s/{self.shop.slug}/pay/sim/{order.public_token}"

    def refund(self, order: Order, amount: int, idem: str) -> dict:
        return {"id": f"sim_re_{idem}", "status": "succeeded"}


class StripeProvider:
    name = "stripe"

    def __init__(self, shop, acc: PaymentAccount):
        import stripe
        if acc.mode != shop.payment_mode:
            raise PaymentError("Compte Stripe d'un autre mode")
        self.shop, self.acc = shop, acc
        self.client = stripe.StripeClient(decrypt_secret(acc.secret_enc))

    def create_checkout(self, order: Order, base_url: str) -> str:
        line_items = [{"quantity": it.qty, "price_data": {"currency": order.currency.lower(), "unit_amount": it.unit_price_cents,
                                                          "product_data": {"name": f"{it.title} {it.variant_label}".strip()}}}
                      for it in order.items]
        params = {
            "mode": "payment", "customer_email": order.email, "client_reference_id": str(order.id),
            "line_items": line_items,
            "metadata": {"order_id": str(order.id), "shop_id": str(self.shop.id)},
            "payment_intent_data": {"metadata": {"order_id": str(order.id), "shop_id": str(self.shop.id)}},
            "success_url": f"{base_url}/s/{self.shop.slug}/order/{order.public_token}?return=1",
            "cancel_url": f"{base_url}/s/{self.shop.slug}/cart?cancelled=1",
        }
        if order.discount_cents:
            coupon = self.client.v1.coupons.create(params={"amount_off": order.discount_cents, "currency": order.currency.lower(),
                                                           "duration": "once", "name": order.discount_code or "Réduction"},
                                                   options={"idempotency_key": f"coupon-{order.idempotency_key}"})
            params["discounts"] = [{"coupon": coupon.id}]
        if order.shipping_cents:
            params["shipping_options"] = [{"shipping_rate_data": {"type": "fixed_amount", "display_name": order.shipping_method.get("name") or "Livraison",
                                                                  "fixed_amount": {"amount": order.shipping_cents, "currency": order.currency.lower()}}}]
        session = self.client.v1.checkout.sessions.create(params=params, options={"idempotency_key": f"checkout-{order.idempotency_key}"})
        order.provider, order.provider_ref = self.name, session.id
        return session.url

    def refund(self, order: Order, amount: int, idem: str) -> dict:
        session = self.client.v1.checkout.sessions.retrieve(order.provider_ref)
        r = self.client.v1.refunds.create(params={"payment_intent": session.payment_intent, "amount": amount,
                                                  "metadata": {"order_id": str(order.id)}},
                                          options={"idempotency_key": idem})
        return {"id": r.id, "status": r.status}



def parse_stripe_webhook(acc: PaymentAccount, payload: bytes, sig: str) -> dict:
    """Vérifie la signature Stripe (HMAC + tolérance temporelle) avec le secret du compte."""
    import stripe
    try:
        stripe.Webhook.construct_event(payload, sig, decrypt_secret(acc.webhook_secret_enc))
    except (ValueError, stripe.SignatureVerificationError) as exc:
        raise PaymentError("Signature de webhook invalide") from exc
    return json.loads(payload)


def provider_for(db, shop):
    acc = account(db, shop)
    if acc and acc.provider == "stripe":
        return StripeProvider(shop, acc)
    if shop.payment_mode == "test" and acc and acc.provider == "simulator":
        return SimulatorProvider(shop)
    raise PaymentError("Aucun moyen de paiement connecté pour le mode " + shop.payment_mode)


# ---------------------------------------------------------------- traitement idempotent des événements
def record_event(db, shop_id, provider, event_id, type_, payload) -> PaymentEvent | None:
    """Insère l'événement ; None s'il a déjà été reçu (doublon)."""
    try:
        with db.begin_nested():
            ev = PaymentEvent(shop_id=shop_id, provider=provider, event_id=event_id, type=type_, payload=payload)
            db.add(ev)
            db.flush()
        return ev
    except IntegrityError:
        return None


def _order_for(db, shop_id, *, token=None, order_id=None):
    if token:
        o = db.scalar(select(Order).where(Order.public_token == token))
    else:
        o = db.get(Order, int(order_id)) if order_id else None
    if o is None or o.shop_id != shop_id:
        return None
    return o


def handle_simulator_event(db, shop, body: bytes, signature: str) -> str:
    if not hmac.compare_digest(sim_sign(shop.id, body), signature or ""):
        raise PaymentError("Signature invalide")
    data = json.loads(body)
    ev = record_event(db, shop.id, "simulator", data["id"], data["type"], data)
    if ev is None:
        return "duplicate"
    d = data["data"]
    order = _order_for(db, shop.id, token=d["order_token"])
    if order is None:
        ev.result = "commande inconnue"
        return ev.result
    ev.order_id = order.id
    if order.payment_mode != "test":
        ev.result = "refusé : événement simulé sur commande live"
        return ev.result
    if data["type"] == "payment.succeeded":
        ev.result = orders.mark_paid(db, order, int(d["amount"]), d["currency"], d["payment_ref"])
    elif data["type"] == "payment.failed":
        ev.result = orders.mark_failed(db, order, "carte refusée (simulation)")
    elif data["type"] == "payment.cancelled":
        ev.result = orders.cancel_unpaid(db, order, "paiement abandonné (simulation)")
    else:
        ev.result = "type ignoré"
    return ev.result


def handle_stripe_event(db, shop, event: dict) -> str:
    ev = record_event(db, shop.id, "stripe", event["id"], event["type"], {"type": event["type"], "id": event["id"]})
    if ev is None:
        return "duplicate"
    obj = event["data"]["object"]
    meta = obj.get("metadata") or {}
    if meta.get("shop_id") and meta["shop_id"] != str(shop.id):
        ev.result = "refusé : événement d'une autre boutique"
        return ev.result
    t = event["type"]
    order = _order_for(db, shop.id, order_id=meta.get("order_id") or obj.get("client_reference_id"))
    if t.startswith("refund."):
        r = db.scalar(select(Refund).where(Refund.shop_id == shop.id, Refund.provider_ref == obj.get("id")))
        if r and obj.get("status") == "succeeded" and r.status != "succeeded":
            apply_refund_success(db, r)
            ev.result = "remboursement confirmé"
        elif r and obj.get("status") in ("failed", "canceled"):
            r.status = "failed"
            ev.result = "remboursement en échec"
        else:
            ev.result = "sans effet"
        return ev.result
    if order is None:
        ev.result = "commande inconnue"
        return ev.result
    ev.order_id = order.id
    if bool(event.get("livemode")) != (order.payment_mode == "live"):
        ev.result = "refusé : mode test/live incohérent"
        return ev.result
    if t in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        if obj.get("payment_status") == "paid":
            ev.result = orders.mark_paid(db, order, int(obj["amount_total"]), obj["currency"], obj.get("id", ""))
        else:
            ev.result = "paiement en attente (moyen asynchrone)"
    elif t in ("checkout.session.async_payment_failed", "payment_intent.payment_failed"):
        ev.result = orders.mark_failed(db, order, "paiement refusé (Stripe)")
    elif t == "checkout.session.expired":
        ev.result = orders.cancel_unpaid(db, order, "session de paiement expirée")
    else:
        ev.result = "type ignoré"
    return ev.result


# ---------------------------------------------------------------- remboursements
def refund_order(db, shop, order: Order, amount_cents: int, reason: str, restock_items: list[dict], user_id=None) -> Refund:
    if order.shop_id != shop.id:
        raise PaymentError("Commande d'une autre boutique")
    if order.status not in ("paid", "fulfilled", "partially_refunded"):
        raise PaymentError(f"Remboursement impossible au statut {order.status}")
    remaining = order.total_cents - order.refunded_cents - sum(r.amount_cents for r in order.refunds if r.status == "pending")
    if amount_cents <= 0 or amount_cents > remaining:
        raise PaymentError(f"Montant invalide (maximum remboursable : {remaining/100:.2f} €)")
    item_ids = {it.id: it for it in order.items}
    for ri in restock_items:
        if int(ri["order_item_id"]) not in item_ids or int(ri["qty"]) > item_ids[int(ri["order_item_id"])].qty:
            raise PaymentError("Article de remise en stock invalide")
    refund = Refund(shop_id=shop.id, order=order, amount_cents=amount_cents, reason=reason, items=restock_items,
                    restock=bool(restock_items))
    db.add(refund)
    db.flush()
    prov = SimulatorProvider(shop) if order.provider == "simulator" else provider_for(db, shop)
    try:
        res = prov.refund(order, amount_cents, f"refund-{order.id}-{refund.id}")
    except Exception as exc:
        refund.status = "failed"
        audit(db, shop.id, user_id, "refund.failed", "order", order.id, after={"error": str(exc)[:300]})
        raise PaymentError(f"Le prestataire a refusé le remboursement : {exc}") from exc
    refund.provider_ref = res["id"]
    if res["status"] == "succeeded":
        apply_refund_success(db, refund)
    elif res["status"] in ("failed", "canceled"):
        refund.status = "failed"
    audit(db, shop.id, user_id, "refund.requested", "order", order.id, after={"amount": amount_cents, "status": refund.status})
    return refund


def apply_refund_success(db, refund: Refund):
    order = refund.order
    refund.status = "succeeded"
    order.refunded_cents += refund.amount_cents
    orders.transition(order, "refunded" if order.refunded_cents >= order.total_cents else "partially_refunded",
                      f"remboursement {refund.amount_cents/100:.2f} € confirmé")
    if order.stock_state == "committed":
        for ri in refund.items or []:
            it = next(i for i in order.items if i.id == int(ri["order_item_id"]))
            if it.variant_id:
                inventory.restock(db, order.shop_id, it.variant_id, int(ri["qty"]))
