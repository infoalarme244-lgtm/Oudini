"""Parcours simple de lancement en 4 étapes : tout est pré-rempli, le commerçant relit et valide."""
from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from ..config import settings
from ..db import get_db
from ..models import Brief, Product, Variant
from ..services import approvals, autopilot, generator, media, payments, readiness, versions
from ..services.audit import audit
from ..services.catalog import _money, slugify
from ..services.tenancy import get_owned
from ..web import ShopCtx, check_csrf, redirect, render, shop_ctx

router = APIRouter(prefix="/app/shops/{shop_id}/lancement", dependencies=[Depends(check_csrf)])
SUPPLY = ["Je fabrique / je crée moi-même", "Grossiste / fournisseur avec factures", "Distributeur agréé (justificatif)",
          "Seconde main / revente"]


def _brief(db, shop):
    return db.scalar(select(Brief).where(Brief.shop_id == shop.id).order_by(Brief.id.desc()))


@router.get("")
def wizard(request: Request, etape: int = 0, ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    shop = ctx.shop
    steps = autopilot.steps(db, shop)
    current = etape or next((s["n"] for s in steps if not s["done"]), 4)
    products = [p for p in db.scalars(select(Product).where(Product.shop_id == shop.id).order_by(Product.id)) if not p.is_demo]
    b = _brief(db, shop)
    brands = (b.extracted or {}).get("brands") if b else []
    ai_filled = bool(((b.extracted or {}).get("photo") or {}).get("vision")) if b else False
    return render(request, "merchant/launch_simple.html", ctx, steps=steps, current=current, products=products,
                  suggestions={p.id: autopilot.price_suggestion(db, shop, p) for p in products},
                  brands=brands, supply=SUPPLY, recap=autopilot.recap(db, shop), readiness=readiness.check(db, shop),
                  platform_mode=payments.platform_mode(), connect=payments.connect_account(db, shop) if payments.platform_mode() else None,
                  lookup=request.query_params.get("lookup"), ai_filled=ai_filled)


# ---------------------------------------------------------------- 1. produit
@router.post("/produit/{pid}")
async def product_save(pid: int, request: Request, photos: list[UploadFile] = File([]), ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    p = get_owned(db, Product, ctx.shop.id, pid)
    f = await request.form()
    try:
        price = _money(f.get("price"))
        stock = int(f.get("stock") or 0)
    except ValueError:
        return redirect(f"/app/shops/{ctx.shop.id}/lancement?etape=1", "Prix ou stock invalide")
    if not price or stock < 0:
        return redirect(f"/app/shops/{ctx.shop.id}/lancement?etape=1", "Indiquez un prix de vente et votre stock")
    p.title = str(f.get("title") or p.title).strip()[:255]
    p.description = str(f.get("description") or "").strip()
    p.description = p.description.replace("\n\n[À COMPLÉTER : vérifiez et complétez cette description.]", "")
    rights = str(f.get("photo_rights") or "")
    for up in photos or []:
        if up and up.filename:
            try:
                url = media.save(ctx.shop.id, media.load_clean(await up.read()))
            except media.ImageRejected as exc:
                return redirect(f"/app/shops/{ctx.shop.id}/lancement?etape=1", f"Photo refusée : {exc}")
            p.images = (p.images or []) + [{"url": url, "alt": p.title, "license": "photo du commerçant" if rights else ""}]
    supply = str(f.get("supply") or "")
    if supply:
        p.origin = p.authenticity = f"Déclaré par le commerçant : {supply}"
        p.brand = str(f.get("brand") or p.brand or "")
    v = p.variants[0] if p.variants else Variant(shop_id=ctx.shop.id, product=p, sku=f"P{ctx.shop.id}-{p.id}", size="Unique")
    if not p.variants:
        db.add(v)
    v.price_cents, v.stock = price, stock
    if not p.handle or p.handle.startswith("mon-produit"):
        p.handle = slugify(p.title) or p.handle
    p.status = "active" if not (p.brand and not p.authenticity) else "draft"
    db.flush()
    audit(db, ctx.shop.id, ctx.user.id, "launch.product", "product", p.id, after={"price": price, "stock": stock, "status": p.status})
    db.commit()
    msg = "Produit prêt à la vente" if p.status == "active" else "Indiquez votre approvisionnement pour ce produit de marque"
    return redirect(f"/app/shops/{ctx.shop.id}/lancement", msg)


# ---------------------------------------------------------------- 2. entreprise
@router.post("/entreprise/recherche")
def company_lookup(number: str = Form(...), ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    if not settings.company_lookup:
        return redirect(f"/app/shops/{ctx.shop.id}/lancement?etape=2", "Recherche automatique désactivée : remplissez le formulaire")
    try:
        data = autopilot.lookup_company(number, transport=LOOKUP_TRANSPORT)
    except autopilot.LookupError_ as exc:
        return redirect(f"/app/shops/{ctx.shop.id}/lancement?etape=2", str(exc))
    legal = dict(ctx.shop.legal or {})
    for k in ("company_name", "legal_form", "address", "registration", "vat_number"):
        if data.get(k):
            legal[k] = data[k]
    legal["return_address"] = legal.get("return_address") or data.get("address", "")
    legal["company_source"] = f"{data['source']} (consulté le {data['retrieved_at']})"
    ctx.shop.legal = legal
    audit(db, ctx.shop.id, ctx.user.id, "launch.company_lookup", "shop", ctx.shop.id, after={"source": data["source"]})
    db.commit()
    return redirect(f"/app/shops/{ctx.shop.id}/lancement?etape=2", "Informations retrouvées : vérifiez-les puis enregistrez")


LOOKUP_TRANSPORT = None  # remplaçable en test


@router.post("/entreprise")
async def company_save(request: Request, ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    f = await request.form()
    legal = dict(ctx.shop.legal or {})
    for k in ("company_name", "legal_form", "address", "registration", "vat_number", "email", "phone", "publisher", "mediator"):
        if k in f:
            legal[k] = str(f[k]).strip()
    legal["return_address"] = legal.get("return_address") or legal.get("address", "")
    ctx.shop.legal = legal
    snap = versions.snapshot(db, ctx.shop)
    snap["pages"].update(generator.legal_pages(ctx.shop, snap.get("verticals")))  # pages légales regénérées avec les vraies infos
    versions.create(db, ctx.shop, snap, "Pages légales mises à jour depuis les informations de l'entreprise", ctx.user.id)
    db.commit()
    return redirect(f"/app/shops/{ctx.shop.id}/lancement", "Entreprise enregistrée, pages légales mises à jour")


# ---------------------------------------------------------------- 3. paiements (Stripe Connect)
CONNECT_CLIENT = None  # remplaçable en test


@router.api_route("/paiements/connecter", methods=["GET", "POST"])
def connect(ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    ctx.need("owner")
    try:
        url = payments.start_connect(db, ctx.shop, ctx.user.email, settings.base_url, client=CONNECT_CLIENT)
    except payments.PaymentError as exc:
        return redirect(f"/app/shops/{ctx.shop.id}/lancement?etape=3", str(exc))
    db.commit()
    return RedirectResponse(url, status_code=303)


@router.get("/paiements/retour")
def connect_return(ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    acc = payments.refresh_connect(db, ctx.shop, client=CONNECT_CLIENT)
    db.commit()
    return redirect(f"/app/shops/{ctx.shop.id}/lancement?etape=3", acc.account_label if acc else "Aucun compte connecté")


# ---------------------------------------------------------------- 4. vérifier et publier
@router.post("/publier")
def publish(confirm: str = Form(""), ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    ctx.need("owner")
    if not confirm:
        return redirect(f"/app/shops/{ctx.shop.id}/lancement?etape=4", "Cochez la case de validation pour continuer")
    blockers = autopilot.confirm_all(db, ctx.shop, ctx.user)
    if blockers:
        db.rollback()
        return redirect(f"/app/shops/{ctx.shop.id}/lancement?etape=4", " ; ".join(blockers))
    acc = payments.connect_account(db, ctx.shop) if payments.platform_mode() else None
    if acc and acc.status == "connected" and acc.mode == "live":
        ctx.shop.payment_mode = "live"  # la case cochée est la validation explicite des paiements réels
    db.flush()
    r = readiness.check(db, ctx.shop)
    if not r["can_publish"]:
        db.commit()  # les validations faites restent acquises
        left = [i["label"] for i in r["blocking"]]
        return redirect(f"/app/shops/{ctx.shop.id}/lancement?etape=4", "Validé. Reste avant la mise en ligne : " + " ; ".join(left))
    v = versions.current(db, ctx.shop)
    req = approvals.request(db, ctx.shop, "publish_store", f"Publication de la version {v.number} (parcours simple)", {"version_id": v.id}, ctx.user.id)
    approvals.decide(db, ctx.shop, req, True, ctx.user.id)
    db.commit()
    return redirect(f"/app/shops/{ctx.shop.id}/lancement", "Votre boutique est en ligne" if ctx.shop.status == "published" else req.result)
