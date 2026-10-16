"""Webhooks prestataires : signature vérifiée, traitement idempotent, aucune session ni CSRF."""
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select

from ..db import get_db
from ..models import PaymentAccount, Shop
from ..services import payments

router = APIRouter(prefix="/webhooks")
log = logging.getLogger("shopforge.webhooks")


@router.post("/stripe/{shop_id}")
async def stripe_webhook(shop_id: int, request: Request, db=Depends(get_db)):
    shop = db.get(Shop, shop_id)
    if shop is None:
        raise HTTPException(404)
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    accs = db.scalars(select(PaymentAccount).where(PaymentAccount.shop_id == shop.id, PaymentAccount.provider == "stripe",
                                                   PaymentAccount.status == "connected")).all()
    event = None
    for acc in accs:  # le secret test ou live valide la signature
        try:
            event = payments.parse_stripe_webhook(acc, payload, sig)
            break
        except payments.PaymentError:
            event = None
    if event is None:
        log.warning("stripe signature invalid", extra={"shop_id": shop.id})
        raise HTTPException(400, "Signature invalide")
    result = payments.handle_stripe_event(db, shop, event)
    db.commit()
    return {"received": True, "result": result}


@router.post("/simulator/{shop_id}")
async def simulator_webhook(shop_id: int, request: Request, db=Depends(get_db)):
    shop = db.get(Shop, shop_id)
    if shop is None:
        raise HTTPException(404)
    try:
        result = payments.handle_simulator_event(db, shop, await request.body(), request.headers.get("x-sim-signature", ""))
    except payments.PaymentError:
        raise HTTPException(400, "Signature invalide")
    db.commit()
    return {"received": True, "result": result}


one_click = APIRouter()


@one_click.post("/unsubscribe/{slug}/{token}")
def unsubscribe_one_click(slug: str, token: str, db=Depends(get_db)):
    """Désabonnement en un clic (RFC 8058) depuis le client mail : le jeton secret tient lieu d'autorisation."""
    from ..models import Customer, now
    shop = db.scalar(select(Shop).where(Shop.slug == slug))
    c = db.scalar(select(Customer).where(Customer.shop_id == shop.id, Customer.unsubscribe_token == token)) if shop else None
    if c is None:
        raise HTTPException(404)
    c.marketing_consent, c.unsubscribed_at = False, c.unsubscribed_at or now()
    db.commit()
    return {"unsubscribed": True}
