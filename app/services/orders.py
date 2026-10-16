"""Panier → commande : calcul des montants, idempotence, machine à états cohérente avec le prestataire."""
from sqlalchemy import func, select

from ..models import Customer, DiscountCode, Order, OrderItem, ShippingZone, TaxSetting, Variant, now
from . import inventory
from .audit import audit

TRANSITIONS = {
    "pending_payment": {"paid", "payment_failed", "cancelled"},
    "payment_failed": {"paid", "pending_payment", "cancelled"},
    "paid": {"fulfilled", "partially_refunded", "refunded"},
    "fulfilled": {"partially_refunded", "refunded"},
    "partially_refunded": {"partially_refunded", "refunded", "fulfilled"},
    "refunded": set(), "cancelled": set(),
}


class InvalidTransition(Exception):
    pass


class CheckoutError(Exception):
    pass


def transition(order: Order, new: str, note: str = ""):
    if new not in TRANSITIONS.get(order.status, set()):
        raise InvalidTransition(f"{order.status} → {new} interdit")
    order.status_history = (order.status_history or []) + [{"from": order.status, "to": new, "at": now().isoformat(), "note": note}]
    order.status = new


def shipping_options(db, shop_id: int, country: str, subtotal_cents: int) -> list[dict]:
    opts = []
    for z in db.scalars(select(ShippingZone).where(ShippingZone.shop_id == shop_id)):
        if country in (z.countries or []):
            for r in z.rates or []:
                free = r.get("free_over_cents")
                price = 0 if free and subtotal_cents >= free else int(r.get("price_cents") or 0)
                opts.append({**r, "zone": z.name, "price_cents": price, "free_applied": bool(free and subtotal_cents >= free)})
    return opts


def validate_discount(db, shop_id: int, code: str, subtotal: int) -> tuple[DiscountCode | None, str]:
    if not code:
        return None, ""
    d = db.scalar(select(DiscountCode).where(DiscountCode.shop_id == shop_id, func.upper(DiscountCode.code) == code.upper()))
    if not d or not d.active:
        return None, "Code inconnu ou inactif"
    t = now()
    if (d.starts_at and t < d.starts_at) or (d.ends_at and t > d.ends_at):
        return None, "Code hors période de validité"
    if d.usage_limit is not None and d.used_count >= d.usage_limit:
        return None, "Code épuisé"
    if subtotal < d.min_subtotal_cents:
        return None, f"Minimum d'achat : {d.min_subtotal_cents/100:.2f} €"
    return d, ""


def price_cart(db, shop, items: list[dict], country: str = "", rate_id: str = "", code: str = "") -> dict:
    lines, errors = [], []
    for it in items:
        v = db.get(Variant, int(it["variant_id"]))
        if v is None or v.shop_id != shop.id or v.product.status != "active":
            errors.append("Article indisponible retiré du panier")
            continue
        if v.price_cents is None:
            errors.append(f"{v.product.title} n'a pas de prix : non vendable")
            continue
        qty = max(1, int(it.get("qty", 1)))
        if qty > v.available:
            errors.append(f"Stock insuffisant pour {v.product.title} {v.label} ({v.available} disponible(s))")
        lines.append({"variant": v, "qty": qty, "unit": v.price_cents, "total": v.price_cents * qty})
    subtotal = sum(l["total"] for l in lines)
    disc, disc_err = validate_discount(db, shop.id, code, subtotal)
    if disc_err:
        errors.append(disc_err)
    discount = 0
    free_ship = False
    if disc:
        if disc.kind == "percent":
            discount = subtotal * min(disc.value, 100) // 100
        elif disc.kind == "fixed":
            discount = min(disc.value, subtotal)
        elif disc.kind == "free_shipping":
            free_ship = True
    shipping, rate = 0, None
    options = shipping_options(db, shop.id, country, subtotal - discount) if country else []
    if country:
        if not options:
            errors.append("Aucune livraison configurée vers ce pays")
        else:
            rate = next((o for o in options if o.get("id") == rate_id), options[0])
            shipping = 0 if free_ship else rate["price_cents"]
    total = subtotal - discount + shipping
    tax = 0
    if country:
        ts = db.scalar(select(TaxSetting).where(TaxSetting.shop_id == shop.id, TaxSetting.country == country))
        if ts:  # prix TTC : TVA incluse = total - total / (1 + taux)
            tax = total - round(total * 10000 / (10000 + ts.rate_bp))
    return {"lines": lines, "subtotal": subtotal, "discount": discount, "discount_obj": disc, "shipping": shipping,
            "rate": rate, "options": options, "tax": tax, "total": total, "errors": errors}


def _next_number(db, shop_id: int) -> str:
    n = (db.scalar(select(func.count(Order.id)).where(Order.shop_id == shop_id)) or 0) + 1
    return f"{shop_id:03d}-{n:05d}"


def create_order(db, shop, items, email, address: dict, country: str, rate_id: str, code: str,
                 idempotency_key: str, channel: str = "direct", marketing_consent: bool = False,
                 used_size_advisor: bool = False) -> Order:
    existing = db.scalar(select(Order).where(Order.shop_id == shop.id, Order.idempotency_key == idempotency_key))
    if existing:
        return existing  # double clic / rechargement : même commande
    priced = price_cart(db, shop, items, country, rate_id, code)
    if priced["errors"]:
        raise CheckoutError("; ".join(priced["errors"]))
    if not priced["lines"]:
        raise CheckoutError("Panier vide")
    if not email or "@" not in email:
        raise CheckoutError("Email invalide")
    customer = db.scalar(select(Customer).where(Customer.shop_id == shop.id, Customer.email == email.lower()))
    if customer is None:
        customer = Customer(shop_id=shop.id, email=email.lower(), name=address.get("name", ""))
        db.add(customer)
    if marketing_consent and not customer.marketing_consent:
        customer.marketing_consent, customer.consent_at, customer.consent_source = True, now(), "case cochée au paiement"
        customer.unsubscribed_at = None
    order = Order(shop_id=shop.id, number=_next_number(db, shop.id), email=email.lower(),
                  currency=shop.currency, subtotal_cents=priced["subtotal"], discount_cents=priced["discount"],
                  shipping_cents=priced["shipping"], tax_cents=priced["tax"], total_cents=priced["total"],
                  country=country, address=address, shipping_method={k: priced["rate"].get(k) for k in
                  ("id", "name", "carrier", "min_days", "max_days", "cost_cents", "zone")} if priced["rate"] else {},
                  discount_code=priced["discount_obj"].code if priced["discount_obj"] else "",
                  idempotency_key=idempotency_key, payment_mode=shop.payment_mode, channel=channel[:60],
                  used_size_advisor=used_size_advisor)
    db.add(order)
    db.flush()
    order.customer_id = customer.id
    for l in priced["lines"]:
        v = l["variant"]
        db.add(OrderItem(shop_id=shop.id, order=order, variant_id=v.id, product_id=v.product_id, title=v.product.title,
                         variant_label=v.label, sku=v.sku, qty=l["qty"], unit_price_cents=l["unit"],
                         unit_cost_cents=v.cost_cents))
    db.flush()
    try:
        inventory.reserve_order(db, order)
    except inventory.OutOfStock as exc:
        raise CheckoutError(str(exc)) from exc
    order.status_history = [{"from": None, "to": "pending_payment", "at": now().isoformat(), "note": "commande créée"}]
    audit(db, shop.id, None, "order.created", "order", order.id, after={"total": order.total_cents, "mode": order.payment_mode})
    return order


def retry_payment(db, order: Order):
    if order.status != "payment_failed":
        raise InvalidTransition("Seule une commande en échec de paiement peut être relancée")
    inventory.reserve_order(db, order)
    transition(order, "pending_payment", "nouvelle tentative de paiement")


def mark_paid(db, order: Order, amount_cents: int, currency: str, provider_ref: str) -> str:
    if order.status == "paid" or order.paid_at:
        return "déjà payée"
    if amount_cents != order.total_cents or currency.upper() != order.currency.upper():
        audit(db, order.shop_id, None, "payment.amount_mismatch", "order", order.id,
              after={"expected": order.total_cents, "received": amount_cents, "currency": currency})
        return "montant incohérent : commande non validée, vérification manuelle requise"
    note = "paiement confirmé par le prestataire"
    try:
        inventory.commit_order(db, order)
    except inventory.OutOfStock:
        note = "PAYÉE MAIS STOCK INSUFFISANT (paiement après expiration) : rembourser ou réapprovisionner"
    transition(order, "paid", note)
    order.paid_at, order.provider_ref = now(), provider_ref or order.provider_ref
    if order.discount_code:
        d = db.scalar(select(DiscountCode).where(DiscountCode.shop_id == order.shop_id, DiscountCode.code == order.discount_code))
        if d:
            d.used_count += 1
    from ..models import EmailOutbox
    db.add(EmailOutbox(shop_id=order.shop_id, to=order.email, subject=f"Commande {order.number} confirmée",
                       body=f"Merci ! Votre paiement de {order.total_cents/100:.2f} {order.currency} est confirmé."))
    return note


def mark_failed(db, order: Order, reason: str = "") -> str:
    if order.status not in ("pending_payment",):
        return f"ignoré (statut {order.status})"
    inventory.release_order(db, order)
    transition(order, "payment_failed", reason or "paiement refusé par le prestataire")
    return "échec enregistré, stock libéré"


def cancel_unpaid(db, order: Order, reason: str) -> str:
    if order.status not in ("pending_payment", "payment_failed"):
        return f"ignoré (statut {order.status})"
    inventory.release_order(db, order)
    transition(order, "cancelled", reason)
    return "annulée, stock libéré"


def fulfil(db, order: Order, tracking: str = ""):
    transition(order, "fulfilled", f"expédiée {tracking}".strip())
    order.fulfilled_at, order.tracking_number = now(), tracking
