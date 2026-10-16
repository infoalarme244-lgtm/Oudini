"""Pilotage : tableau de bord et marges, analyse concurrentielle, expérimentation, marketing, veille, support."""
from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select

from ..db import get_db
from ..models import (ChannelSpend, Competitor, CompetitorAnalysis, Experiment, FinancialFigure, MarketingDraft, Product,
                      Recommendation, ReviewInsight, Simulation, SupportTicket, Variant, WatchAlert, WatchTarget, now)
from ..services import analysis as an
from ..services import experiments as exps
from ..services import jobs, margin, marketing, versions
from ..services.audit import audit
from ..services.tenancy import get_owned
from ..services.url_guard import UnsafeURL, validate_public_url
from ..web import ShopCtx, check_csrf, redirect, render, shop_ctx

router = APIRouter(prefix="/app/shops/{shop_id}", dependencies=[Depends(check_csrf)])


@router.get("/dashboard")
def dashboard(request: Request, mode: str = "", days: int = 30, ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    mode = mode if mode in ("test", "live") else ctx.shop.payment_mode
    d = margin.dashboard(db, ctx.shop, mode, max(1, min(days, 365)))
    spends = db.scalars(select(ChannelSpend).where(ChannelSpend.shop_id == ctx.shop.id).order_by(ChannelSpend.month.desc())).all()
    return render(request, "merchant/dashboard.html", ctx, d=d, spends=spends)


@router.post("/spend")
def spend_add(channel: str = Form(...), month: str = Form(...), amount: str = Form(...), source: str = Form("saisie commerçant"),
              ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    from ..services.catalog import _money
    db.add(ChannelSpend(shop_id=ctx.shop.id, channel=channel.strip().lower()[:60], month=month[:7], spend_cents=_money(amount) or 0, source=source))
    db.commit()
    return redirect(f"/app/shops/{ctx.shop.id}/dashboard", "Dépense enregistrée")


@router.get("/copilot")
def copilot(request: Request, discount: float = 0, ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    vs = db.scalars(select(Variant).where(Variant.shop_id == ctx.shop.id).order_by(Variant.product_id, Variant.id)).all()
    rows = [(v, margin.unit_economics(db, ctx.shop, v, discount)) for v in vs]
    missing = sorted({m for _, ue in rows for m in ue.get("missing", [])})
    return render(request, "merchant/copilot.html", ctx, rows=rows, discount=discount, missing=missing)


# ---------------------------------------------------------------- analyse concurrentielle
@router.get("/analysis")
def analysis_index(request: Request, ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    a = db.scalar(select(CompetitorAnalysis).where(CompetitorAnalysis.shop_id == ctx.shop.id).order_by(CompetitorAnalysis.id.desc()))
    if a is None:
        a = an.start_analysis(db, ctx.shop, None, [])
        db.commit()
    return redirect(f"/app/shops/{ctx.shop.id}/analysis/{a.id}", request.query_params.get("flash", ""))


@router.get("/analysis/{aid}")
def analysis_view(aid: int, request: Request, ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    a = get_owned(db, CompetitorAnalysis, ctx.shop.id, aid)
    recs = db.scalars(select(Recommendation).where(Recommendation.analysis_id == a.id).order_by(Recommendation.score.desc())).all()
    sims = db.scalars(select(Simulation).where(Simulation.shop_id == ctx.shop.id).order_by(Simulation.id.desc())).all()
    dims = ["positioning", "pricing", "product", "ux", "mobile", "shipping", "returns", "payment", "seo", "social", "ads", "reviews"]
    reviews = {c.id: db.scalars(select(ReviewInsight).where(ReviewInsight.competitor_id == c.id)).all() for c in a.competitors}
    return render(request, "merchant/analysis.html", ctx, a=a, recs=recs, sims=sims, dims=dims, reviews=reviews,
                  criteria=an.SELECTION_CRITERIA)


@router.post("/analysis/{aid}/competitors")
def competitor_add(aid: int, name: str = Form(...), url: str = Form(...), reason: str = Form(""), ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    a = get_owned(db, CompetitorAnalysis, ctx.shop.id, aid)
    try:
        validate_public_url(url)
    except UnsafeURL as exc:
        return redirect(f"/app/shops/{ctx.shop.id}/analysis/{a.id}", f"URL refusée : {exc}")
    db.add(Competitor(shop_id=ctx.shop.id, analysis=a, name=name, url=url.strip(), origin="merchant", selection_reason=reason or "Ajouté par le commerçant"))
    db.commit()
    return redirect(f"/app/shops/{ctx.shop.id}/analysis/{a.id}", "Concurrent ajouté")


@router.post("/competitors/{cid}/delete")
def competitor_delete(cid: int, ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    c = get_owned(db, Competitor, ctx.shop.id, cid)
    aid = c.analysis_id
    db.delete(c)
    db.commit()
    return redirect(f"/app/shops/{ctx.shop.id}/analysis/{aid}", "Concurrent retiré")


@router.post("/analysis/{aid}/run")
def analysis_run(aid: int, ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    a = get_owned(db, CompetitorAnalysis, ctx.shop.id, aid)
    for c in a.competitors:
        if c.status in ("failed",):
            c.status = "pending"
    j = jobs.enqueue(db, ctx.shop.id, "competitor_analysis", {"analysis_id": a.id})
    db.commit()
    return redirect(f"/app/shops/{ctx.shop.id}/analysis/{a.id}", f"Analyse lancée (tâche #{j.id})")


@router.post("/analysis/{aid}/recompute")
def analysis_recompute(aid: int, ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    a = get_owned(db, CompetitorAnalysis, ctx.shop.id, aid)
    from ..models import Brief
    an.recommend(db, ctx.shop, a, db.get(Brief, a.brief_id) if a.brief_id else None)
    db.commit()
    return redirect(f"/app/shops/{ctx.shop.id}/analysis/{a.id}", "Recommandations recalculées")


@router.post("/competitors/{cid}/reviews")
def reviews_add(cid: int, source_url: str = Form(...), text: str = Form(...), ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    c = get_owned(db, Competitor, ctx.shop.id, cid)
    if not source_url.startswith(("http://", "https://")):
        return redirect(f"/app/shops/{ctx.shop.id}/analysis/{c.analysis_id}", "Source des avis obligatoire (lien)")
    ri = an.add_reviews(db, c, source_url, text)
    db.commit()
    return redirect(f"/app/shops/{ctx.shop.id}/analysis/{c.analysis_id}", f"{ri.sample_size} avis analysés")


@router.post("/competitors/{cid}/financials")
async def financial_add(cid: int, request: Request, ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    c = get_owned(db, Competitor, ctx.shop.id, cid)
    f = dict(await request.form())
    f.pop("csrf_token", None)
    errs = an.validate_financial(f)
    if errs:
        return redirect(f"/app/shops/{ctx.shop.id}/analysis/{c.analysis_id}", "Chiffre refusé : " + " ; ".join(errs))
    for old in [x for x in c.financials if x.metric == f["metric"] and x.status == "unavailable"]:
        db.delete(old)
    fig = FinancialFigure(shop_id=ctx.shop.id, competitor_id=c.id, metric=f["metric"], value=float(str(f["value"]).replace(",", ".")),
                          currency=f.get("currency", "EUR"), source_url=f["source_url"], source_title=f.get("source_title", ""),
                          published_on=f["published_on"], period=f["period"], legal_entity=f["legal_entity"], scope=f["scope"],
                          status=f["status"], method=f["method"], confidence=f.get("confidence", "medium"))
    db.add(fig)
    db.flush()
    audit(db, ctx.shop.id, ctx.user.id, "financial.added", "financial_figure", fig.id, after=f)
    db.commit()
    return redirect(f"/app/shops/{ctx.shop.id}/analysis/{c.analysis_id}", "Chiffre sourcé enregistré")


@router.post("/simulations")
async def simulation_add(request: Request, ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    f = await request.form()
    comp = get_owned(db, Competitor, ctx.shop.id, int(f["competitor_id"])) if f.get("competitor_id") else None
    try:
        traffic = float(f["traffic"])
        scen = [{"name": n, "conversion_pct": float(f[f"conv_{k}"]), "aov": float(f[f"aov_{k}"])}
                for k, n in (("low", "Prudent"), ("mid", "Central"), ("high", "Optimiste"))]
    except (KeyError, ValueError):
        return redirect(f"/app/shops/{ctx.shop.id}/analysis", "Valeurs de simulation invalides")
    an.create_simulation(db, ctx.shop, comp, str(f.get("name") or "Simulation"), traffic, str(f.get("traffic_source", "")), scen)
    db.commit()
    return redirect(f"/app/shops/{ctx.shop.id}/analysis", "Simulation créée (séparée des données factuelles)")


@router.post("/recommendations/{rid}")
def recommendation_status(rid: int, status: str = Form(...), ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    r = get_owned(db, Recommendation, ctx.shop.id, rid)
    if status in ("accepted", "dismissed", "proposed"):
        r.status = status
    db.commit()
    return redirect(f"/app/shops/{ctx.shop.id}/analysis/{r.analysis_id}", "Recommandation mise à jour")


# ---------------------------------------------------------------- expérimentation
@router.get("/experiments")
def experiments(request: Request, ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    es = db.scalars(select(Experiment).where(Experiment.shop_id == ctx.shop.id).order_by(Experiment.id.desc())).all()
    prods = db.scalars(select(Product).where(Product.shop_id == ctx.shop.id)).all()
    return render(request, "merchant/experiments.html", ctx, exps=es, results={e.id: exps.results(db, e) for e in es if e.status != "draft"},
                  products=prods, metrics=exps.METRICS)


@router.post("/experiments")
def experiment_add(name: str = Form(...), hypothesis: str = Form(""), product_id: int = Form(...), variant_b: str = Form(...),
                   metric: str = Form("add_to_cart_rate"), min_sample: int = Form(400), min_days: int = Form(7),
                   ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    p = get_owned(db, Product, ctx.shop.id, product_id)
    e = Experiment(shop_id=ctx.shop.id, name=name, hypothesis=hypothesis, target="product_title", product_id=p.id,
                   arms=[{"key": "A", "value": p.title}, {"key": "B", "value": variant_b.strip()}], metric=metric,
                   min_sample_per_arm=min_sample, min_days=min_days)
    errs = exps.validate(e)
    if errs:
        return redirect(f"/app/shops/{ctx.shop.id}/experiments", "Test non créé : " + " ; ".join(errs))
    db.add(e)
    db.commit()
    return redirect(f"/app/shops/{ctx.shop.id}/experiments", "Test créé (brouillon) : conditions figées au lancement")


@router.post("/experiments/{eid}/{action}")
def experiment_action(eid: int, action: str, ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    e = get_owned(db, Experiment, ctx.shop.id, eid)
    if action == "start" and e.status == "draft":
        e.status, e.started_at = "running", now()
    elif action == "stop" and e.status == "running":
        e.status, e.stopped_at = "stopped", now()
    audit(db, ctx.shop.id, ctx.user.id, f"experiment.{action}", "experiment", e.id)
    db.commit()
    return redirect(f"/app/shops/{ctx.shop.id}/experiments", "Test mis à jour")


# ---------------------------------------------------------------- marketing
@router.get("/marketing")
def marketing_view(request: Request, ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    ds = db.scalars(select(MarketingDraft).where(MarketingDraft.shop_id == ctx.shop.id).order_by(MarketingDraft.id.desc())).all()
    return render(request, "merchant/marketing.html", ctx, drafts=ds, audience=len(marketing.audience(db, ctx.shop.id)))


@router.post("/marketing/generate")
def marketing_generate(ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    for t in marketing.draft_templates(db, ctx.shop, versions.snapshot(db, ctx.shop)):
        db.add(MarketingDraft(shop_id=ctx.shop.id, **t))
    db.commit()
    return redirect(f"/app/shops/{ctx.shop.id}/marketing", "Brouillons préparés : à relire et valider")


@router.post("/marketing/{did}")
def marketing_save(did: int, title: str = Form(...), body: str = Form(...), ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    d = get_owned(db, MarketingDraft, ctx.shop.id, did)
    if d.status == "sent":
        return redirect(f"/app/shops/{ctx.shop.id}/marketing", "Déjà envoyé")
    d.title, d.body = title, body
    db.commit()
    return redirect(f"/app/shops/{ctx.shop.id}/marketing", "Brouillon enregistré")


@router.post("/marketing/{did}/send")
def marketing_send(did: int, ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    d = get_owned(db, MarketingDraft, ctx.shop.id, did)
    try:
        req = marketing.request_send(db, ctx.shop, d, ctx.user.id)
    except ValueError as exc:
        return redirect(f"/app/shops/{ctx.shop.id}/marketing", str(exc))
    db.commit()
    return redirect(f"/app/shops/{ctx.shop.id}/approvals" if req.status == "pending" else f"/app/shops/{ctx.shop.id}/marketing",
                    "Envoi en attente de validation" if req.status == "pending" else req.result)


@router.post("/marketing/{did}/campaign")
def marketing_campaign(did: int, budget: str = Form(...), ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    from ..services.catalog import _money
    d = get_owned(db, MarketingDraft, ctx.shop.id, did)
    try:
        marketing.request_campaign(db, ctx.shop, d, _money(budget) or 0, ctx.user.id)
    except ValueError as exc:
        return redirect(f"/app/shops/{ctx.shop.id}/marketing", str(exc))
    db.commit()
    return redirect(f"/app/shops/{ctx.shop.id}/approvals", "Budget de campagne soumis à validation")


# ---------------------------------------------------------------- veille
@router.get("/watch")
def watch_view(request: Request, ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    ts = db.scalars(select(WatchTarget).where(WatchTarget.shop_id == ctx.shop.id)).all()
    als = db.scalars(select(WatchAlert).where(WatchAlert.shop_id == ctx.shop.id).order_by(WatchAlert.id.desc()).limit(50)).all()
    prods = db.scalars(select(Product).where(Product.shop_id == ctx.shop.id)).all()
    return render(request, "merchant/watch.html", ctx, targets=ts, alerts=als, products=prods)


@router.post("/watch")
def watch_add(label: str = Form(...), url: str = Form(...), my_product_id: str = Form(""), frequency_hours: int = Form(24),
              ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    try:
        validate_public_url(url)
    except UnsafeURL as exc:
        return redirect(f"/app/shops/{ctx.shop.id}/watch", f"URL refusée : {exc}")
    pid = get_owned(db, Product, ctx.shop.id, int(my_product_id)).id if my_product_id else None
    db.add(WatchTarget(shop_id=ctx.shop.id, label=label, url=url.strip(), my_product_id=pid, frequency_hours=max(6, frequency_hours)))
    db.commit()
    return redirect(f"/app/shops/{ctx.shop.id}/watch", "Cible de veille ajoutée")


@router.post("/watch/{tid}/check")
def watch_check(tid: int, ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    t = get_owned(db, WatchTarget, ctx.shop.id, tid)
    j = jobs.enqueue(db, ctx.shop.id, "watch_check", {"target_id": t.id})
    db.commit()
    return redirect(f"/app/shops/{ctx.shop.id}/watch", f"Vérification lancée (tâche #{j.id})")


@router.get("/support")
def support(request: Request, ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    ts = db.scalars(select(SupportTicket).where(SupportTicket.shop_id == ctx.shop.id).order_by(SupportTicket.id.desc())).all()
    return render(request, "merchant/support.html", ctx, tickets=ts)


@router.post("/support/{tid}/close")
def support_close(tid: int, ctx: ShopCtx = Depends(shop_ctx), db=Depends(get_db)):
    t = get_owned(db, SupportTicket, ctx.shop.id, tid)
    t.status = "closed"
    db.commit()
    return redirect(f"/app/shops/{ctx.shop.id}/support", "Demande clôturée")
