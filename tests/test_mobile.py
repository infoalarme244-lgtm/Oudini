"""Fonctionnement mobile réel : navigateur Chromium à 375 px, parcours d'achat complet, aucun défilement horizontal."""
import os
import re
import socket
import threading
import time
from pathlib import Path

import pytest

pw = pytest.importorskip("playwright.sync_api")
SHOTS = Path(__file__).resolve().parents[1] / "docs" / "screenshots"


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture()
def server(app, db):
    import uvicorn
    port = _free_port()
    cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    srv = uvicorn.Server(cfg)
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    t.join(timeout=5)


def _no_hscroll(page):
    return page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")


def _browser(p):
    exe = "/opt/pw-browsers/chromium" if os.path.exists("/opt/pw-browsers/chromium") else None
    try:
        return p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()
    except Exception as exc:
        pytest.skip(f"Chromium indisponible : {exc}")


def test_mobile_purchase_flow(server, app, merchant, db):
    from conftest import make_ready, shop_of
    sid = merchant.create_shop(demo=True)
    make_ready(db, sid)
    shop = shop_of(db, sid)
    SHOTS.mkdir(parents=True, exist_ok=True)
    with pw.sync_playwright() as p:
        b = _browser(p)
        ctx = b.new_context(viewport={"width": 375, "height": 812}, device_scale_factor=2, is_mobile=True, has_touch=True)
        page = ctx.new_page()
        base = f"{server}/s/{shop.slug}"
        page.goto(f"{base}/?preview={shop.preview_token}")
        assert _no_hscroll(page), "accueil"
        page.screenshot(path=str(SHOTS / "mobile-1-accueil.png"), full_page=True)
        page.goto(f"{base}/c/running")
        assert _no_hscroll(page), "collection"
        page.screenshot(path=str(SHOTS / "mobile-2-collection.png"), full_page=True)
        page.goto(f"{base}/product/runner-demo-aero")
        assert _no_hscroll(page), "fiche produit"
        # aide pointure
        page.fill("#fm", "258")
        page.click("#sform button")
        page.wait_for_selector("#sres p")
        assert "certitude" in page.inner_text("#sres")
        page.screenshot(path=str(SHOTS / "mobile-3-produit.png"), full_page=True)
        # taille en stock (tap)
        page.locator(".sizes label:has(input:not([disabled]))").first.tap()
        page.click("text=Ajouter au panier")
        page.wait_for_url(re.compile(r".*/cart.*"))
        assert _no_hscroll(page), "panier"
        page.click("text=Commander")
        page.wait_for_url(re.compile(r".*/checkout.*"))
        assert _no_hscroll(page), "paiement"
        page.fill("#em", "mobile@example.com")
        page.fill("#nm", "Client Mobile")
        page.fill("#l1", "Rue du Midi 10")
        page.fill("#pc", "1000")
        page.fill("#ci", "Bruxelles")
        page.screenshot(path=str(SHOTS / "mobile-4-checkout.png"), full_page=True)
        page.click("button:has-text('Payer')")
        page.wait_for_url(re.compile(r".*/pay/sim/.*"))
        assert _no_hscroll(page), "simulateur"
        page.click("text=Simuler un paiement accepté")
        page.wait_for_url(re.compile(r".*/order/.*"))
        assert "Paiement confirmé" in page.content()
        page.screenshot(path=str(SHOTS / "mobile-5-confirmation.png"), full_page=True)
        # assistant d'achat
        page.click("summary:has-text('Une question')")
        page.fill("#aq", "running homme en 42")
        page.click("#aform button")
        page.wait_for_function("document.querySelectorAll('#alog p').length >= 3")
        assert _no_hscroll(page)
        b.close()
    from sqlalchemy import select
    from app.models import Order
    db.expire_all()
    o = db.scalar(select(Order).where(Order.shop_id == sid, Order.email == "mobile@example.com"))
    assert o and o.status == "paid"


def test_mobile_merchant_pages(server, app, merchant, db):
    from conftest import shop_of
    sid = merchant.create_shop(demo=True)
    cookies = [{"name": k, "value": v, "url": server} for k, v in merchant.c.cookies.items()]
    with pw.sync_playwright() as p:
        b = _browser(p)
        ctx = b.new_context(viewport={"width": 375, "height": 812}, is_mobile=True)
        ctx.add_cookies(cookies)
        page = ctx.new_page()
        for path in ("", "/brief", "/products", "/dashboard", "/launch", "/content", "/analysis", "/copilot"):
            page.goto(f"{server}/app/shops/{sid}{path}")
            assert page.locator("h1").count() >= 1, path
            assert _no_hscroll(page), f"défilement horizontal sur {path}"
        page.goto(f"{server}/app/shops/{sid}/launch")
        page.screenshot(path=str(SHOTS / "mobile-6-lancement-commercant.png"), full_page=True)
        b.close()


def test_mobile_watch_shop(server, app, merchant, db):
    from conftest import make_ready, shop_of
    sid = merchant.create_shop("Crée une boutique de montres Seiko et Casio pour la France, style élégant.", demo=True)
    make_ready(db, sid)
    shop = shop_of(db, sid)
    with pw.sync_playwright() as p:
        b = _browser(p)
        page = b.new_context(viewport={"width": 375, "height": 812}, device_scale_factor=2, is_mobile=True).new_page()
        base = f"{server}/s/{shop.slug}"
        page.goto(f"{base}/?preview={shop.preview_token}")
        page.goto(f"{base}/search?attr_movement=automatique")
        assert _no_hscroll(page)
        page.goto(f"{base}/product/field-demo-40")
        page.fill("#fm", "160")
        page.click("#sform button")
        page.wait_for_selector("#sres p")
        assert _no_hscroll(page) and "certitude" in page.inner_text("#sres")
        page.screenshot(path=str(SHOTS / "mobile-7-montre.png"), full_page=True)
        b.close()
