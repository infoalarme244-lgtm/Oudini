"""Catalogue : produits, variantes, stocks, fournisseurs, guides des tailles, import/export, réapprovisionnement."""
import base64

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response
from sqlalchemy import select

from ..db import get_db
from ..models import Job, Product, SizeGuide, StockAlert, Supplier, Variant
from ..services import approvals, catalog, inventory, jobs
from ..services.audit import audit
from ..services.catalog import _money, slugify
from ..services.tenancy import get_owned
from ..web import ShopCtx, check_csrf, redirect, render, shop_ctx

router = APIRouter(prefix="/app/shops/{shop_id}", dependencies=[Depends(check_csrf)])


@router.get("/products")
def products(request: Request, q: str = "", ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    ps = db.scalars(select(Product).where(Product.shop_id == ctx.shop.id).order_by(Product.id)).all()
    if q:
        ps = [p for p in ps if slugify(q) in slugify(p.title + " " + p.brand + " " + " ".join(v.sku for v in p.variants))]
    return render(request, "merchant/products.html", ctx, products=ps, q=q,
                  has_demo=any(p.is_demo for p in ps), reorder=inventory.reorder_suggestions(db, ctx.shop.id)[:10])


@router.post("/products/new")
def product_new(title: str = Form(...), ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    handle, i = slugify(title), 1
    while db.scalar(select(Product).where(Product.shop_id == ctx.shop.id, Product.handle == handle)):
        i += 1
        handle = f"{slugify(title)}-{i}"
    p = Product(shop_id=ctx.shop.id, handle=handle, title=title, status="draft")
    db.add(p)
    db.flush()
    audit(db, ctx.shop.id, ctx.user.id, "product.created", "product", p.id)
    db.commit()
    return redirect(f"/app/shops/{ctx.shop.id}/products/{p.id}")


@router.get("/products/{pid}")
def product_view(pid: int, request: Request, ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    p = get_owned(db, Product, ctx.shop.id, pid)
    sups = db.scalars(select(Supplier).where(Supplier.shop_id == ctx.shop.id)).all()
    from ..services import margin
    ues = {v.id: margin.unit_economics(db, ctx.shop, v) for v in p.variants}
    guide, level = catalog.size_guide_for(db, ctx.shop.id, p)
    from ..services import verticals
    vp = verticals.for_product(p)
    alerts = db.scalars(select(StockAlert).where(StockAlert.shop_id == ctx.shop.id,
                                                 StockAlert.variant_id.in_([v.id for v in p.variants] or [0]))).all()
    return render(request, "merchant/product.html", ctx, p=p, suppliers=sups, ues=ues, guide=guide, guide_level=level, alerts=alerts,
                  vp=vp, verticals=verticals.PROFILES)


@router.post("/products/{pid}")
async def product_save(pid: int, request: Request, ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    p = get_owned(db, Product, ctx.shop.id, pid)
    f = await request.form()
    before = {"title": p.title, "status": p.status}
    for k in ("title", "description", "brand", "model", "origin", "authenticity", "authenticity_evidence"):
        if k in f:
            setattr(p, k, str(f[k]).strip())
    for k in ("usage", "gender"):
        if k in f:
            setattr(p, k, slugify(str(f[k])) if f[k] else "")
    p.tags = [slugify(t) for t in str(f.get("tags", "")).split(",") if t.strip()]
    if "category" in f:
        p.category = catalog.normalise_category(str(f["category"])) if f["category"] else ""
    attrs = dict(p.attributes or {})
    for k in [k for k in f.keys() if k.startswith("attr_")]:
        v = str(f[k]).strip()
        if v:
            attrs[k[5:]] = v
        else:
            attrs.pop(k[5:], None)
    p.attributes = attrs
    if f.get("status") in ("active", "draft", "archived"):
        p.status = f["status"]
    sid = f.get("supplier_id")
    p.supplier_id = get_owned(db, Supplier, ctx.shop.id, int(sid)).id if sid else None
    if f.get("image_url"):
        url = str(f["image_url"]).strip()
        if not url.startswith("https://"):
            return redirect(f"/app/shops/{ctx.shop.id}/products/{p.id}", "Image : URL https requise")
        if not f.get("image_license"):
            return redirect(f"/app/shops/{ctx.shop.id}/products/{p.id}", "Image : déclarez vos droits d'utilisation")
        p.images = (p.images or []) + [{"url": url, "alt": p.title, "license": str(f["image_license"])[:200]}]
    if p.status == "active" and p.brand and not p.is_demo and not (p.authenticity or p.authenticity_evidence):
        p.status = "draft"
        msg = "Produit gardé en brouillon : renseignez la provenance/authenticité pour un produit de marque"
    else:
        msg = "Produit enregistré"
    audit(db, ctx.shop.id, ctx.user.id, "product.updated", "product", p.id, before=before, after={"title": p.title, "status": p.status})
    db.commit()
    return redirect(f"/app/shops/{ctx.shop.id}/products/{p.id}", msg)


@router.post("/products/{pid}/images/remove")
def image_remove(pid: int, index: int = Form(...), ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    p = get_owned(db, Product, ctx.shop.id, pid)
    imgs = list(p.images or [])
    if 0 <= index < len(imgs):
        imgs.pop(index)
    p.images = imgs
    db.commit()
    return redirect(f"/app/shops/{ctx.shop.id}/products/{p.id}", "Image retirée")


@router.post("/products/{pid}/variants")
def variant_add(pid: int, sku: str = Form(...), size: str = Form(""), color: str = Form(""), price: str = Form(""),
                cost: str = Form(""), stock: int = Form(0), ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    p = get_owned(db, Product, ctx.shop.id, pid)
    if db.scalar(select(Variant).where(Variant.shop_id == ctx.shop.id, Variant.sku == sku.strip())):
        return redirect(f"/app/shops/{ctx.shop.id}/products/{p.id}", "SKU déjà utilisé")
    try:
        v = Variant(shop_id=ctx.shop.id, product=p, sku=sku.strip(), size=size.strip(), color=color.strip(),
                    price_cents=_money(price), cost_cents=_money(cost), stock=max(0, stock))
    except ValueError:
        return redirect(f"/app/shops/{ctx.shop.id}/products/{p.id}", "Montant invalide")
    db.add(v)
    db.flush()
    audit(db, ctx.shop.id, ctx.user.id, "variant.created", "variant", v.id, after={"sku": v.sku, "stock": v.stock})
    db.commit()
    return redirect(f"/app/shops/{ctx.shop.id}/products/{p.id}", "Variante ajoutée")


@router.post("/variants/{vid}")
def variant_save(vid: int, price: str = Form(""), cost: str = Form(""), stock: int = Form(...), reorder_point: int = Form(2),
                 ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    v = get_owned(db, Variant, ctx.shop.id, vid)
    before = {"price": v.price_cents, "cost": v.cost_cents, "stock": v.stock}
    try:
        v.price_cents, v.cost_cents = _money(price), _money(cost)
        inventory.set_stock(db, v, stock)
    except ValueError as exc:
        return redirect(f"/app/shops/{ctx.shop.id}/products/{v.product_id}", str(exc))
    v.reorder_point = max(0, reorder_point)
    audit(db, ctx.shop.id, ctx.user.id, "variant.updated", "variant", v.id, before=before,
          after={"price": v.price_cents, "cost": v.cost_cents, "stock": v.stock})
    db.commit()
    return redirect(f"/app/shops/{ctx.shop.id}/products/{v.product_id}", "Variante enregistrée")


@router.post("/catalog/purge-demo")
def purge_demo(ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    ctx.need("admin")
    catalog.purge_demo(db, ctx.shop)
    audit(db, ctx.shop.id, ctx.user.id, "catalog.purge_demo", "catalog", "")
    db.commit()
    return redirect(f"/app/shops/{ctx.shop.id}/products", "Données de démonstration supprimées")


@router.get("/import")
def import_view(request: Request, ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    js = db.scalars(select(Job).where(Job.shop_id == ctx.shop.id, Job.kind == "catalog_import").order_by(Job.id.desc()).limit(10)).all()
    return render(request, "merchant/import.html", ctx, jobs=js, columns=catalog.EXPORT_COLS)


@router.post("/import")
async def import_upload(request: Request, file: UploadFile = File(...), dry_run: str = Form(""),
                        ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    content = await file.read()
    if len(content) > 5_000_000:
        raise HTTPException(400, "Fichier trop volumineux (5 Mo max)")
    if not file.filename.lower().endswith((".csv", ".xlsx", ".txt")):
        return redirect(f"/app/shops/{ctx.shop.id}/import", "Format accepté : CSV ou XLSX")
    if dry_run:
        rep = catalog.import_rows(db, ctx.shop, catalog.read_rows(file.filename, content), ctx.user.id, dry_run=True)
        db.rollback()
        return render(request, "merchant/import.html", ctx, jobs=[], columns=catalog.EXPORT_COLS, preview=rep)
    j = jobs.enqueue(db, ctx.shop.id, "catalog_import", {"filename": file.filename, "content_b64": base64.b64encode(content).decode(),
                                                         "user_id": ctx.user.id})
    db.commit()
    return redirect(f"/app/shops/{ctx.shop.id}/import", f"Import lancé (tâche #{j.id})")


@router.get("/export.csv")
def export(ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    return Response(catalog.export_csv(db, ctx.shop), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{ctx.shop.slug}-catalogue.csv"'})


@router.get("/suppliers")
def suppliers(request: Request, ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    s = db.scalars(select(Supplier).where(Supplier.shop_id == ctx.shop.id)).all()
    g = db.scalars(select(SizeGuide).where(SizeGuide.shop_id == ctx.shop.id)).all()
    from ..services import verticals
    return render(request, "merchant/suppliers.html", ctx, suppliers=s, guides=g,
                  fit_verticals=[(k, vp["label"], vp["fit"]) for k, vp in verticals.PROFILES.items() if vp.get("fit")])


@router.post("/suppliers")
def supplier_add(name: str = Form(...), email: str = Form(""), lead_time_days: int = Form(7), notes: str = Form(""),
                 ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    if db.scalar(select(Supplier).where(Supplier.shop_id == ctx.shop.id, Supplier.name == name.strip())):
        return redirect(f"/app/shops/{ctx.shop.id}/suppliers", "Fournisseur déjà existant")
    db.add(Supplier(shop_id=ctx.shop.id, name=name.strip(), email=email, lead_time_days=lead_time_days, notes=notes))
    db.commit()
    return redirect(f"/app/shops/{ctx.shop.id}/suppliers", "Fournisseur ajouté")


def parse_guide_rows(text: str) -> list[dict]:
    """Une ligne par taille : « taille;mesure_max » ou « taille;équivalence;mesure_max » ;
    l'ancien format chaussures « EU;UK;US;mm » reste accepté."""
    parsed = []
    for line in text.strip().splitlines():
        parts = [x.strip() for x in line.split(";")]
        if not parts or not parts[0] or parts[0].lower() in ("eu", "taille", "size"):
            continue
        try:
            if len(parts) >= 4:
                parsed.append({"size": parts[0], "eq": f"UK {parts[1]} / US {parts[2]}", "measure": float(parts[3].replace(",", "."))})
            elif len(parts) == 3:
                parsed.append({"size": parts[0], "eq": parts[1], "measure": float(parts[2].replace(",", "."))})
            elif len(parts) == 2:
                parsed.append({"size": parts[0], "eq": "", "measure": float(parts[1].replace(",", "."))})
            else:
                raise ValueError
        except ValueError:
            raise ValueError(f"Ligne invalide : {line}")
    return parsed


@router.post("/size-guides")
def guide_add(brand: str = Form(...), model: str = Form(""), vertical: str = Form("footwear"), source_url: str = Form(""),
              rows: str = Form(...), notes: str = Form(""), allowance: str = Form(""),
              ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    from ..services import verticals
    try:
        parsed = parse_guide_rows(rows)
    except ValueError as exc:
        return redirect(f"/app/shops/{ctx.shop.id}/suppliers", str(exc))
    if not parsed:
        return redirect(f"/app/shops/{ctx.shop.id}/suppliers", "Format : taille;équivalence;mesure_max (une ligne par taille)")
    vp = verticals.get(vertical)
    if not vp.get("fit"):
        return redirect(f"/app/shops/{ctx.shop.id}/suppliers", "Ce type de produit n'utilise pas de guide des tailles")
    db.add(SizeGuide(shop_id=ctx.shop.id, brand=brand.strip(), model=model.strip(), vertical=vp["key"], measure_label=vp["fit"]["measure_label"],
                     allowance=float(allowance.replace(",", ".")) if allowance.strip() else None,
                     source_url=source_url.strip(), rows=parsed, notes=notes))
    db.commit()
    return redirect(f"/app/shops/{ctx.shop.id}/suppliers", "Guide des tailles ajouté")


@router.post("/prices/bulk")
async def bulk_price(request: Request, ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    ctx.need("admin")
    f = await request.form()
    ids = [int(x) for x in f.getlist("variant_ids")]
    pct = float(str(f.get("change_pct", "0")).replace(",", "."))
    if not ids or pct == 0:
        return redirect(f"/app/shops/{ctx.shop.id}/products", "Sélectionnez des variantes et un pourcentage")
    for i in ids:
        get_owned(db, Variant, ctx.shop.id, i)
    req = approvals.request(db, ctx.shop, "bulk_price_change", f"Modifier de {pct:+.1f} % le prix de {len(ids)} variante(s)",
                            {"variant_ids": ids, "change_pct": pct, "product_count": len(ids)}, ctx.user.id)
    db.commit()
    if req.status == "executed":
        return redirect(f"/app/shops/{ctx.shop.id}/products", "Appliqué via autorisation automatique : " + req.result)
    return redirect(f"/app/shops/{ctx.shop.id}/approvals", "Modification massive en attente de validation")
