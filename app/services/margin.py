"""Copilote de rentabilité et indicateurs du tableau de bord.

Vocabulaire strict :
- Chiffre d'affaires (CA) : montants encaissés TTC, puis HT ;
- Marge brute : CA HT − coût d'achat des produits vendus ;
- Marge contributive : marge brute − coûts variables (livraison, emballage, frais de paiement, publicité, retours) ;
- Résultat net : uniquement si les charges fixes sont renseignées ; sinon « non disponible ».
"""
from collections import defaultdict
from datetime import timedelta

from sqlalchemy import select

from ..models import ChannelSpend, Order, ReturnRequest, StoreEvent, TaxSetting, now

PAID = ("paid", "fulfilled", "partially_refunded", "refunded")
COST_FIELDS = {
    "shipping_cost_cents": "Coût transporteur moyen par colis",
    "packaging_cents": "Coût d'emballage par commande",
    "payment_fee_bp": "Commission de paiement (%)",
    "payment_fee_fixed_cents": "Frais fixes de paiement par transaction",
    "cac_cents": "Coût publicitaire par commande (prévisionnel)",
    "return_rate_bp": "Taux de retour attendu (%)",
    "return_cost_cents": "Coût d'un retour (port + traitement)",
}


def vat_rate_bp(db, shop, country=None) -> tuple[int | None, str]:
    country = country or (shop.countries or ["FR"])[0]
    ts = db.scalar(select(TaxSetting).where(TaxSetting.shop_id == shop.id, TaxSetting.country == country))
    return (ts.rate_bp, country) if ts else (None, country)


def ht(cents: int, rate_bp: int | None) -> int:
    return cents if rate_bp is None else round(cents * 10000 / (10000 + rate_bp))


def unit_economics(db, shop, variant, discount_pct: float = 0.0, country=None) -> dict:
    """Marge contributive prévisionnelle d'une vente unitaire (commande d'un article)."""
    cs = shop.cost_settings or {}
    missing = []
    rate, ctry = vat_rate_bp(db, shop, country)
    if rate is None:
        missing.append(f"Taux de TVA {ctry}")
    if variant.price_cents is None:
        return {"ok": False, "missing": ["Prix de vente"], "lines": []}
    price_ttc = round(variant.price_cents * (1 - discount_pct / 100))
    price_ht = ht(price_ttc, rate)
    lines = [("Prix de vente TTC", price_ttc), ("TVA", -(price_ttc - price_ht))]
    if variant.cost_cents is None:
        missing.append("Coût d'achat")
    cost = variant.cost_cents or 0
    lines.append(("Coût d'achat", -cost))
    gross = price_ht - cost
    var = {}
    for key, label in COST_FIELDS.items():
        if cs.get(key) in (None, ""):
            missing.append(label)
    var["Livraison"] = int(cs.get("shipping_cost_cents") or 0)
    var["Emballage"] = int(cs.get("packaging_cents") or 0)
    var["Frais de paiement"] = round(price_ttc * float(cs.get("payment_fee_bp") or 0) / 10000) + int(cs.get("payment_fee_fixed_cents") or 0)
    var["Publicité (par commande)"] = int(cs.get("cac_cents") or 0)
    rr = float(cs.get("return_rate_bp") or 0) / 10000
    var["Retours attendus"] = round(rr * (int(cs.get("return_cost_cents") or 0) + var["Livraison"]))
    for k, v in var.items():
        lines.append((k, -v))
    contribution = gross - sum(var.values())
    return {"ok": True, "price_ttc": price_ttc, "price_ht": price_ht, "gross_margin": gross,
            "gross_margin_pct": round(gross / price_ht * 100, 1) if price_ht else None,
            "contribution": contribution, "contribution_pct": round(contribution / price_ht * 100, 1) if price_ht else None,
            "lines": lines, "missing": missing, "reliable": not missing, "vat_country": ctry,
            "deficit": contribution < 0}


def promo_check(db, shop, variants, discount_pct: float) -> list[dict]:
    out = []
    for v in variants:
        ue = unit_economics(db, shop, v, discount_pct)
        if ue["ok"]:
            out.append({"variant": v, "contribution": ue["contribution"], "deficit": ue["deficit"], "missing": ue["missing"]})
    return out


def dashboard(db, shop, mode: str = "live", days: int = 30) -> dict:
    cs = shop.cost_settings or {}
    since = now() - timedelta(days=days)
    orders = db.scalars(select(Order).where(Order.shop_id == shop.id, Order.payment_mode == mode,
                                            Order.status.in_(PAID), Order.paid_at >= since)).all()
    n = len(orders)
    gross_ttc = sum(o.total_cents for o in orders)
    refunded = sum(o.refunded_cents for o in orders)
    tax = sum(o.tax_cents for o in orders)
    net_ttc = gross_ttc - refunded
    # TVA proportionnelle à la part non remboursée
    net_ht = net_ttc - (round(tax * net_ttc / gross_ttc) if gross_ttc else 0)
    cogs, missing_cost_items = 0, 0
    per_order = []
    shipping_missing = 0
    fee_bp, fee_fixed = float(cs.get("payment_fee_bp") or 0), int(cs.get("payment_fee_fixed_cents") or 0)
    pack = int(cs.get("packaging_cents") or 0)
    for o in orders:
        o_cogs, o_missing = 0, False
        restocked = defaultdict(int)  # articles revenus en stock : leur coût n'est pas consommé
        for r in o.refunds:
            if r.status == "succeeded":
                for ri in r.items or []:
                    restocked[int(ri["order_item_id"])] += int(ri["qty"])
        for it in o.items:
            qty = it.qty - restocked.get(it.id, 0)
            if it.unit_cost_cents is None:
                missing_cost_items += qty
                o_missing = o_missing or qty > 0
            else:
                o_cogs += it.unit_cost_cents * qty
        o_cogs_effective = o_cogs
        cogs += o_cogs_effective
        ship_cost = o.shipping_method.get("cost_cents") if o.shipping_method else None
        if ship_cost is None:
            ship_cost = cs.get("shipping_cost_cents")
        if ship_cost is None:
            shipping_missing += 1
        ship_cost = int(ship_cost or 0)
        o_ht = (o.total_cents - o.refunded_cents) - (round(o.tax_cents * (o.total_cents - o.refunded_cents) / o.total_cents) if o.total_cents else 0)
        fees = round(o.total_cents * fee_bp / 10000) + fee_fixed
        o_gross = o_ht - o_cogs_effective
        per_order.append({"order": o, "revenue_ht": o_ht, "gross": o_gross,
                          "contribution_before_ads": o_gross - ship_cost - pack - fees, "incomplete": o_missing or ship_cost == 0})
    months = sorted({(since + timedelta(days=d)).strftime("%Y-%m") for d in range(days + 1)})
    spends = db.scalars(select(ChannelSpend).where(ChannelSpend.shop_id == shop.id, ChannelSpend.month.in_(months))).all()
    ad_spend = sum(s.spend_cents for s in spends)
    returns = db.scalars(select(ReturnRequest).join(Order).where(ReturnRequest.shop_id == shop.id, Order.payment_mode == mode,
                                                                 ReturnRequest.created_at >= since)).all()
    return_cost = len(returns) * int(cs.get("return_cost_cents") or 0)
    shipping_total = sum(int((o.shipping_method or {}).get("cost_cents") or cs.get("shipping_cost_cents") or 0) for o in orders)
    fees_total = sum(round(o.total_cents * fee_bp / 10000) + fee_fixed for o in orders)
    gross_margin = net_ht - cogs
    contribution = gross_margin - shipping_total - pack * n - fees_total - ad_spend - return_cost
    missing = []
    if missing_cost_items:
        missing.append(f"{missing_cost_items} article(s) vendu(s) sans coût d'achat")
    if shipping_missing:
        missing.append(f"Coût transporteur inconnu pour {shipping_missing} commande(s)")
    for key in ("packaging_cents", "payment_fee_bp", "return_cost_cents"):
        if cs.get(key) in (None, ""):
            missing.append(COST_FIELDS[key])
    if not spends:
        missing.append("Dépenses publicitaires non renseignées (CAC non calculable)")
    fixed = cs.get("fixed_costs_monthly_cents")
    net_result = None if fixed in (None, "") or missing else contribution - round(int(fixed) * days / 30)
    # conversion (sessions suivies)
    ev = db.execute(select(StoreEvent.session_id, StoreEvent.type).where(StoreEvent.shop_id == shop.id,
                                                                           StoreEvent.created_at >= since)).all()
    sessions = {s for s, _ in ev}
    buyers = {s for s, t in ev if t == "purchase"}
    first_orders = {}
    for o in orders:
        first_orders.setdefault(o.customer_id, o)
    by_channel = defaultdict(lambda: {"orders": 0, "revenue": 0, "spend": 0})
    for o in orders:
        by_channel[o.channel]["orders"] += 1
        by_channel[o.channel]["revenue"] += o.total_cents - o.refunded_cents
    for s in spends:
        by_channel[s.channel]["spend"] += s.spend_cents
    for c in by_channel.values():
        c["roas"] = round(c["revenue"] / c["spend"], 2) if c["spend"] else None
        c["cac"] = round(c["spend"] / c["orders"]) if c["spend"] and c["orders"] else None
    return {
        "mode": mode, "days": days, "orders": n, "revenue_ttc": gross_ttc, "refunds": refunded,
        "revenue_net_ttc": net_ttc, "revenue_ht": net_ht, "aov": round(net_ttc / n) if n else None,
        "cogs": cogs, "gross_margin": gross_margin, "contribution": contribution, "net_result": net_result,
        "net_result_reason": ("Charges fixes non renseignées" if fixed in (None, "") else
                              "Coûts variables incomplets") if net_result is None else "",
        "missing": missing, "reliable": not missing,
        "sessions": len(sessions), "conversion": round(len(buyers) / len(sessions) * 100, 2) if len(sessions) >= 1 else None,
        "ad_spend": ad_spend, "cac": round(ad_spend / len(first_orders)) if ad_spend and first_orders else None,
        "returns": len(returns), "return_rate": round(len(returns) / n * 100, 1) if n else None,
        "channels": dict(by_channel), "per_order": per_order[-50:],
        "size_advisor": size_advisor_effect(db, shop, mode),
    }


def size_advisor_effect(db, shop, mode="live") -> dict:
    orders = db.scalars(select(Order).where(Order.shop_id == shop.id, Order.payment_mode == mode, Order.status.in_(PAID))).all()
    size_returns = {r.order_id for r in db.scalars(select(ReturnRequest).where(ReturnRequest.shop_id == shop.id, ReturnRequest.reason == "size"))}
    groups = {}
    for used in (True, False):
        g = [o for o in orders if o.used_size_advisor == used]
        r = sum(1 for o in g if o.id in size_returns)
        groups["with" if used else "without"] = {"orders": len(g), "size_returns": r,
                                                 "rate": round(r / len(g) * 100, 1) if g else None}
    enough = min(groups["with"]["orders"], groups["without"]["orders"]) >= 30
    return {**groups, "conclusive": enough,
            "note": "" if enough else "Échantillon insuffisant (< 30 commandes par groupe) : aucun effet ne peut être affirmé."}
