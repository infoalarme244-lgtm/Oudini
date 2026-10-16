"""Actions engageantes : validation explicite du commerçant, ou autorisation automatique limitée et révocable."""
from sqlalchemy import select

from ..models import ENGAGING_ACTIONS, ApprovalRequest, AutoPermission, now
from .audit import audit

EXECUTORS: dict = {}


class ApprovalRequired(Exception):
    def __init__(self, req):
        super().__init__("Validation requise")
        self.request = req


def executor(action):
    def deco(fn):
        EXECUTORS[action] = fn
        return fn
    return deco


def _within(limits: dict, payload: dict) -> bool:
    checks = {"max_change_pct": "change_pct", "max_products": "product_count", "max_recipients": "recipients",
              "max_budget_cents": "budget_cents"}
    for lk, pk in checks.items():
        if lk in limits and (pk not in payload or abs(float(payload[pk])) > float(limits[lk])):
            return False
    return True


def active_permission(db, shop_id, action, payload):
    perms = db.scalars(select(AutoPermission).where(AutoPermission.shop_id == shop_id, AutoPermission.action == action,
                                                    AutoPermission.revoked_at.is_(None))).all()
    for p in perms:
        if (p.expires_at is None or p.expires_at > now()) and _within(p.limits or {}, payload):
            return p
    return None


def request(db, shop, action: str, summary: str, payload: dict, user_id=None) -> ApprovalRequest:
    assert action in ENGAGING_ACTIONS, action
    req = ApprovalRequest(shop_id=shop.id, action=action, summary=summary, payload=payload, requested_by=user_id)
    db.add(req)
    db.flush()
    # Publication et paiements réels : jamais d'automatisation.
    perm = None if action in ("publish_store", "enable_live_payments", "buy_domain") else active_permission(db, shop.id, action, payload)
    if perm:
        req.status, req.auto_permission_id, req.decided_at = "approved", perm.id, now()
        audit(db, shop.id, None, "approval.auto", "approval", req.id, after={"permission": perm.id, "action": action})
        execute(db, shop, req, None)
    else:
        audit(db, shop.id, user_id, "approval.requested", "approval", req.id, after={"action": action, "summary": summary})
    return req


def decide(db, shop, req: ApprovalRequest, approve: bool, user_id):
    if req.shop_id != shop.id or req.status != "pending":
        raise ValueError("Demande non modifiable")
    req.status, req.decided_by, req.decided_at = ("approved" if approve else "rejected"), user_id, now()
    audit(db, shop.id, user_id, "approval." + req.status, "approval", req.id, after={"action": req.action})
    if approve:
        execute(db, shop, req, user_id)


def execute(db, shop, req: ApprovalRequest, user_id):
    fn = EXECUTORS.get(req.action)
    if fn is None:
        req.status, req.result = "approved", "Validé : action à finaliser manuellement (aucune intégration automatique)"
        return
    try:
        req.result = fn(db, shop, req.payload, user_id) or "exécuté"
        req.status = "executed"
    except Exception as exc:
        req.status, req.result = "failed", str(exc)[:500]
    audit(db, shop.id, user_id, "approval.executed", "approval", req.id, after={"status": req.status, "result": req.result})
