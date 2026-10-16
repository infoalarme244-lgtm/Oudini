"""Ventes : commandes, remboursements, retours, clients, réductions, livraison, TVA, paiements."""
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select

from ..db import get_db
from ..models import (Customer, DiscountCode, Order, PaymentAccount, PaymentEvent, ReturnRequest, ShippingZone, TaxSetting,
                      Variant)
from ..services import approvals, margin, orders as ordersvc, payments
from ..services.audit import audit
from ..services.catalog import _money
from ..services.security import encrypt_secret, mask
from ..services.tenancy import get_owned
from ..web import ShopCtx, check_csrf, redirect, render, shop_ctx

router = APIRouter(prefix="/app/shops/{shop_id}", dependencies=[Depends(check_csrf)])


@router.get("/orders")
def orders_list(request: Request, status: str = "", mode: str = "", ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    q = select(Order).where(Order.shop_id == ctx.shop.id)
    if status:
        q = q.where(Order.status == status)
    if mode in ("test", "live"):
        q = q.where(Order.payment_mode == mode)
    return render(request, "merchant/orders.html", ctx, orders=db.scalars(q.order_by(Order.id.desc()).limit(200)).all(),
                  status=status, mode=mode)


@router.get("/orders/{oid}")
def order_view(oid: int, request: Request, ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    o = get_owned(db, Order, ctx.shop.id, oid)
    evs = db.scalars(select(PaymentEvent).where(PaymentEvent.shop_id == ctx.shop.id, PaymentEvent.order_id == o.id)
                     .order_by(PaymentEvent.id)).all()
    rets = db.scalars(select(ReturnRequest).where(ReturnRequest.order_id == o.id)).all()
    return render(request, "merchant/order.html", ctx, o=o, events=evs, returns=rets)


@router.post("/orders/{oid}/refund")
async def order_refund(oid: int, request: Request, ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    ctx.need("admin")
    o = get_owned(db, Order, ctx.shop.id, oid)
    f = await request.form()
    try:
        amount = _money(f.get("amount"))
    except ValueError:
        amount = None
    items = [{"order_item_id": it.id, "qty": int(f.get(f"restock_{it.id}") or 0)} for it in o.items if int(f.get(f"restock_{it.id}") or 0) > 0]
    try:
        r = payments.refund_order(db, ctx.shop, o, amount or 0, str(f.get("reason", ""))[:255], items, ctx.user.id)
    except payments.PaymentError as exc:
        db.rollback()
        return redirect(f"/app/shops/{ctx.shop.id}/orders/{o.id}", str(exc))
    rid = f.get("return_id")
    if rid:
        ret = get_owned(db, ReturnRequest, ctx.shop.id, int(rid))
        ret.status = "refunded"
    db.commit()
    return redirect(f"/app/shops/{ctx.shop.id}/orders/{o.id}", f"Remboursement : {r.status}")


@router.post("/orders/{oid}/fulfil")
def order_fulfil(oid: int, tracking: str = Form(""), ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    o = get_owned(db, Order, ctx.shop.id, oid)
    try:
        ordersvc.fulfil(db, o, tracking.strip())
    except ordersvc.InvalidTransition as exc:
        return redirect(f"/app/shops/{ctx.shop.id}/orders/{o.id}", str(exc))
    audit(db, ctx.shop.id, ctx.user.id, "order.fulfilled", "order", o.id, after={"tracking": tracking})
    db.commit()
    return redirect(f"/app/shops/{ctx.shop.id}/orders/{o.id}", "Commande marquée expédiée")


@router.post("/orders/{oid}/cancel")
def order_cancel(oid: int, ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    o = get_owned(db, Order, ctx.shop.id, oid)
    msg = ordersvc.cancel_unpaid(db, o, "annulée par le commerçant")
    audit(db, ctx.shop.id, ctx.user.id, "order.cancel", "order", o.id, after={"result": msg})
    db.commit()
    return redirect(f"/app/shops/{ctx.shop.id}/orders/{o.id}", msg)


@router.post("/orders/{oid}/return")
def order_return(oid: int, reason: str = Form("other"), comment: str = Form(""), ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    o = get_owned(db, Order, ctx.shop.id, oid)
    db.add(ReturnRequest(shop_id=ctx.shop.id, order_id=o.id, reason=reason if reason in ("size", "defect", "changed_mind", "other") else "other",
                         comment=comment))
    db.commit()
    return redirect(f"/app/shops/{ctx.shop.id}/orders/{o.id}", "Retour enregistré")


@router.get("/returns")
def returns(request: Request, ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    rs = db.scalars(select(ReturnRequest).where(ReturnRequest.shop_id == ctx.shop.id).order_by(ReturnRequest.id.desc())).all()
    return render(request, "merchant/returns.html", ctx, returns=rs)


@router.get("/customers")
def customers(request: Request, ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    cs = db.scalars(select(Customer).where(Customer.shop_id == ctx.shop.id).order_by(Customer.id.desc())).all()
    return render(request, "merchant/customers.html", ctx, customers=cs)


@router.get("/discounts")
def discounts(request: Request, ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    ds = db.scalars(select(DiscountCode).where(DiscountCode.shop_id == ctx.shop.id)).all()
    variants = db.scalars(select(Variant).where(Variant.shop_id == ctx.shop.id)).all()
    checks = {d.id: margin.promo_check(db, ctx.shop, variants, d.value) for d in ds if d.kind == "percent"}
    return render(request, "merchant/discounts.html", ctx, discounts=ds, checks=checks)


@router.post("/discounts")
def discount_add(code: str = Form(...), kind: str = Form("percent"), value: str = Form("0"), min_subtotal: str = Form(""),
                 usage_limit: str = Form(""), starts_at: str = Form(""), ends_at: str = Form(""),
                 ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    code = code.strip().upper()
    if not code or db.scalar(select(DiscountCode).where(DiscountCode.shop_id == ctx.shop.id, DiscountCode.code == code)):
        return redirect(f"/app/shops/{ctx.shop.id}/discounts", "Code vide ou déjà existant")
    if kind not in ("percent", "fixed", "free_shipping"):
        return redirect(f"/app/shops/{ctx.shop.id}/discounts", "Type invalide")
    try:
        val = int(float(value)) if kind == "percent" else (_money(value) or 0)
        if kind == "percent" and not 0 < val <= 90:
            raise ValueError
    except ValueError:
        return redirect(f"/app/shops/{ctx.shop.id}/discounts", "Valeur invalide (1–90 %)")
    d = DiscountCode(shop_id=ctx.shop.id, code=code, kind=kind, value=val, min_subtotal_cents=_money(min_subtotal) or 0,
                     usage_limit=int(usage_limit) if usage_limit else None,
                     starts_at=datetime.fromisoformat(starts_at) if starts_at else None,
                     ends_at=datetime.fromisoformat(ends_at) if ends_at else None)
    db.add(d)
    db.flush()
    msg = "Code créé"
    if kind == "percent":
        variants = db.scalars(select(Variant).where(Variant.shop_id == ctx.shop.id)).all()
        deficit = [c for c in margin.promo_check(db, ctx.shop, variants, val) if c["deficit"]]
        if deficit:
            msg += f" — ATTENTION : {len(deficit)} variante(s) deviendraient déficitaires avec -{val} %"
    audit(db, ctx.shop.id, ctx.user.id, "discount.created", "discount", d.id, after={"code": code, "kind": kind, "value": val})
    db.commit()
    return redirect(f"/app/shops/{ctx.shop.id}/discounts", msg)


@router.post("/discounts/{did}/toggle")
def discount_toggle(did: int, ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    d = get_owned(db, DiscountCode, ctx.shop.id, did)
    d.active = not d.active
    db.commit()
    return redirect(f"/app/shops/{ctx.shop.id}/discounts", "Code " + ("activé" if d.active else "désactivé"))


@router.get("/shipping")
def shipping(request: Request, ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    zs = db.scalars(select(ShippingZone).where(ShippingZone.shop_id == ctx.shop.id)).all()
    return render(request, "merchant/shipping.html", ctx, zones=zs)


@router.post("/shipping/zones")
def zone_add(name: str = Form(...), countries: str = Form(...), ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    db.add(ShippingZone(shop_id=ctx.shop.id, name=name, countries=[c.strip().upper() for c in countries.split(",") if c.strip()]))
    db.commit()
    return redirect(f"/app/shops/{ctx.shop.id}/shipping", "Zone ajoutée")


@router.post("/shipping/zones/{zid}/rates")
def rate_add(zid: int, name: str = Form(...), carrier: str = Form(""), price: str = Form(...), free_over: str = Form(""),
             min_days: int = Form(...), max_days: int = Form(...), cost: str = Form(""), ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    z = get_owned(db, ShippingZone, ctx.shop.id, zid)
    if min_days < 0 or max_days < min_days:
        return redirect(f"/app/shops/{ctx.shop.id}/shipping", "Délais incohérents")
    try:
        rate = {"id": uuid.uuid4().hex[:8], "name": name, "carrier": carrier, "price_cents": _money(price) or 0,
                "free_over_cents": _money(free_over), "min_days": min_days, "max_days": max_days, "cost_cents": _money(cost)}
    except ValueError:
        return redirect(f"/app/shops/{ctx.shop.id}/shipping", "Montant invalide")
    z.rates = list(z.rates or []) + [rate]
    audit(db, ctx.shop.id, ctx.user.id, "shipping.rate_added", "shipping_zone", z.id, after=rate)
    db.commit()
    return redirect(f"/app/shops/{ctx.shop.id}/shipping", "Tarif ajouté")


@router.post("/shipping/zones/{zid}/rates/{rid}/delete")
def rate_delete(zid: int, rid: str, ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    z = get_owned(db, ShippingZone, ctx.shop.id, zid)
    z.rates = [r for r in z.rates or [] if r.get("id") != rid]
    db.commit()
    return redirect(f"/app/shops/{ctx.shop.id}/shipping", "Tarif supprimé")


@router.get("/taxes")
def taxes(request: Request, ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    ts = db.scalars(select(TaxSetting).where(TaxSetting.shop_id == ctx.shop.id)).all()
    return render(request, "merchant/taxes.html", ctx, taxes=ts)


@router.post("/taxes/{tid}")
def tax_save(tid: int, rate: str = Form(...), validated: str = Form(""), ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    ctx.need("admin")
    t = get_owned(db, TaxSetting, ctx.shop.id, tid)
    before = {"rate": t.rate_bp, "validated": t.validated}
    t.rate_bp = int(round(float(rate.replace(",", ".")) * 100))
    t.validated = bool(validated)
    audit(db, ctx.shop.id, ctx.user.id, "tax.updated", "tax", t.id, before=before, after={"rate": t.rate_bp, "validated": t.validated})
    db.commit()
    return redirect(f"/app/shops/{ctx.shop.id}/taxes", "TVA enregistrée")


@router.get("/payments")
def payments_view(request: Request, ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    accs = db.scalars(select(PaymentAccount).where(PaymentAccount.shop_id == ctx.shop.id)).all()
    evs = db.scalars(select(PaymentEvent).where(PaymentEvent.shop_id == ctx.shop.id).order_by(PaymentEvent.id.desc()).limit(30)).all()
    from ..config import settings
    return render(request, "merchant/payments.html", ctx, accounts=accs, events=evs, mask=mask,
                  webhook_url=f"{settings.base_url}/webhooks/stripe/{ctx.shop.id}")


@router.post("/payments/stripe")
def stripe_connect(mode: str = Form(...), secret_key: str = Form(...), webhook_secret: str = Form(...),
                   publishable_key: str = Form(""), ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    ctx.need("owner")
    secret_key, webhook_secret = secret_key.strip(), webhook_secret.strip()
    try:
        payments.validate_stripe_keys(mode, secret_key, webhook_secret)
    except payments.PaymentError as exc:
        return redirect(f"/app/shops/{ctx.shop.id}/payments", str(exc))
    acc = db.scalar(select(PaymentAccount).where(PaymentAccount.shop_id == ctx.shop.id, PaymentAccount.provider == "stripe",
                                                 PaymentAccount.mode == mode))
    if acc is None:
        acc = PaymentAccount(shop_id=ctx.shop.id, provider="stripe", mode=mode)
        db.add(acc)
    label = "Clés enregistrées (non vérifiées en ligne)"
    try:  # vérification facultative auprès de Stripe : échoue proprement sans réseau
        import stripe
        a = stripe.StripeClient(secret_key).v1.accounts.retrieve_current()
        label = f"Compte {a.id} — paiements {'activés' if a.charges_enabled else 'non activés (vérification Stripe à finaliser)'}"
    except Exception as exc:
        label = f"Clés enregistrées ; vérification en ligne impossible ({type(exc).__name__})"
    acc.secret_enc, acc.webhook_secret_enc = encrypt_secret(secret_key), encrypt_secret(webhook_secret)
    acc.publishable_key, acc.status, acc.account_label = publishable_key.strip(), "connected", label
    audit(db, ctx.shop.id, ctx.user.id, "payment.connected", "payment_account", mode, after={"provider": "stripe", "mode": mode, "key": mask(secret_key)})
    db.commit()
    return redirect(f"/app/shops/{ctx.shop.id}/payments", f"Stripe {mode} connecté. {label}")


@router.post("/payments/live/request")
def live_request(ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    ctx.need("owner")
    approvals.request(db, ctx.shop, "enable_live_payments", "Activer les paiements réels (Stripe live)", {}, ctx.user.id)
    db.commit()
    return redirect(f"/app/shops/{ctx.shop.id}/approvals", "Demande d'activation des paiements réels créée")


@router.post("/payments/test-mode")
def back_to_test(ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    ctx.need("owner")
    ctx.shop.payment_mode = "test"
    audit(db, ctx.shop.id, ctx.user.id, "payment.mode_test", "shop", ctx.shop.id)
    db.commit()
    return redirect(f"/app/shops/{ctx.shop.id}/payments", "Retour au mode test")
