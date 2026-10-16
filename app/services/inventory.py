"""Stock par variante : réservation atomique, engagement au paiement confirmé, libération, remise en stock."""
from datetime import timedelta

from sqlalchemy import func, select, update

from ..models import EmailOutbox, Order, OrderItem, StockAlert, Supplier, Variant, now
from .jobs import handler

RESERVATION_MINUTES = 30


class OutOfStock(Exception):
    def __init__(self, sku):
        super().__init__(f"Stock insuffisant pour {sku}")
        self.sku = sku


def reserve(db, shop_id: int, variant_id: int, qty: int):
    """UPDATE conditionnel : deux clients ne peuvent pas réserver la dernière paire."""
    res = db.execute(update(Variant)
                     .where(Variant.id == variant_id, Variant.shop_id == shop_id, Variant.stock - Variant.reserved >= qty)
                     .values(reserved=Variant.reserved + qty).execution_options(synchronize_session=False))
    if res.rowcount != 1:
        v = db.get(Variant, variant_id)
        raise OutOfStock(v.sku if v else variant_id)


def reserve_order(db, order: Order):
    with db.begin_nested():
        for it in order.items:
            reserve(db, order.shop_id, it.variant_id, it.qty)
    order.stock_state = "reserved"
    order.reservation_expires_at = now() + timedelta(minutes=RESERVATION_MINUTES)
    _refresh(db, order)


def commit_order(db, order: Order):
    if order.stock_state == "committed":
        return
    if order.stock_state == "released":
        reserve_order(db, order)  # peut lever OutOfStock (paiement tardif après expiration)
    for it in order.items:
        db.execute(update(Variant).where(Variant.id == it.variant_id, Variant.shop_id == order.shop_id)
                   .values(stock=Variant.stock - it.qty, reserved=Variant.reserved - it.qty)
                   .execution_options(synchronize_session=False))
    order.stock_state = "committed"
    _refresh(db, order)


def release_order(db, order: Order):
    if order.stock_state != "reserved":
        return
    for it in order.items:
        db.execute(update(Variant).where(Variant.id == it.variant_id, Variant.shop_id == order.shop_id)
                   .values(reserved=Variant.reserved - it.qty).execution_options(synchronize_session=False))
    order.stock_state = "released"
    _refresh(db, order)


def restock(db, shop_id: int, variant_id: int, qty: int):
    v = db.get(Variant, variant_id)
    if v is None or v.shop_id != shop_id:
        return
    was_out = v.available == 0
    db.execute(update(Variant).where(Variant.id == variant_id).values(stock=Variant.stock + qty)
               .execution_options(synchronize_session=False))
    db.refresh(v)
    if was_out and v.available > 0:
        notify_back_in_stock(db, v)


def set_stock(db, variant: Variant, new_stock: int):
    if new_stock < variant.reserved:
        raise ValueError(f"Le stock ne peut pas être inférieur aux réservations en cours ({variant.reserved})")
    was_out = variant.available == 0
    variant.stock = new_stock
    db.flush()
    if was_out and variant.available > 0:
        notify_back_in_stock(db, variant)


def notify_back_in_stock(db, v: Variant):
    alerts = db.scalars(select(StockAlert).where(StockAlert.variant_id == v.id, StockAlert.notified_at.is_(None))).all()
    for a in alerts:
        db.add(EmailOutbox(shop_id=v.shop_id, to=a.email, kind="stock_alert",
                           subject=f"De retour en stock : {v.product.title} ({v.label})",
                           body=f"La variante {v.label} de {v.product.title} est de nouveau disponible. "
                                f"Stock limité aux quantités réellement disponibles ; aucune réservation n'est faite pour vous."))
        a.notified_at = now()


def _refresh(db, order):
    db.flush()
    for it in order.items:
        v = db.get(Variant, it.variant_id)
        if v is not None:
            db.refresh(v)


def expire_reservations(db, shop_id: int | None = None) -> int:
    from .orders import transition
    q = select(Order).where(Order.status.in_(("pending_payment", "payment_failed")), Order.stock_state == "reserved",
                            Order.reservation_expires_at < now())
    if shop_id:
        q = q.where(Order.shop_id == shop_id)
    n = 0
    for o in db.scalars(q):
        release_order(db, o)
        transition(o, "cancelled", "Réservation expirée sans paiement confirmé")
        n += 1
    db.flush()
    return n


@handler("expire_reservations")
def _job_expire(db, job):
    return {"expired": expire_reservations(db, job.shop_id)}


def reorder_suggestions(db, shop_id: int, days: int = 30) -> list[dict]:
    """Suggestion = vitesse de vente réelle (commandes payées) × (délai fournisseur + 14 j de couverture)."""
    since = now() - timedelta(days=days)
    sold = dict(db.execute(select(OrderItem.variant_id, func.sum(OrderItem.qty)).join(Order)
                           .where(Order.shop_id == shop_id, Order.paid_at.is_not(None), Order.paid_at >= since,
                                  Order.payment_mode == "live")
                           .group_by(OrderItem.variant_id)).all())
    out = []
    for v in db.scalars(select(Variant).where(Variant.shop_id == shop_id)):
        lead = v.product.supplier.lead_time_days if v.product.supplier else 7
        velocity = (sold.get(v.id) or 0) / days
        need = round(velocity * (lead + 14)) - v.available
        if v.available <= v.reorder_point or need > 0:
            out.append({"variant": v, "available": v.available, "sold_30d": sold.get(v.id) or 0,
                        "lead_time_days": lead, "suggested_qty": max(need, 0),
                        "basis": "ventes réelles 30 j" if sold.get(v.id) else "aucune vente réelle : quantité à décider",
                        "supplier": v.product.supplier.name if v.product.supplier else "—"})
    return sorted(out, key=lambda x: x["available"])
