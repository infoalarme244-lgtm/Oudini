"""Espace commerçant : création guidée, hypothèses, génération, contenu versionné, publication, gouvernance."""
import base64
import copy
import re
from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from sqlalchemy import select

from ..db import get_db
from ..models import (ENGAGING_ACTIONS, AiTrace, ApprovalRequest, AuditLog, AutoPermission, Brief, Job, Membership,
                      PaymentAccount, ShippingZone, Shop, ShopVersion, TaxSetting, User, now, token)
from ..services import actions  # noqa: F401  (enregistre les exécuteurs)
from ..services import analysis as an
from ..services import approvals, brief as briefsvc, catalog, chat_editor, generator, jobs, readiness, versions
from ..services.audit import audit
from ..services.tenancy import get_owned, require_role
from ..web import ShopCtx, check_csrf, login_required, redirect, render, shop_ctx

router = APIRouter(prefix="/app", dependencies=[Depends(check_csrf)])

STEP_KIND = {"auto": "Automatique", "external": "Connexion externe", "validation": "Validation du commerçant"}


@router.get("")
def index(request: Request, user=Depends(login_required), db=Depends(get_db)):
    ms = db.scalars(select(Membership).where(Membership.user_id == user.id)).all()
    return render(request, "merchant/index.html", user=user, memberships=ms)


@router.get("/new")
def new_form(request: Request, expert: int = 0, user=Depends(login_required)):
    from ..services import verticals
    return render(request, "merchant/new.html", user=user, expert=bool(expert), verticals=verticals.PROFILES)


def _unique_slug(db, base):
    slug, i = catalog.slugify(base)[:60] or "boutique", 1
    cand = slug
    while db.scalar(select(Shop).where(Shop.slug == cand)):
        i += 1
        cand = f"{slug}-{i}"
    return cand


@router.post("/new")
async def create(request: Request, prompt: str = Form(""), competitor_url: str = Form(""), name: str = Form(""),
                 demo: str = Form(""), catalog_file: UploadFile | None = File(None),
                 photo_file: UploadFile | None = File(None), photo_rights: str = Form(""),
                 mode: str = Form("expert"), product_type: str = Form(""), countries: list[str] = Form([]),
                 product_url: str = Form(""), language: str = Form("fr"),
                 user=Depends(login_required), db=Depends(get_db)):
    has_photo = bool(photo_file and photo_file.filename)
    simple = mode == "simple"
    facts = None
    if product_url.strip():  # lien d'un produit : lu comme source d'informations, jamais recopié
        from ..services import product_import, verticals
        try:
            facts = product_import.fetch_product(product_url.strip())
        except (product_import.ImportFailed, ValueError) as exc:
            return render(request, "merchant/new.html", user=user, expert=not simple, verticals=verticals.PROFILES,
                          error=f"Lien produit inutilisable : {exc}", status_code=400)
        if not product_type and facts.get("vertical"):
            product_type = facts["vertical"]
    if simple:  # parcours simple : la phrase est construite à partir des choix, rien à rédiger
        from ..services import verticals
        label = verticals.PROFILES[product_type]["label"].lower() if product_type in verticals.PROFILES else ""
        names = {"BE": "la Belgique", "FR": "la France", "LU": "le Luxembourg", "NL": "les Pays-Bas"}
        where = " et ".join(names[c] for c in countries if c in names) or "la France"
        prompt = (f"Boutique de {label} pour {where}. " if label else f"Boutique pour {where}. ") + prompt
    if not prompt.strip() and not competitor_url.strip() and not (catalog_file and catalog_file.filename) and not has_photo and not facts:
        from ..services import verticals
        return render(request, "merchant/new.html", user=user, expert=not simple, verticals=verticals.PROFILES,
                      error="Décrivez votre projet, ou donnez une photo, une URL concurrente ou un catalogue.", status_code=400)
    img = None
    if has_photo:
        from ..services import media
        try:
            img = media.load_clean(await photo_file.read())
        except media.ImageRejected as exc:
            from ..services import verticals
            return render(request, "merchant/new.html", user=user, expert=not simple, verticals=verticals.PROFILES,
                          error=f"Photo refusée : {exc}", status_code=400)
    shop = Shop(slug=_unique_slug(db, name or "ma-boutique"), name=name or "Ma boutique")
    db.add(shop)
    db.flush()
    db.add(Membership(user_id=user.id, shop_id=shop.id, role="owner"))
    res = briefsvc.understand(db, shop, prompt or "")
    product_draft = None
    if img is not None:
        from ..services import media, photo
        photo_url = media.save(shop.id, img)
        result = photo.analyse(db, shop, img, prompt)
        pa, pq, photo_info = photo.to_brief(result, bool(photo_rights), photo_url)
        res = _merge_photo(res, pa, pq, photo_info)
        product_draft = photo.draft_product(result, photo_url, bool(photo_rights), _RIGHTS.get(photo_rights, ""))
    url = competitor_url.strip() or (res["extracted"]["urls"][0] if res["extracted"]["urls"] else None)
    b = Brief(shop_id=shop.id, prompt=prompt, competitor_url=url, extracted=res["extracted"],
              assumptions=res["assumptions"], questions=res["questions"])
    db.add(b)
    db.flush()
    b.positioning_options = generator.positioning_options(b)
    ident = generator.visual_identity(b, shop.slug)
    if not name:
        shop.name = ident["suggested_name"]
        shop.slug = _unique_slug(db, shop.name)
    _sync_shop(shop, b)
    if catalog_file and catalog_file.filename:
        content = await catalog_file.read()
        if len(content) > 5_000_000:
            raise HTTPException(400, "Fichier trop volumineux (5 Mo max)")
        jobs.enqueue(db, shop.id, "catalog_import", {"filename": catalog_file.filename,
                                                      "content_b64": base64.b64encode(content).decode(), "user_id": user.id})
    if demo:
        catalog.seed_demo(db, shop, res["extracted"].get("categories") or [None])
    if facts:
        src_copy = _apply_source_product(db, shop, b, facts, product_type, language if language in ("fr", "nl") else "fr",
                                         has_draft=bool(product_draft))
        if product_draft and product_draft["title"] == "Mon produit":  # photo sans IA : les textes viennent de la fiche réécrite
            product_draft.update(title=src_copy["title"], description=src_copy["description"], brand=facts.get("brand", ""))
    if product_draft:
        if simple and not product_draft["category"] and product_type:
            product_draft["category"] = product_type
        _create_photo_product(db, shop, product_draft)
    audit(db, shop.id, user.id, "shop.created", "shop", shop.id, after={"prompt": prompt[:500], "url": url, "photo": bool(img)})
    if simple:
        # aucune question bloquante : les choix par défaut sont appliqués et restent modifiables
        b.questions = [{**q, "answer": q.get("answer") or ("fr" if q["key"] == "languages" else None)} for q in b.questions]
        _generate(db, shop, b, user, 0, analyse=True)
        db.commit()
        return redirect(f"/app/shops/{shop.id}/lancement")
    db.commit()
    return redirect(f"/app/shops/{shop.id}/brief")


_RIGHTS = {"own": "photo personnelle du commerçant", "supplier": "fournie par le fournisseur avec autorisation"}
TEXT_ORIGINS = ("demande", "déduit des pays")


def _merge_photo(res, photo_assumptions, photo_questions, photo_info):
    """Le texte du commerçant prime ; la photo complète ce qu'il n'a pas précisé (valeurs par défaut)."""
    A = list(res["assumptions"])
    for pa in photo_assumptions:
        cur = next((a for a in A if a["key"] == pa["key"]), None)
        if cur is None:
            A.append(pa)
        elif cur["origin"] not in TEXT_ORIGINS:
            A[A.index(cur)] = pa
    keys = {a["key"] for a in A}
    Q = [q for q in res["questions"] if not (q["key"] == "category" and "category" in keys)]
    for pq in photo_questions:
        if pq["key"] == "supply" and any(q["key"] == "supply" for q in Q):
            continue
        if pq["key"] == "category" and "category" in keys:
            continue
        Q.append(pq)
    ex = dict(res["extracted"])
    ex["photo"] = photo_info
    cat = next((a["value"] for a in A if a["key"] == "category"), None)
    if cat and not ex.get("category"):
        ex["category"], ex["categories"] = cat, [cat]
    brands = next((a["value"] for a in A if a["key"] == "brands"), None)
    if brands and not ex.get("brands"):
        ex["brands"] = brands
    return {"extracted": ex, "assumptions": A, "questions": Q}


def _create_photo_product(db, shop, d):
    """Produit en BROUILLON : prix, stock, marque et provenance restent à renseigner par le commerçant."""
    from ..models import Product, Variant
    p = Product(shop_id=shop.id, handle=catalog.slugify(d["title"]) or "mon-produit", title=d["title"], description=d["description"],
                brand=d.get("brand", ""), category=d["category"], usage=catalog.slugify(d["usage"]) if d["usage"] else "", gender=d["gender"],
                attributes=d["attributes"], images=[d["image"]], status="draft")
    db.add(p)
    db.add(Variant(shop_id=shop.id, product=p, sku=f"P{shop.id}-1", color=d["color"][:60], size="Unique", price_cents=None, stock=0))
    db.flush()
    return p


def _apply_source_product(db, shop, b, facts, product_type, language, has_draft=False):
    """Brief et produit à partir d'une page produit : faits repris, textes réécrits, photos jamais reprises."""
    from ..models import Product, Variant
    from ..services import product_import
    ex = dict(b.extracted or {})
    ex["source_product"] = {k: facts.get(k) for k in ("url", "domain", "name", "brand", "price", "currency", "options",
                                                      "image_count", "retrieved_at", "security_flags")}
    A = [dict(a) for a in b.assumptions]
    if facts.get("brand") and not any(a["key"] == "brands" for a in A):
        A.append({"key": "brands", "label": "Marques", "value": [facts["brand"]], "editable": True,
                  "origin": f"marque indiquée sur {facts['domain']} — à confirmer (ne vaut pas autorisation de revente)"})
        ex["brands"] = [facts["brand"]]
    if language == "nl" and "nl" not in shop.languages:
        shop.languages = ["nl"] + [l for l in shop.languages if l != "nl"]
        shop.default_language = "nl"
    b.assumptions, b.extracted = A, ex
    b.competitor_url = b.competitor_url or facts["url"]
    copy = product_import.write_copy(db, shop, facts, language)
    if has_draft:
        return copy
    p = Product(shop_id=shop.id, handle=catalog.slugify(copy["title"]) or "produit", title=copy["title"],
                description=copy["description"], category=product_type or facts.get("vertical") or "", brand=facts.get("brand", ""),
                status="draft", images=[],
                origin=f"Fiche créée à partir de {facts['domain']} (faits uniquement ; textes réécrits, photos non reprises)")
    db.add(p)
    opts = list((facts.get("options") or {}).items())
    sizes = next((v for k, v in opts if re.search(r"taille|size|pointure|maat|grootte", k, re.I)), None) or ["Unique"]
    colors = next((v for k, v in opts if re.search(r"couleur|colou?r|kleur", k, re.I)), None) or [""]
    combos = [(sz, c) for c in colors for sz in sizes][:30]
    for i, (sz, c) in enumerate(combos, 1):
        db.add(Variant(shop_id=shop.id, product=p, sku=f"P{shop.id}-{i}", size=str(sz)[:20], color=str(c)[:60], price_cents=None, stock=0))
    db.flush()
    return copy


def _sync_shop(shop, b):
    shop.countries = briefsvc.assumption(b, "countries", ["FR"]) or ["FR"]
    shop.currency = briefsvc.assumption(b, "currency", "EUR")
    shop.languages = briefsvc.assumption(b, "languages", ["fr"]) or ["fr"]
    shop.default_language = shop.languages[0]


def _brief(db, shop) -> Brief:
    b = db.scalar(select(Brief).where(Brief.shop_id == shop.id).order_by(Brief.id.desc()))
    if b is None:
        raise HTTPException(404, "Aucune demande")
    return b


@router.get("/shops/{shop_id}/brief")
def brief_view(request: Request, ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    b = _brief(db, ctx.shop)
    ident = generator.visual_identity(b, ctx.shop.slug)
    cols, filters = generator.catalog_structure(b)
    from ..services import verticals
    cats = briefsvc.assumption(b, "categories") or ([briefsvc.assumption(b, "category")] if briefsvc.assumption(b, "category") else [])
    return render(request, "merchant/brief.html", ctx, brief=b, identity=ident, collections=cols, filters=filters,
                  profiles=[verticals.get(c) for c in cats],
                  candidates=an.candidates_for(briefsvc.assumption(b, "categories") or briefsvc.assumption(b, "category")))


@router.post("/shops/{shop_id}/brief/assumptions")
async def brief_assumptions(request: Request, ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    form = await request.form()
    b = _brief(db, ctx.shop)
    before = copy.deepcopy(b.assumptions)
    new = []
    for a in b.assumptions:
        a = dict(a)
        raw = form.get(f"a_{a['key']}")
        if raw is not None:
            if isinstance(a["value"], list):
                val = [x.strip() for x in str(raw).split(",") if x.strip()]
                if a["key"] == "countries":
                    val = [x.upper() for x in val]
                if a["key"] == "categories":
                    val = [catalog.normalise_category(x) for x in val]
            elif isinstance(a["value"], int):
                val = int(raw) if str(raw).isdigit() else a["value"]
            else:
                val = str(raw).strip()
                if a["key"] == "category":
                    val = catalog.normalise_category(val)
            if val != a["value"]:
                a["value"], a["origin"] = val, "modifié par le commerçant"
        new.append(a)
    b.assumptions = new
    b.positioning_options = generator.positioning_options(b)
    _sync_shop(ctx.shop, b)
    audit(db, ctx.shop.id, ctx.user.id, "brief.assumptions", "brief", b.id, before={"a": before}, after={"a": new})
    db.commit()
    return redirect(f"/app/shops/{ctx.shop.id}/brief", "Hypothèses mises à jour")


@router.post("/shops/{shop_id}/brief/questions")
async def brief_questions(request: Request, ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    form = await request.form()
    b = _brief(db, ctx.shop)
    qs, A = [], [dict(a) for a in b.assumptions]
    for q in b.questions:
        q = dict(q)
        ans = form.get(f"q_{q['key']}")
        if ans:
            q["answer"] = str(ans)
            if q["key"] == "languages":
                for a in A:
                    if a["key"] == "languages":
                        langs = ["fr", "nl"] if ans == "fr+nl" else ["fr"]
                        a["value"], a["origin"] = langs + [l for l in a["value"] if l not in langs], "réponse du commerçant"
            if q["key"] == "photo_rights":
                from ..models import Product
                label = {"Oui, c'est ma photo": _RIGHTS["own"], "Oui, autorisation du fournisseur": _RIGHTS["supplier"]}.get(ans, "")
                photo_info = dict((b.extracted or {}).get("photo") or {})
                if photo_info:
                    photo_info["rights"] = bool(label)
                    b.extracted = {**b.extracted, "photo": photo_info}
                    for p in db.scalars(select(Product).where(Product.shop_id == ctx.shop.id)):
                        p.images = [{**i, "license": label} if i.get("url") == photo_info.get("url") else i for i in p.images or []]
                    if ctx.shop.current_version_id:  # boutique déjà générée : la photo de couverture suit le droit déclaré
                        snap = versions.snapshot(db, ctx.shop)
                        snap.setdefault("home", {})["hero_image"] = photo_info["url"] if label else ""
                        versions.create(db, ctx.shop, snap, "Photo de couverture : droit d'utilisation " + ("déclaré" if label else "non déclaré"), ctx.user.id)
            if q["key"] == "category":
                from ..services import verticals
                key = next((k for k, vp in verticals.PROFILES.items() if vp["label"] == ans), None) or catalog.normalise_category(ans)
                A = [a for a in A if a["key"] not in ("category", "categories")]
                A += [{"key": "category", "label": "Catégorie principale", "value": key, "origin": "réponse du commerçant", "editable": True},
                      {"key": "categories", "label": "Catégories vendues", "value": [key], "origin": "réponse du commerçant", "editable": True}]
            if q["key"] == "countries":
                A.append({"key": "countries", "label": "Pays ciblés", "value": [x.strip().upper() for x in ans.split(",")],
                          "origin": "réponse du commerçant", "editable": True})
            if q["key"] == "supply" and ans == "Ce n'est pas cette marque":
                A = [a for a in A if a["key"] != "brands"]
                b.extracted = {**(b.extracted or {}), "brands": []}
            if q["key"] == "supply":
                legal = dict(ctx.shop.legal or {})
                if ans.startswith("Contrat"):
                    legal["reseller_statement"] = ("Distributeur agréé : justificatif à téléverser et vérifier avant d'utiliser "
                                                   "cette mention. En attendant, la boutique se présente comme revendeur indépendant.")
                legal["supply_mode"] = ans
                ctx.shop.legal = legal
        qs.append(q)
    b.questions, b.assumptions = qs, A
    b.positioning_options = generator.positioning_options(b)
    _sync_shop(ctx.shop, b)
    db.commit()
    return redirect(f"/app/shops/{ctx.shop.id}/brief", "Réponses enregistrées")


@router.post("/shops/{shop_id}/generate")
def generate(request: Request, positioning: int = Form(0), analyse: str = Form(""), ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    _generate(db, ctx.shop, _brief(db, ctx.shop), ctx.user, positioning, bool(analyse))
    db.commit()
    return redirect(f"/app/shops/{ctx.shop.id}", "Boutique générée en prévisualisation privée")


def _generate(db, shop, b, user, positioning: int = 0, analyse: bool = True):
    """Génère la boutique (contenu, TVA, zones de livraison, simulateur) puis pré-remplit tout ce qui peut l'être."""
    from ..services import autopilot
    b.chosen_positioning = max(0, min(positioning, len(b.positioning_options) - 1))
    snap = generator.initial_snapshot(shop, b)
    photo_url = ((b.extracted or {}).get("photo") or {}).get("url")
    if photo_url:  # le produit de la photo devient la vedette de l'accueil
        from ..models import Product
        hp = next((p for p in db.scalars(select(Product).where(Product.shop_id == shop.id))
                   if any(i.get("url") == photo_url for i in p.images or [])), None)
        if hp:
            snap["home"]["hero_product"] = hp.handle
    versions.create(db, shop, snap, "Génération initiale", user.id)
    for t in generator.default_tax_settings(shop.countries):
        if not db.scalar(select(TaxSetting).where(TaxSetting.shop_id == shop.id, TaxSetting.country == t["country"])):
            db.add(TaxSetting(shop_id=shop.id, **t))
    if not db.scalar(select(ShippingZone).where(ShippingZone.shop_id == shop.id)):
        for c in shop.countries:
            db.add(ShippingZone(shop_id=shop.id, name=generator.COUNTRY_NAMES.get(c, c), countries=[c], rates=[]))
    if not db.scalar(select(PaymentAccount).where(PaymentAccount.shop_id == shop.id, PaymentAccount.provider == "simulator")):
        db.add(PaymentAccount(shop_id=shop.id, provider="simulator", mode="test", status="connected",
                              account_label="Simulateur intégré (aucun argent réel)"))
    db.flush()
    b.status = "generated"
    autopilot.autofill(db, shop, user)
    if analyse:
        comps = []
        if b.competitor_url:
            comps.append({"name": b.competitor_url.split("//")[-1].split("/")[0], "url": b.competitor_url,
                          "reason": "URL fournie par le commerçant", "origin": "merchant"})
        n = int(briefsvc.assumption(b, "competitor_count", 10) or 10)
        for c in an.candidates_for(briefsvc.assumption(b, "categories") or briefsvc.assumption(b, "category")):
            if len(comps) >= n:
                break
            comps.append(c)
        a = an.start_analysis(db, shop, b, comps)
        jobs.enqueue(db, shop.id, "competitor_analysis", {"analysis_id": a.id})
    audit(db, shop.id, user.id, "shop.generated", "shop", shop.id, after={"positioning": b.chosen_positioning})


@router.get("/shops/{shop_id}")
def overview(request: Request, ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    shop = ctx.shop
    if not shop.current_version_id:
        return redirect(f"/app/shops/{shop.id}/brief")
    r = readiness.check(db, shop)
    jobs_ = db.scalars(select(Job).where(Job.shop_id == shop.id).order_by(Job.id.desc()).limit(5)).all()
    steps = [
        ("Boutique générée (pages, thème, collections, filtres)", "auto", True),
        ("Analyse concurrentielle sourcée", "auto", any(j.kind == "competitor_analysis" and j.status == "done" for j in jobs_)),
        ("Importer le catalogue réel (CSV/XLSX)", "validation", next((i["ok"] for i in r["items"] if i["key"] == "catalog"), False)),
        ("Renseigner les informations légales réelles", "validation", next(i["ok"] for i in r["items"] if i["key"] == "legal_info")),
        ("Configurer tarifs et délais de livraison", "validation", next(i["ok"] for i in r["items"] if i["key"] == "shipping")),
        ("Valider les taux de TVA", "validation", next(i["ok"] for i in r["items"] if i["key"] == "taxes")),
        ("Connecter Stripe (test puis production) — identité et IBAN chez Stripe", "external",
         next(i["ok"] for i in r["items"] if i["key"] == "payment_live")),
        ("Relire et valider les pages légales", "validation", next(i["ok"] for i in r["items"] if i["key"] == "legal_pages")),
        ("Publier (validation explicite)", "validation", shop.status == "published"),
    ]
    from ..services import autopilot
    return render(request, "merchant/overview.html", ctx, readiness=r, steps=steps, step_kind=STEP_KIND, jobs=jobs_,
                  simple_steps=autopilot.steps(db, shop))


# ---------------------------------------------------------------- contenu & édition conversationnelle
@router.get("/shops/{shop_id}/content")
def content(request: Request, ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    vs = db.scalars(select(ShopVersion).where(ShopVersion.shop_id == ctx.shop.id).order_by(ShopVersion.number.desc())).all()
    return render(request, "merchant/content.html", ctx, versions=vs, snap=versions.snapshot(db, ctx.shop))


@router.post("/shops/{shop_id}/content/chat")
def chat(request: Request, message: str = Form(...), ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    v, res = chat_editor.propose(db, ctx.shop, message, ctx.user.id)
    db.commit()
    if v is None:
        return redirect(f"/app/shops/{ctx.shop.id}/content", res["summary"] + ". " + " ".join(res["notes"]))
    return redirect(f"/app/shops/{ctx.shop.id}/content/versions/{v.id}")


@router.get("/shops/{shop_id}/content/versions/{vid}")
def version_view(vid: int, request: Request, ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    v = get_owned(db, ShopVersion, ctx.shop.id, vid)
    cur = versions.snapshot(db, ctx.shop)
    snap = {k: val for k, val in v.snapshot.items() if k != "_meta"}
    return render(request, "merchant/version.html", ctx, version=v, changes=versions.diff(cur, snap),
                  notes=(v.snapshot.get("_meta") or {}).get("notes", []))


@router.post("/shops/{shop_id}/content/versions/{vid}/apply")
def version_apply(vid: int, ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    v = get_owned(db, ShopVersion, ctx.shop.id, vid)
    meta = (v.snapshot or {}).get("_meta") or {}
    v.snapshot = {k: val for k, val in v.snapshot.items() if k != "_meta"}
    versions.apply_proposal(db, ctx.shop, v, ctx.user.id)
    if meta.get("add_language") and meta["add_language"] not in ctx.shop.languages:
        ctx.shop.languages = list(ctx.shop.languages) + [meta["add_language"]]
    db.commit()
    return redirect(f"/app/shops/{ctx.shop.id}/content", f"Version {v.number} appliquée au brouillon (non publiée)")


@router.post("/shops/{shop_id}/content/versions/{vid}/discard")
def version_discard(vid: int, ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    v = get_owned(db, ShopVersion, ctx.shop.id, vid)
    if v.status == "proposed":
        v.status = "discarded"
    db.commit()
    return redirect(f"/app/shops/{ctx.shop.id}/content", "Proposition écartée")


@router.post("/shops/{shop_id}/content/versions/{vid}/rollback")
def version_rollback(vid: int, ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    v = get_owned(db, ShopVersion, ctx.shop.id, vid)
    nv = versions.rollback(db, ctx.shop, v, ctx.user.id)
    db.commit()
    return redirect(f"/app/shops/{ctx.shop.id}/content", f"Retour à la version {v.number} (nouvelle version {nv.number})")


@router.get("/shops/{shop_id}/content/pages/{key}")
def page_edit(key: str, request: Request, ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    snap = versions.snapshot(db, ctx.shop)
    page = snap.get("pages", {}).get(key) or (_ for _ in ()).throw(HTTPException(404, "Page inconnue"))
    return render(request, "merchant/page_edit.html", ctx, key=key, page=page)


@router.post("/shops/{shop_id}/content/pages/{key}")
def page_save(key: str, title: str = Form(...), body: str = Form(...), validated: str = Form(""),
              ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    snap = versions.snapshot(db, ctx.shop)
    if key not in snap.get("pages", {}):
        raise HTTPException(404, "Page inconnue")
    p = snap["pages"][key]
    p.update({"title": title, "body": body})
    if p.get("legal"):
        if validated and "[À COMPLÉTER" in body:
            return redirect(f"/app/shops/{ctx.shop.id}/content/pages/{key}", "Impossible de valider : des champs « À COMPLÉTER » restent")
        p["validated"] = bool(validated)
        p["validated_by"] = ctx.user.email if validated else None
    versions.create(db, ctx.shop, snap, f"Page « {title} » modifiée", ctx.user.id)
    db.commit()
    return redirect(f"/app/shops/{ctx.shop.id}/content", "Page enregistrée")


@router.post("/shops/{shop_id}/content/regenerate-legal")
def regenerate_legal(ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    snap = versions.snapshot(db, ctx.shop)
    snap["pages"].update(generator.legal_pages(ctx.shop, snap.get("verticals")))
    versions.create(db, ctx.shop, snap, "Pages légales regénérées depuis les informations du commerçant", ctx.user.id)
    db.commit()
    return redirect(f"/app/shops/{ctx.shop.id}/content", "Pages légales mises à jour : relecture et validation requises")


# ---------------------------------------------------------------- paramètres
LEGAL_FIELDS = [("company_name", "Raison sociale"), ("legal_form", "Forme juridique"), ("address", "Adresse du siège"),
                ("registration", "N° RCS/SIREN ou BCE/KBO"), ("vat_number", "N° de TVA"), ("email", "Email de contact"),
                ("phone", "Téléphone"), ("publisher", "Directeur de la publication"), ("host", "Hébergeur"),
                ("mediator", "Médiateur de la consommation (France)"), ("return_address", "Adresse de retour")]
COST_FIELDS = [("shipping_cost_cents", "Coût transporteur moyen par colis (€)", True), ("packaging_cents", "Emballage par commande (€)", True),
               ("payment_fee_bp", "Commission paiement (%)", "pct"), ("payment_fee_fixed_cents", "Frais fixes paiement (€)", True),
               ("cac_cents", "Coût publicitaire prévu par commande (€)", True), ("return_rate_bp", "Taux de retour attendu (%)", "pct"),
               ("return_cost_cents", "Coût d'un retour (€)", True), ("fixed_costs_monthly_cents", "Charges fixes mensuelles (€)", True),
               ("min_contribution_pct", "Plancher de marge contributive (%)", "raw")]


@router.get("/shops/{shop_id}/settings")
def settings_view(request: Request, ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    members = db.scalars(select(Membership).where(Membership.shop_id == ctx.shop.id)).all()
    return render(request, "merchant/settings.html", ctx, legal_fields=LEGAL_FIELDS, cost_fields=COST_FIELDS, members=members)


def _to_store(raw, kind):
    if raw in (None, ""):
        return None
    v = float(str(raw).replace(",", "."))
    return int(round(v * 100)) if kind in (True, "pct") else v


@router.post("/shops/{shop_id}/settings")
async def settings_save(request: Request, ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    ctx.need("admin")
    form = await request.form()
    shop = ctx.shop
    before = {"legal": shop.legal, "costs": shop.cost_settings}
    shop.name = str(form.get("name") or shop.name)[:160]
    legal = dict(shop.legal or {})
    for k, _ in LEGAL_FIELDS:
        legal[k] = str(form.get(f"legal_{k}", "")).strip()
    shop.legal = legal
    costs = dict(shop.cost_settings or {})
    try:
        for k, _, kind in COST_FIELDS:
            costs[k] = _to_store(form.get(f"cost_{k}"), kind)
    except ValueError:
        return redirect(f"/app/shops/{shop.id}/settings", "Valeur numérique invalide")
    shop.cost_settings = costs
    if form.get("ai_budget_usd"):
        shop.ai_budget_usd = max(0.0, float(form["ai_budget_usd"]))
    audit(db, shop.id, ctx.user.id, "settings.update", "shop", shop.id, before=before, after={"legal": legal, "costs": costs})
    db.commit()
    return redirect(f"/app/shops/{shop.id}/settings", "Paramètres enregistrés")


@router.post("/shops/{shop_id}/members")
def add_member(email: str = Form(...), role: str = Form("staff"), ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    ctx.need("owner")
    u = db.scalar(select(User).where(User.email == email.strip().lower()))
    if u is None or role not in ("admin", "staff", "viewer"):
        return redirect(f"/app/shops/{ctx.shop.id}/settings", "Utilisateur inconnu (il doit d'abord créer un compte) ou rôle invalide")
    if not db.scalar(select(Membership).where(Membership.user_id == u.id, Membership.shop_id == ctx.shop.id)):
        db.add(Membership(user_id=u.id, shop_id=ctx.shop.id, role=role))
        audit(db, ctx.shop.id, ctx.user.id, "member.added", "user", u.id, after={"role": role})
    db.commit()
    return redirect(f"/app/shops/{ctx.shop.id}/settings", "Membre ajouté")


@router.post("/shops/{shop_id}/domain")
def domain_request(domain: str = Form(...), ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    ctx.need("owner")
    approvals.request(db, ctx.shop, "buy_domain", f"Achat du domaine {domain.strip().lower()}", {"domain": domain.strip().lower()}, ctx.user.id)
    db.commit()
    return redirect(f"/app/shops/{ctx.shop.id}/approvals", "Demande créée : l'achat se fait chez votre registrar après validation")


# ---------------------------------------------------------------- lancement & gouvernance
@router.get("/shops/{shop_id}/launch")
def launch(request: Request, ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    return render(request, "merchant/launch.html", ctx, readiness=readiness.check(db, ctx.shop))


@router.post("/shops/{shop_id}/launch/request")
def launch_request(ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    ctx.need("owner")
    r = readiness.check(db, ctx.shop)
    if not r["can_publish"]:
        return redirect(f"/app/shops/{ctx.shop.id}/launch", f"{len(r['blocking'])} blocage(s) à lever avant publication")
    v = versions.current(db, ctx.shop)
    approvals.request(db, ctx.shop, "publish_store", f"Publier la version {v.number} de la boutique", {"version_id": v.id}, ctx.user.id)
    db.commit()
    return redirect(f"/app/shops/{ctx.shop.id}/approvals", "Demande de publication créée : validez-la pour mettre en ligne")


@router.post("/shops/{shop_id}/unpublish")
def unpublish(ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    ctx.need("owner")
    ctx.shop.status = "paused"
    audit(db, ctx.shop.id, ctx.user.id, "shop.paused", "shop", ctx.shop.id)
    db.commit()
    return redirect(f"/app/shops/{ctx.shop.id}/launch", "Boutique mise hors ligne")


@router.get("/shops/{shop_id}/approvals")
def approvals_view(request: Request, ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    reqs = db.scalars(select(ApprovalRequest).where(ApprovalRequest.shop_id == ctx.shop.id).order_by(ApprovalRequest.id.desc())).all()
    perms = db.scalars(select(AutoPermission).where(AutoPermission.shop_id == ctx.shop.id).order_by(AutoPermission.id.desc())).all()
    return render(request, "merchant/approvals.html", ctx, reqs=reqs, perms=perms, actions=ENGAGING_ACTIONS)


@router.post("/shops/{shop_id}/approvals/{rid}")
def approvals_decide(rid: int, decision: str = Form(...), ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    req = get_owned(db, ApprovalRequest, ctx.shop.id, rid)
    require_role(ctx.membership, "owner" if req.action in ("publish_store", "enable_live_payments", "buy_domain", "launch_paid_campaign") else "admin")
    try:
        approvals.decide(db, ctx.shop, req, decision == "approve", ctx.user.id)
    except ValueError as exc:
        return redirect(f"/app/shops/{ctx.shop.id}/approvals", str(exc))
    db.commit()
    return redirect(f"/app/shops/{ctx.shop.id}/approvals", f"Demande {req.status} : {req.result}")


@router.post("/shops/{shop_id}/permissions")
async def permission_create(request: Request, ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    ctx.need("owner")
    form = await request.form()
    action = str(form.get("action"))
    if action not in ("bulk_price_change", "send_marketing", "launch_paid_campaign"):
        return redirect(f"/app/shops/{ctx.shop.id}/approvals", "Cette action ne peut pas être automatisée")
    limits = {}
    for k in ("max_change_pct", "max_products", "max_recipients", "max_budget_cents"):
        if form.get(k):
            limits[k] = float(form[k]) * (100 if k == "max_budget_cents" else 1)
    if not limits:
        return redirect(f"/app/shops/{ctx.shop.id}/approvals", "Au moins une limite est obligatoire")
    exp = form.get("expires_at")
    p = AutoPermission(shop_id=ctx.shop.id, action=action, limits=limits, created_by=ctx.user.id,
                       expires_at=datetime.fromisoformat(exp) if exp else None)
    db.add(p)
    db.flush()
    audit(db, ctx.shop.id, ctx.user.id, "permission.created", "auto_permission", p.id, after={"action": action, "limits": limits})
    db.commit()
    return redirect(f"/app/shops/{ctx.shop.id}/approvals", "Autorisation automatique créée (révocable à tout moment)")


@router.post("/shops/{shop_id}/permissions/{pid}/revoke")
def permission_revoke(pid: int, ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    ctx.need("owner")
    p = get_owned(db, AutoPermission, ctx.shop.id, pid)
    p.revoked_at = now()
    audit(db, ctx.shop.id, ctx.user.id, "permission.revoked", "auto_permission", p.id)
    db.commit()
    return redirect(f"/app/shops/{ctx.shop.id}/approvals", "Autorisation révoquée")


@router.get("/shops/{shop_id}/traces")
def traces(request: Request, ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    t = db.scalars(select(AiTrace).where(AiTrace.shop_id == ctx.shop.id).order_by(AiTrace.id.desc()).limit(100)).all()
    logs = db.scalars(select(AuditLog).where(AuditLog.shop_id == ctx.shop.id).order_by(AuditLog.id.desc()).limit(100)).all()
    js = db.scalars(select(Job).where(Job.shop_id == ctx.shop.id).order_by(Job.id.desc()).limit(30)).all()
    return render(request, "merchant/traces.html", ctx, traces=t, logs=logs, jobs=js)


@router.post("/shops/{shop_id}/preview-token")
def rotate_preview(ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    ctx.need("admin")
    ctx.shop.preview_token = token()
    db.commit()
    return redirect(f"/app/shops/{ctx.shop.id}", "Lien de prévisualisation renouvelé (l'ancien ne fonctionne plus)")
