"""Vitrine publique (ou prévisualisation privée) d'une boutique."""
import secrets

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

from ..config import settings
from ..db import get_db
from ..models import (Cart, Customer, Experiment, Order, Product, ShippingZone, ShopVersion, Shop, SizeRecommendation,
                      StockAlert, StoreEvent, SupportTicket, Variant, now)
from ..services import assistant, catalog, imagegen, experiments, orders as ordersvc, payments, size_advisor, versions
from ..services.security import hash_password, verify_password
from ..services.tenancy import membership_for
from ..web import check_csrf, csrf, redirect, session, set_session, templates

router = APIRouter(prefix="/s/{slug}", dependencies=[Depends(check_csrf)])


class Store:
    def __init__(self, shop, snap, preview, lang, request):
        self.shop, self.snap, self.preview, self.lang, self.request = shop, snap, preview, lang, request
        self.base = f"/s/{shop.slug}"

    def t(self, s: str) -> str:
        if self.lang == "fr":
            return s
        return (self.snap.get("translations", {}).get(self.lang, {}).get("ui", {}) or {}).get(s, s)

    def page(self, key):
        p = self.snap.get("pages", {}).get(key)
        if p and self.lang != "fr":
            tp = self.snap.get("translations", {}).get(self.lang, {}).get("pages", {}).get(key)
            if tp:
                return {**p, **tp}
        return p


def store_ctx(slug: str, request: Request, db=Depends(get_db)) -> Store:
    shop = db.scalar(select(Shop).where(Shop.slug == slug))
    if shop is None:
        raise HTTPException(404, "Boutique introuvable")
    s = session(request)
    preview = False
    tok = request.query_params.get("preview")
    if tok and secrets.compare_digest(tok, shop.preview_token):
        s.setdefault("previews", [])
        if shop.id not in s["previews"]:
            s["previews"].append(shop.id)
        request.state.new_session = s
    if shop.id in s.get("previews", []) and not tok:
        tok = shop.preview_token  # session déjà autorisée
    if tok == shop.preview_token or (s.get("uid") and membership_for(db, s["uid"], shop.id)):
        preview = True
    if shop.status != "published" and not preview:
        raise HTTPException(404, "Boutique introuvable")
    snap = versions.published_snapshot(db, shop) if (shop.status == "published" and not preview) else versions.snapshot(db, shop)
    vid = request.query_params.get("version")
    if preview and vid and vid.isdigit():
        v = db.get(ShopVersion, int(vid))
        if v and v.shop_id == shop.id:
            snap = {k: val for k, val in v.snapshot.items() if k != "_meta"}
            request.state.new_session = s
    if not snap:
        raise HTTPException(404, "Boutique en cours de génération")
    lang = request.query_params.get("lang") or s.get(f"lang_{shop.id}") or shop.default_language
    if lang not in shop.languages:
        lang = shop.default_language
    if request.query_params.get("lang"):
        s[f"lang_{shop.id}"] = lang
        request.state.new_session = s
    return Store(shop, snap, preview, lang, request)


def page(st: Store, name: str, db, status_code=200, **kw):
    s = session(st.request)
    data = {"request": st.request, "st": st, "shop": st.shop, "snap": st.snap, "theme": st.snap.get("theme", {}),
            "csrf_token": csrf(st.request), "t": st.t, "cart_count": sum(i["qty"] for i in _cart(db, st, create=False).items)
            if _cart(db, st, create=False) else 0, "customer": _customer(db, st), "flash": st.request.query_params.get("flash")}
    data.update(kw)
    resp = templates.TemplateResponse(st.request, name, data, status_code=status_code)
    if getattr(st.request.state, "new_session", None) or "sid" not in s:
        set_session(resp, getattr(st.request.state, "new_session", None) or s)
    return resp


def _event(db, st, type_, product_id=None, experiment_id=None, arm=""):
    s = session(st.request)
    db.add(StoreEvent(shop_id=st.shop.id, session_id=s["sid"], type=type_, product_id=product_id, experiment_id=experiment_id,
                      arm=arm, channel=s.get(f"utm_{st.shop.id}", "")))


def _cart(db, st, create=True) -> Cart | None:
    s = session(st.request)
    tok = s.get(f"cart_{st.shop.id}")
    c = db.scalar(select(Cart).where(Cart.token == tok, Cart.shop_id == st.shop.id)) if tok else None
    if c is None and create:
        c = Cart(shop_id=st.shop.id)
        db.add(c)
        db.flush()
        s[f"cart_{st.shop.id}"] = c.token
        st.request.state.new_session = s
    return c


def _customer(db, st):
    cid = session(st.request).get(f"cust_{st.shop.id}")
    c = db.get(Customer, cid) if cid else None
    return c if c and c.shop_id == st.shop.id else None


def _collections(st, products):
    out = []
    for c in st.snap.get("collections", []):
        items = [p for p in products if catalog.collection_match(p, c["rule"])]
        out.append({**c, "count": len(items), "items": items})
    return out


def _filters(request):
    q = request.query_params

    def cents(name):
        v = q.get(name)
        try:
            return int(float(v) * 100) if v else None
        except ValueError:
            return None
    return {"q": q.get("q", ""), "size": q.get("size", ""), "color": q.get("color", ""), "usage": q.get("usage", ""),
            "gender": q.get("gender", ""), "brand": q.get("brand", ""), "price_min": cents("price_min"),
            "price_max": cents("price_max"), "in_stock": q.get("in_stock") == "1", "sort": q.get("sort", "relevance"),
            "category": q.get("category", ""),
            "attrs": {k[5:]: v for k, v in q.items() if k.startswith("attr_") and v}}


@router.get("")
@router.get("/")
def home(request: Request, st: Store = Depends(store_ctx), db=Depends(get_db)):
    if request.query_params.get("utm_source"):
        s = session(request)
        s[f"utm_{st.shop.id}"] = request.query_params["utm_source"][:60]
        request.state.new_session = s
    products = catalog.sellable(db, st.shop.id)
    cols = _collections(st, products)
    featured = [c for c in cols if c["handle"] in st.snap.get("home", {}).get("featured_collections", [])]
    home_tr = st.snap.get("translations", {}).get(st.lang, {}).get("home", {}) if st.lang != "fr" else {}
    return page(st, "store/home.html", db, collections=cols, featured=featured, products=products[:8],
                home={**st.snap.get("home", {}), **home_tr}, min_price=catalog.min_price)


def _listing(st, db, products, title, description="", handle=""):
    f = _filters(st.request)
    found = catalog.search(products, **f)
    from ..services import verticals
    keys = st.snap.get("verticals") or sorted({p.category for p in products if p.category}) or [None]
    profs = [verticals.get(k) for k in keys]
    attr_filters = []  # [(clé, libellé)]
    for vp in profs:
        for fk in vp["filters"]:
            if fk.startswith("attr:") and fk in st.snap.get("filters", []) and fk[5:] not in [a for a, _ in attr_filters]:
                attr_filters.append((fk[5:], verticals.attribute_label(vp, fk[5:])))
    return page(st, "store/listing.html", db, title=title, description=description, products=found, facets=catalog.facets(products),
                f=f, handle=handle, min_price=catalog.min_price, filters_enabled=st.snap.get("filters", []),
                attr_filters=attr_filters, axes=profs[0]["axes"], usage_label=profs[0]["usage_label"],
                categories=[(vp["key"], vp["label"]) for vp in profs] if len(profs) > 1 else [])


@router.get("/c/{handle}")
def collection(handle: str, st: Store = Depends(store_ctx), db=Depends(get_db)):
    c = next((c for c in st.snap.get("collections", []) if c["handle"] == handle), None)
    if c is None:
        raise HTTPException(404, "Collection introuvable")
    products = [p for p in catalog.sellable(db, st.shop.id) if catalog.collection_match(p, c["rule"])]
    return _listing(st, db, products, c["title"], c.get("description", ""), handle)


@router.get("/search")
def search(st: Store = Depends(store_ctx), db=Depends(get_db)):
    return _listing(st, db, catalog.sellable(db, st.shop.id), st.t("Rechercher"))


def _experiment_title(db, st, p):
    e = db.scalar(select(Experiment).where(Experiment.shop_id == st.shop.id, Experiment.product_id == p.id,
                                           Experiment.status == "running", Experiment.target == "product_title"))
    if not e:
        return p.title, None, ""
    arm = experiments.assign(e, session(st.request)["sid"])
    return arm["value"], e, arm["key"]


@router.get("/product/{handle}")
def product(handle: str, st: Store = Depends(store_ctx), db=Depends(get_db)):
    p = db.scalar(select(Product).where(Product.shop_id == st.shop.id, Product.handle == handle))
    if p is None or (p.status != "active" and not st.preview):
        raise HTTPException(404, "Produit introuvable")
    title, exp, arm = _experiment_title(db, st, p)
    _event(db, st, "view_product", p.id)
    if exp:
        _event(db, st, "exposure", p.id, exp.id, arm)
    db.commit()
    guide, level = catalog.size_guide_for(db, st.shop.id, p)
    zones = db.scalars(select(ShippingZone).where(ShippingZone.shop_id == st.shop.id)).all()
    delays = [{"zone": z.name, "countries": z.countries, "min": min(r["min_days"] for r in z.rates), "max": max(r["max_days"] for r in z.rates)}
              for z in zones if z.rates]
    colors = sorted({v.color for v in p.variants if v.color})
    from ..services import verticals, size_advisor as sa
    vp = verticals.for_product(p)
    specs = [(lbl, (p.attributes or {}).get(k)) for k, lbl, _ in vp["attributes"] if (p.attributes or {}).get(k) not in (None, "")]
    return page(st, "store/product.html", db, p=p, title=title, guide=guide, guide_level=level, delays=delays, colors=colors,
                images=imagegen.public_images(p), vp=vp, specs=specs, highlights=_highlights(p, st.snap),
                guide_rows=sa._rows(guide) if guide else [])


def _highlights(p, snap) -> list[str]:
    """Arguments de la fiche (lignes « - » de la description) ; à défaut, les engagements réels de la boutique."""
    lines = [l[2:].strip() for l in (p.description or "").splitlines() if l.startswith("- ") and l[2:].strip()]
    return lines or list(snap.get("home", {}).get("value_props", []))


@router.post("/cart/add")
def cart_add(variant_id: int = Form(...), qty: int = Form(1), st: Store = Depends(store_ctx), db=Depends(get_db)):
    v = db.get(Variant, variant_id)
    if v is None or v.shop_id != st.shop.id:
        raise HTTPException(404, "Article introuvable")
    if v.price_cents is None or v.available < 1:
        return redirect(f"{st.base}/product/{v.product.handle}", "Indisponible pour le moment")
    cart = _cart(db, st)
    items = [dict(i) for i in cart.items]
    cur = next((i for i in items if i["variant_id"] == v.id), None)
    want = (cur["qty"] if cur else 0) + max(1, qty)
    if want > v.available:
        return redirect(f"{st.base}/product/{v.product.handle}", f"Seulement {v.available} disponible(s)")
    if cur:
        cur["qty"] = want
    else:
        items.append({"variant_id": v.id, "qty": max(1, qty)})
    cart.items = items
    e = db.scalar(select(Experiment).where(Experiment.shop_id == st.shop.id, Experiment.product_id == v.product_id,
                                           Experiment.status == "running"))
    _event(db, st, "add_to_cart", v.product_id, e.id if e else None, experiments.assign(e, session(st.request)["sid"])["key"] if e else "")
    db.commit()
    resp = redirect(f"{st.base}/cart", "Ajouté au panier")
    if getattr(st.request.state, "new_session", None):
        set_session(resp, st.request.state.new_session)
    return resp


@router.get("/cart")
def cart_view(st: Store = Depends(store_ctx), db=Depends(get_db)):
    cart = _cart(db, st, create=False)
    priced = ordersvc.price_cart(db, st.shop, cart.items if cart else [], code=cart.discount_code if cart else "")
    return page(st, "store/cart.html", db, priced=priced, cart=cart)


@router.post("/cart/update")
async def cart_update(request: Request, st: Store = Depends(store_ctx), db=Depends(get_db)):
    f = await request.form()
    cart = _cart(db, st)
    items = []
    for i in cart.items:
        q = int(f.get(f"qty_{i['variant_id']}", i["qty"]) or 0)
        if q > 0:
            items.append({"variant_id": i["variant_id"], "qty": q})
    cart.items = items
    if "discount_code" in f:
        cart.discount_code = str(f["discount_code"]).strip().upper()[:60]
    db.commit()
    return redirect(f"{st.base}/cart")


@router.get("/checkout")
def checkout_form(st: Store = Depends(store_ctx), db=Depends(get_db)):
    cart = _cart(db, st, create=False)
    if not cart or not cart.items:
        return redirect(f"{st.base}/cart")
    country = st.request.query_params.get("country") or (st.shop.countries or ["FR"])[0]
    priced = ordersvc.price_cart(db, st.shop, cart.items, country, st.request.query_params.get("rate", ""), cart.discount_code)
    _event(db, st, "begin_checkout")
    db.commit()
    return page(st, "store/checkout.html", db, priced=priced, country=country, idem=secrets.token_urlsafe(16),
                customer=_customer(db, st), test_mode=st.shop.payment_mode == "test")


@router.post("/checkout")
async def checkout(request: Request, st: Store = Depends(store_ctx), db=Depends(get_db)):
    f = await request.form()
    cart = _cart(db, st, create=False)
    if not cart or not cart.items:
        return redirect(f"{st.base}/cart")
    address = {k: str(f.get(k, "")).strip()[:200] for k in ("name", "line1", "line2", "postal_code", "city")}
    if not all(address[k] for k in ("name", "line1", "postal_code", "city")):
        return redirect(f"{st.base}/checkout?country={f.get('country','')}", "Adresse incomplète")
    s = session(request)
    try:
        order = ordersvc.create_order(db, st.shop, cart.items, str(f.get("email", "")), address, str(f.get("country", "")),
                                      str(f.get("rate", "")), cart.discount_code, str(f.get("idempotency_key") or secrets.token_urlsafe(16)),
                                      s.get(f"utm_{st.shop.id}", "direct") or "direct", bool(f.get("marketing_consent")),
                                      bool(s.get(f"size_used_{st.shop.id}")))
        if order.status != "pending_payment":  # double soumission d'une commande déjà traitée
            db.commit()
            return redirect(f"{st.base}/order/{order.public_token}")
        prov = payments.provider_for(db, st.shop)
        url = prov.create_checkout(order, settings.base_url if prov.name == "stripe" else "")
    except ordersvc.CheckoutError as exc:
        db.rollback()
        return redirect(f"{st.base}/checkout?country={f.get('country','')}", str(exc))
    except payments.PaymentError as exc:
        db.rollback()
        return redirect(f"{st.base}/cart", f"Paiement indisponible : {exc}")
    cust = _customer(db, st)
    if cust:
        order.customer_id = cust.id
    cart.items, cart.discount_code = [], ""
    db.commit()
    return redirect(url)


@router.get("/pay/sim/{token}")
def sim_page(token: str, st: Store = Depends(store_ctx), db=Depends(get_db)):
    o = db.scalar(select(Order).where(Order.public_token == token, Order.shop_id == st.shop.id))
    if o is None or o.provider != "simulator":
        raise HTTPException(404, "Paiement introuvable")
    return page(st, "store/sim_pay.html", db, o=o)


@router.post("/pay/sim/{token}")
def sim_pay(token: str, outcome: str = Form(...), st: Store = Depends(store_ctx), db=Depends(get_db)):
    o = db.scalar(select(Order).where(Order.public_token == token, Order.shop_id == st.shop.id))
    if o is None or o.provider != "simulator" or st.shop.payment_mode != "test":
        raise HTTPException(404, "Paiement introuvable")
    type_ = {"success": "payment.succeeded", "fail": "payment.failed", "cancel": "payment.cancelled"}.get(outcome)
    if not type_:
        raise HTTPException(400, "Issue inconnue")
    body, sig = payments.sim_event(st.shop.id, type_, o)
    payments.handle_simulator_event(db, st.shop, body, sig)  # même chemin qu'un webhook signé
    if o.status == "paid":
        s = session(st.request)
        db.add(StoreEvent(shop_id=st.shop.id, session_id=s["sid"], type="purchase", channel=o.channel))
        e_ids = [it.product_id for it in o.items]
        for e in db.scalars(select(Experiment).where(Experiment.shop_id == st.shop.id, Experiment.status == "running",
                                                     Experiment.product_id.in_(e_ids))):
            db.add(StoreEvent(shop_id=st.shop.id, session_id=s["sid"], type="purchase", experiment_id=e.id,
                              arm=experiments.assign(e, s["sid"])["key"]))
    db.commit()
    return redirect(f"{st.base}/order/{o.public_token}")


@router.post("/order/{token}/retry")
def retry(token: str, st: Store = Depends(store_ctx), db=Depends(get_db)):
    o = db.scalar(select(Order).where(Order.public_token == token, Order.shop_id == st.shop.id))
    if o is None:
        raise HTTPException(404, "Commande introuvable")
    try:
        ordersvc.retry_payment(db, o)
        url = payments.provider_for(db, st.shop).create_checkout(o, settings.base_url) if o.provider == "stripe" \
            else payments.SimulatorProvider(st.shop).create_checkout(o, "")
    except Exception as exc:
        db.rollback()
        return redirect(f"{st.base}/order/{token}", f"Nouvelle tentative impossible : {exc}")
    db.commit()
    return redirect(url)


@router.get("/order/{token}")
def order_status(token: str, st: Store = Depends(store_ctx), db=Depends(get_db)):
    o = db.scalar(select(Order).where(Order.public_token == token, Order.shop_id == st.shop.id))
    if o is None:
        raise HTTPException(404, "Commande introuvable")
    return page(st, "store/order.html", db, o=o)


@router.get("/track")
def track_form(st: Store = Depends(store_ctx), db=Depends(get_db)):
    return page(st, "store/track.html", db)


@router.post("/track")
def track(number: str = Form(...), email: str = Form(...), st: Store = Depends(store_ctx), db=Depends(get_db)):
    o = db.scalar(select(Order).where(Order.shop_id == st.shop.id, Order.number == number.strip(), Order.email == email.strip().lower()))
    if o is None:
        return page(st, "store/track.html", db, error="Aucune commande ne correspond à ce numéro et cet email.", status_code=404)
    return redirect(f"{st.base}/order/{o.public_token}")


@router.get("/account")
def account(st: Store = Depends(store_ctx), db=Depends(get_db)):
    c = _customer(db, st)
    os_ = db.scalars(select(Order).where(Order.shop_id == st.shop.id, Order.email == c.email).order_by(Order.id.desc())).all() if c else []
    return page(st, "store/account.html", db, orders=os_)


@router.post("/account/register")
def register(email: str = Form(...), password: str = Form(...), name: str = Form(""), marketing_consent: str = Form(""),
             st: Store = Depends(store_ctx), db=Depends(get_db)):
    email = email.strip().lower()
    if len(password) < 8 or "@" not in email:
        return redirect(f"{st.base}/account", "Email valide et mot de passe de 8 caractères minimum")
    c = db.scalar(select(Customer).where(Customer.shop_id == st.shop.id, Customer.email == email))
    if c and c.password_hash:
        return redirect(f"{st.base}/account", "Un compte existe déjà : connectez-vous")
    if c is None:
        c = Customer(shop_id=st.shop.id, email=email)
        db.add(c)
    c.password_hash, c.name = hash_password(password), name
    if marketing_consent:
        c.marketing_consent, c.consent_at, c.consent_source, c.unsubscribed_at = True, now(), "création de compte", None
    db.flush()
    s = session(st.request)
    s[f"cust_{st.shop.id}"] = c.id
    db.commit()
    return set_session(redirect(f"{st.base}/account", "Compte créé"), s)


@router.post("/account/login")
def customer_login(email: str = Form(...), password: str = Form(...), st: Store = Depends(store_ctx), db=Depends(get_db)):
    c = db.scalar(select(Customer).where(Customer.shop_id == st.shop.id, Customer.email == email.strip().lower()))
    if not c or not verify_password(password, c.password_hash):
        return redirect(f"{st.base}/account", "Identifiants incorrects")
    s = session(st.request)
    s[f"cust_{st.shop.id}"] = c.id
    return set_session(redirect(f"{st.base}/account"), s)


@router.post("/account/logout")
def customer_logout(st: Store = Depends(store_ctx)):
    s = session(st.request)
    s.pop(f"cust_{st.shop.id}", None)
    return set_session(redirect(f"{st.base}/"), s)


@router.post("/account/return")
def customer_return(token: str = Form(...), reason: str = Form("other"), comment: str = Form(""), st: Store = Depends(store_ctx), db=Depends(get_db)):
    from ..models import ReturnRequest
    c = _customer(db, st)
    o = db.scalar(select(Order).where(Order.public_token == token, Order.shop_id == st.shop.id))
    if o is None or c is None or o.email != c.email or o.status not in ("paid", "fulfilled", "partially_refunded"):
        raise HTTPException(404, "Commande introuvable")
    db.add(ReturnRequest(shop_id=st.shop.id, order_id=o.id, reason=reason if reason in ("size", "defect", "changed_mind", "other") else "other",
                         comment=comment[:2000]))
    db.commit()
    return redirect(f"{st.base}/account", "Demande de retour envoyée")


@router.get("/p/{key}")
def static_page(key: str, st: Store = Depends(store_ctx), db=Depends(get_db)):
    p = st.page(key)
    if p is None:
        raise HTTPException(404, "Page introuvable")
    zones = db.scalars(select(ShippingZone).where(ShippingZone.shop_id == st.shop.id)).all() if p.get("dynamic") == "shipping" else []
    return page(st, "store/page.html", db, p=p, key=key, zones=zones)


@router.post("/contact")
def contact(email: str = Form(...), message: str = Form(...), st: Store = Depends(store_ctx), db=Depends(get_db)):
    db.add(SupportTicket(shop_id=st.shop.id, email=email.strip()[:255], question=message[:4000], context={"via": "contact"}))
    db.commit()
    return redirect(f"{st.base}/p/contact", "Message envoyé : nous vous répondrons par email")


@router.post("/stock-alert")
def stock_alert(variant_id: int = Form(...), email: str = Form(...), st: Store = Depends(store_ctx), db=Depends(get_db)):
    v = db.get(Variant, variant_id)
    if v is None or v.shop_id != st.shop.id:
        raise HTTPException(404, "Article introuvable")
    if not db.scalar(select(StockAlert).where(StockAlert.variant_id == v.id, StockAlert.email == email.strip().lower())):
        db.add(StockAlert(shop_id=st.shop.id, variant_id=v.id, email=email.strip().lower()))
    db.commit()
    return redirect(f"{st.base}/product/{v.product.handle}", "Nous vous écrirons une seule fois, au retour en stock de cette taille")


@router.get("/unsubscribe/{token}")
def unsubscribe_form(token: str, st: Store = Depends(store_ctx), db=Depends(get_db)):
    c = db.scalar(select(Customer).where(Customer.shop_id == st.shop.id, Customer.unsubscribe_token == token))
    if c is None:
        raise HTTPException(404, "Lien invalide")
    return page(st, "store/unsubscribe.html", db, c=c, token=token)


@router.post("/unsubscribe/{token}")
def unsubscribe(token: str, st: Store = Depends(store_ctx), db=Depends(get_db)):
    c = db.scalar(select(Customer).where(Customer.shop_id == st.shop.id, Customer.unsubscribe_token == token))
    if c is None:
        raise HTTPException(404, "Lien invalide")
    c.marketing_consent, c.unsubscribed_at = False, now()
    db.commit()
    return page(st, "store/unsubscribe.html", db, c=c, token=token, done=True)


@router.post("/api/assistant")
async def api_assistant(request: Request, st: Store = Depends(store_ctx), db=Depends(get_db)):
    f = await request.form()
    q = str(f.get("question", ""))[:1000]
    if not q.strip():
        return JSONResponse({"answer": "Posez votre question (taille, usage, budget, livraison, retours…)."})
    res = assistant.answer(db, st.shop, st.snap, q, str(f.get("email", "")))
    db.commit()
    return JSONResponse({**res, "base": st.base})


@router.post("/api/size")
async def api_size(request: Request, st: Store = Depends(store_ctx), db=Depends(get_db)):
    f = await request.form()
    p = db.get(Product, int(f.get("product_id", 0) or 0))
    if p is None or p.shop_id != st.shop.id:
        raise HTTPException(404, "Produit introuvable")
    try:
        foot = float(str(f.get("measure", f.get("foot_mm", ""))).replace(",", "."))
    except ValueError:
        return JSONResponse({"ok": False, "message": "Indiquez votre mesure (nombre)."})
    res = size_advisor.advise(db, st.shop.id, p, foot)
    s = session(request)
    if res.get("ok"):
        db.add(SizeRecommendation(shop_id=st.shop.id, product_id=p.id, session_id=s["sid"], measure=foot,
                                  recommended=res["recommended"], confidence=res["confidence"]))
        s[f"size_used_{st.shop.id}"] = True
    db.commit()
    resp = JSONResponse(res)
    set_session(resp, s)
    return resp
