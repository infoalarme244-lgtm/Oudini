import secrets

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select

from ..db import get_db
from ..models import User
from ..services.audit import audit
from ..services.security import hash_password, verify_password
from ..web import check_csrf, current_user, redirect, render, session, set_session

router = APIRouter(dependencies=[Depends(check_csrf)])


@router.get("/")
def home(request: Request, user=Depends(current_user)):
    if user:
        return redirect("/app")
    return render(request, "landing.html")


@router.get("/signup")
def signup_form(request: Request):
    return render(request, "auth/signup.html")


@router.post("/signup")
def signup(request: Request, email: str = Form(...), password: str = Form(...), name: str = Form(""), db=Depends(get_db)):
    email = email.strip().lower()
    if len(password) < 10:
        return render(request, "auth/signup.html", error="Mot de passe : 10 caractères minimum", email=email, status_code=400)
    if "@" not in email or db.scalar(select(User).where(User.email == email)):
        return render(request, "auth/signup.html", error="Email invalide ou déjà utilisé", email=email, status_code=400)
    u = User(email=email, name=name, password_hash=hash_password(password))
    db.add(u)
    db.flush()
    audit(db, None, u.id, "user.signup", "user", u.id)
    db.commit()
    s = session(request)
    s["uid"] = u.id
    return set_session(redirect("/app/new"), s)


@router.get("/login")
def login_form(request: Request):
    return render(request, "auth/login.html")


@router.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...), db=Depends(get_db)):
    u = db.scalar(select(User).where(User.email == email.strip().lower()))
    if not u or not verify_password(password, u.password_hash):
        return render(request, "auth/login.html", error="Identifiants incorrects", email=email, status_code=401)
    s = {"sid": secrets.token_hex(16), "uid": u.id}  # rotation : pas de fixation de session
    return set_session(redirect("/app"), s)


@router.post("/logout")
def logout(request: Request):
    resp = redirect("/")
    resp.delete_cookie("sf_session")
    return resp
