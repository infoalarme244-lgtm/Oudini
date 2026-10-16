"""Versions de contenu : proposer → prévisualiser → appliquer ; retour à une version précédente."""
import copy

from sqlalchemy import func, select

from ..models import ShopVersion
from .audit import audit


def current(db, shop) -> ShopVersion | None:
    return db.get(ShopVersion, shop.current_version_id) if shop.current_version_id else None


def snapshot(db, shop) -> dict:
    v = current(db, shop)
    return copy.deepcopy(v.snapshot) if v else {}


def published_snapshot(db, shop) -> dict:
    v = db.get(ShopVersion, shop.published_version_id) if shop.published_version_id else None
    return copy.deepcopy(v.snapshot) if v else {}


def _next_number(db, shop_id) -> int:
    return (db.scalar(select(func.max(ShopVersion.number)).where(ShopVersion.shop_id == shop_id)) or 0) + 1


def create(db, shop, snap: dict, message: str, user_id=None, status="draft") -> ShopVersion:
    v = ShopVersion(shop_id=shop.id, number=_next_number(db, shop.id), snapshot=copy.deepcopy(snap),
                    message=message, status=status, parent_id=shop.current_version_id, created_by=user_id)
    db.add(v)
    db.flush()
    if status == "draft":
        shop.current_version_id = v.id
    audit(db, shop.id, user_id, f"version.{status}", "shop_version", v.id, after={"message": message})
    return v


def apply_proposal(db, shop, version: ShopVersion, user_id=None):
    if version.shop_id != shop.id or version.status != "proposed":
        raise ValueError("Proposition invalide")
    version.status = "draft"
    shop.current_version_id = version.id
    audit(db, shop.id, user_id, "version.applied", "shop_version", version.id)


def rollback(db, shop, target: ShopVersion, user_id=None) -> ShopVersion:
    if target.shop_id != shop.id:
        raise ValueError("Version d'une autre boutique")
    return create(db, shop, target.snapshot, f"Retour à la version {target.number}", user_id)


def diff(a, b, path="") -> list[dict]:
    """Différences lisibles entre deux instantanés."""
    out = []
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            out += diff(a.get(k), b.get(k), f"{path}.{k}" if path else k)
    elif a != b:
        out.append({"path": path, "before": a, "after": b})
    return out
