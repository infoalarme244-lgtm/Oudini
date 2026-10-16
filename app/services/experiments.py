"""Tests A/B : métrique et conditions fixées AVANT le lancement ; aucun gagnant sans données suffisantes."""
import hashlib
import math

from sqlalchemy import select

from ..models import StoreEvent, now

METRICS = {"add_to_cart_rate": ("exposure", "add_to_cart"), "purchase_rate": ("exposure", "purchase")}


def assign(exp, session_id: str) -> dict:
    h = int(hashlib.sha256(f"{exp.id}:{session_id}".encode()).hexdigest(), 16)
    return exp.arms[h % len(exp.arms)]


def validate(exp) -> list[str]:
    errs = []
    if len(exp.arms or []) < 2:
        errs.append("Au moins deux variantes")
    if exp.metric not in METRICS:
        errs.append("Métrique inconnue")
    if exp.min_sample_per_arm < 100:
        errs.append("Échantillon minimum ≥ 100 par variante")
    if not exp.hypothesis:
        errs.append("Hypothèse obligatoire")
    return errs


def results(db, exp) -> dict:
    exp_type, conv_type = METRICS[exp.metric]
    rows = db.execute(select(StoreEvent.session_id, StoreEvent.type, StoreEvent.arm)
                      .where(StoreEvent.shop_id == exp.shop_id, StoreEvent.experiment_id == exp.id)).all()
    arms = {a["key"]: {"exposed": set(), "converted": set()} for a in exp.arms}
    for sid, typ, arm in rows:
        if arm in arms:
            (arms[arm]["exposed"] if typ == exp_type else arms[arm]["converted"] if typ == conv_type else set()).add(sid)
    stats = {}
    for k, d in arms.items():
        n, c = len(d["exposed"]), len(d["converted"] & d["exposed"])
        stats[k] = {"n": n, "conversions": c, "rate": round(c / n * 100, 2) if n else None}
    keys = list(stats)
    verdict, p = "insufficient_data", None
    reasons = []
    days = (now() - exp.started_at).days if exp.started_at else 0
    if any(stats[k]["n"] < exp.min_sample_per_arm for k in keys):
        reasons.append(f"Échantillon minimum non atteint ({exp.min_sample_per_arm} par variante)")
    if days < exp.min_days:
        reasons.append(f"Durée minimale non atteinte ({days}/{exp.min_days} jours)")
    if len(keys) == 2 and all(stats[k]["n"] for k in keys):
        a, b = stats[keys[0]], stats[keys[1]]
        p1, p2 = a["conversions"] / a["n"], b["conversions"] / b["n"]
        pool = (a["conversions"] + b["conversions"]) / (a["n"] + b["n"])
        se = math.sqrt(pool * (1 - pool) * (1 / a["n"] + 1 / b["n"])) if 0 < pool < 1 else 0
        if se:
            z = (p2 - p1) / se
            p = round(math.erfc(abs(z) / math.sqrt(2)), 4)
    if not reasons:
        if p is not None and p < exp.alpha:
            best = max(keys, key=lambda k: stats[k]["rate"] or 0)
            verdict = f"winner:{best}"
        else:
            verdict = "no_significant_difference"
    return {"arms": stats, "p_value": p, "verdict": verdict, "reasons": reasons, "days": days}
