"""Outils web partagés : templates, sessions, CSRF, contexte commerçant."""
import json
import re
import secrets
from pathlib import Path

from fastapi import Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from .db import get_db
from .models import Shop, User
from .services.security import csrf_token_for, read_session, sign_session
from .services.tenancy import membership_for, require_role

BASE = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE / "templates"))
SESSION_COOKIE = "sf_session"


def money(cents, currency="EUR"):
    if cents is None:
        return "—"
    s = f"{cents/100:,.2f}".replace(",", " ").replace(".", ",")
    return f"{s} €" if currency == "EUR" else f"{s} {currency}"


def md(text: str) -> Markup:
    """Mini-markdown sûr (échappement systématique) : ## titres, listes, gras, paragraphes."""
    out = []
    for block in re.split(r"\n\s*\n", (text or "").strip()):
        lines = block.split("\n")
        if lines[0].startswith("## "):
            out.append(f"<h2>{escape(lines[0][3:])}</h2>")
            lines = lines[1:]
            if not lines:
                continue
        if all(l.startswith("- ") for l in lines if l):
            out.append("<ul>" + "".join(f"<li>{_inline(l[2:])}</li>" for l in lines if l) + "</ul>")
        else:
            out.append("<p>" + "<br>".join(_inline(l) for l in lines) + "</p>")
    return Markup("\n".join(out))


def _inline(s):
    s = str(escape(s))
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    return re.sub(r"\[(À COMPLÉTER[^\]]*)\]", r'<mark class="todo">[\1]</mark>', s)


templates.env.filters["money"] = money
templates.env.filters["md"] = md
templates.env.filters["tojson_pretty"] = lambda v: json.dumps(v, ensure_ascii=False, indent=2, default=str)
templates.env.globals["STATUS_FR"] = {
    "pending_payment": "En attente de paiement", "paid": "Payée", "payment_failed": "Paiement échoué", "cancelled": "Annulée",
    "fulfilled": "Expédiée", "partially_refunded": "Remboursée partiellement", "refunded": "Remboursée"}
from .services.imagegen import public_images  # noqa: E402
templates.env.globals["public_images"] = public_images
templates.env.globals["CONF_FR"] = {"high": "élevée", "medium": "moyenne", "low": "faible", "none": "aucune"}


def session(request: Request) -> dict:
    """Session de la requête (mise en cache : un seul identifiant par requête)."""
    cached = getattr(request.state, "session", None)
    if cached is not None:
        return cached
    s = read_session(request.cookies.get(SESSION_COOKIE))
    if "sid" not in s:
        s["sid"] = secrets.token_hex(16)
        request.state.new_session = s
    request.state.session = s
    return s


def set_session(resp, data: dict):
    from .config import settings
    resp.set_cookie(SESSION_COOKIE, sign_session(data), httponly=True, samesite="lax", max_age=14 * 24 * 3600,
                    secure=settings.base_url.startswith("https://"))
    return resp


def csrf(request: Request) -> str:
    return csrf_token_for(session(request)["sid"])


async def check_csrf(request: Request):
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        form = await request.form()
        token = form.get("csrf_token") or request.headers.get("x-csrf-token")
        if not token or not secrets.compare_digest(str(token), csrf(request)):
            raise HTTPException(status_code=400, detail="Jeton CSRF invalide : rechargez la page")


def current_user(request: Request, db=Depends(get_db)) -> User | None:
    uid = session(request).get("uid")
    return db.get(User, uid) if uid else None


def login_required(user=Depends(current_user)) -> User:
    if user is None:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return user


class ShopCtx:
    def __init__(self, shop, membership, user):
        self.shop, self.membership, self.user = shop, membership, user

    def need(self, role):
        require_role(self.membership, role)


def shop_ctx(shop_id: int, request: Request, user=Depends(login_required), db=Depends(get_db)) -> ShopCtx:
    m = membership_for(db, user.id, shop_id)
    require_role(m, "viewer")  # 404 si pas membre : aucune fuite d'existence
    if request.method == "POST":
        require_role(m, "staff")
    return ShopCtx(db.get(Shop, shop_id), m, user)


def render(request: Request, name: str, ctx: ShopCtx | None = None, status_code=200, **kw):
    data = {"request": request, "csrf_token": csrf(request), "ctx": ctx, "shop": ctx.shop if ctx else kw.pop("shop", None)}
    data.update(kw)
    resp = templates.TemplateResponse(request, name, data, status_code=status_code)
    if getattr(request.state, "new_session", None):
        set_session(resp, request.state.new_session)
    return resp


def redirect(url: str, flash: str = ""):
    if flash:
        sep = "&" if "?" in url else "?"
        from urllib.parse import quote
        url = f"{url}{sep}flash={quote(flash)}"
    return RedirectResponse(url, status_code=303)
