import json
import logging

from ..models import AiTrace, AuditLog

log = logging.getLogger("shopforge.audit")


def _clean(d):
    if d is None:
        return None
    return json.loads(json.dumps(d, default=str))


def audit(db, shop_id, user_id, action, entity="", entity_id="", before=None, after=None):
    db.add(AuditLog(shop_id=shop_id, user_id=user_id, action=action, entity=entity, entity_id=str(entity_id),
                    before=_clean(before), after=_clean(after)))
    log.info("audit", extra={"shop_id": shop_id, "action": action, "entity": entity, "entity_id": entity_id})


def trace(db, shop_id, feature, engine="rules", input_summary="", sources=None, assumptions=None, output=None,
          tokens_in=0, tokens_out=0, cost_usd=0.0):
    t = AiTrace(shop_id=shop_id, feature=feature, engine=engine, input_summary=input_summary[:4000],
                sources=_clean(sources or []), assumptions=_clean(assumptions or []), output=_clean(output or {}),
                tokens_in=tokens_in, tokens_out=tokens_out, cost_usd=cost_usd)
    db.add(t)
    return t
