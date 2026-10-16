"""Veille concurrentielle périodique : prix, disponibilité, conditions de livraison. Alertes sourcées.
Les suggestions de prix respectent un plancher de marge ; rien n'est appliqué automatiquement."""
from datetime import timedelta

from sqlalchemy import select

from ..models import Product, Shop, WatchAlert, WatchTarget, now
from . import margin
from .extractor import extract_page
from .jobs import enqueue, handler
from .url_guard import SafeFetcher

FETCHER_FACTORY = SafeFetcher


def snapshot_of(page: dict) -> dict:
    prods = page["jsonld_products"]
    p = prods[0] if prods else {}
    return {"price": p.get("price"), "currency": p.get("currency"), "availability": p.get("availability"),
            "free_shipping": page["features"].get("free_shipping"), "threshold": page["features"].get("free_shipping_threshold")}


def price_suggestion(db, shop, product: Product, competitor_price: float) -> dict:
    floor_pct = float((shop.cost_settings or {}).get("min_contribution_pct", 10))
    out = []
    for v in product.variants:
        if v.price_cents is None:
            continue
        target = round(competitor_price * 100)
        test = type("V", (), {"price_cents": target, "cost_cents": v.cost_cents})()
        ue = margin.unit_economics(db, shop, test)
        ok = ue["ok"] and v.cost_cents is not None and (ue["contribution_pct"] or -1) >= floor_pct
        out.append({"sku": v.sku, "current": v.price_cents, "competitor": target,
                    "contribution_pct_at_competitor_price": ue.get("contribution_pct"),
                    "recommendation": "alignement possible" if ok else "ne pas s'aligner : marge sous le plancher ou coûts inconnus"})
    return {"floor_pct": floor_pct, "variants": out, "auto_applied": False}


def check_target(db, t: WatchTarget, fetcher) -> list[WatchAlert]:
    shop = db.get(Shop, t.shop_id)
    r = fetcher.fetch(t.url)
    snap = snapshot_of(extract_page(r.url, r.text))
    alerts = []
    old = t.last_snapshot or {}
    if old:
        for key, kind in (("price", "price_change"), ("availability", "availability_change"),
                          ("free_shipping", "shipping_change"), ("threshold", "shipping_change")):
            if old.get(key) != snap.get(key):
                sugg = {}
                if key == "price" and snap.get("price") and t.my_product_id:
                    p = db.get(Product, t.my_product_id)
                    if p and p.shop_id == shop.id:
                        sugg = price_suggestion(db, shop, p, snap["price"])
                a = WatchAlert(shop_id=t.shop_id, target_id=t.id, kind=kind, old_value=str(old.get(key))[:255],
                               new_value=str(snap.get(key))[:255], source_url=r.url, suggestion=sugg)
                db.add(a)
                alerts.append(a)
    t.last_snapshot, t.last_checked_at = snap, now()
    return alerts


@handler("watch_check")
def _job(db, job):
    t = db.get(WatchTarget, job.payload["target_id"])
    alerts = check_target(db, t, FETCHER_FACTORY())
    return {"alerts": len(alerts)}


def schedule_due(db) -> int:
    n = 0
    for t in db.scalars(select(WatchTarget).where(WatchTarget.active.is_(True))):
        if t.last_checked_at is None or t.last_checked_at + timedelta(hours=t.frequency_hours) <= now():
            enqueue(db, t.shop_id, "watch_check", {"target_id": t.id})
            n += 1
    return n
