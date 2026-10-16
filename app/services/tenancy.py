"""Point d'accès unique aux données d'une boutique : toute lecture passe par shop_id."""
from fastapi import HTTPException
from sqlalchemy import select

from ..models import ROLE_RANK, Membership


def scoped(model, shop_id: int):
    return select(model).where(model.shop_id == shop_id)


def get_owned(db, model, shop_id: int, obj_id: int):
    """Renvoie l'objet uniquement s'il appartient à la boutique ; sinon 404 (pas 403 : on ne révèle rien)."""
    obj = db.get(model, obj_id)
    if obj is None or getattr(obj, "shop_id", None) != shop_id:
        raise HTTPException(status_code=404, detail="Introuvable")
    return obj


def membership_for(db, user_id: int, shop_id: int) -> Membership | None:
    return db.scalar(select(Membership).where(Membership.user_id == user_id, Membership.shop_id == shop_id))


def require_role(m: Membership | None, minimum: str):
    if m is None:
        raise HTTPException(status_code=404, detail="Introuvable")
    if ROLE_RANK[m.role] < ROLE_RANK[minimum]:
        raise HTTPException(status_code=403, detail="Rôle insuffisant")
