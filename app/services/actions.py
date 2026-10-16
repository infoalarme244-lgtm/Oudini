"""Exécution des actions engageantes une fois validées."""
from sqlalchemy import select

from ..models import PaymentAccount, Variant, now
from . import margin, readiness, versions
from .approvals import executor
from .audit import audit


@executor("publish_store")
def publish(db, shop, payload, user_id):
    r = readiness.check(db, shop)
    if not r["can_publish"]:
        raise ValueError("Blocages restants : " + ", ".join(i["label"] for i in r["blocking"]))
    v = versions.current(db, shop)
    v.status = "published"
    shop.published_version_id, shop.status, shop.published_at = v.id, "published", now()
    return f"Version {v.number} publiée"


@executor("enable_live_payments")
def enable_live(db, shop, payload, user_id):
    acc = db.scalar(select(PaymentAccount).where(PaymentAccount.shop_id == shop.id, PaymentAccount.mode == "live",
                                                 PaymentAccount.status == "connected"))
    if acc is None:
        raise ValueError("Aucun compte Stripe live connecté")
    shop.payment_mode = "live"
    return "Paiements réels activés"


@executor("bulk_price_change")
def bulk_price(db, shop, payload, user_id):
    pct = float(payload["change_pct"])
    floor = float((shop.cost_settings or {}).get("min_contribution_pct", 10))
    changed, refused = 0, []
    for vid in payload["variant_ids"]:
        v = db.get(Variant, int(vid))
        if v is None or v.shop_id != shop.id or v.price_cents is None:
            continue
        new_price = round(v.price_cents * (1 + pct / 100))
        if pct < 0:
            test = type("V", (), {"price_cents": new_price, "cost_cents": v.cost_cents})()
            ue = margin.unit_economics(db, shop, test)
            if v.cost_cents is None or (ue["contribution_pct"] or -1) < floor:
                refused.append(v.sku)
                continue
        before = v.price_cents
        v.price_cents = new_price
        audit(db, shop.id, user_id, "price.change", "variant", v.id, before={"price": before}, after={"price": new_price})
        changed += 1
    msg = f"{changed} prix modifié(s)"
    if refused:
        msg += f" ; {len(refused)} refusé(s) car la marge passerait sous {floor:.0f} % ou le coût est inconnu : {', '.join(refused[:10])}"
    return msg
