"""Démarrage par une photo : boutique construite autour du produit, palette issue de l'image, honnêteté sur les droits."""
import io
import json
import re

import pytest
from PIL import Image
from sqlalchemy import select

from app.models import Brief, Product
from app.services import media, versions
from conftest import Shopper, make_ready, shop_of


def photo_bytes(color=(196, 30, 58), bg=(238, 232, 220), fmt="JPEG", gps=True):
    img = Image.new("RGB", (640, 640), bg)
    from PIL import ImageDraw
    d = ImageDraw.Draw(img)
    d.ellipse((170, 170, 470, 470), fill=color)
    d.rectangle((290, 40, 350, 600), fill=(60, 60, 60))
    buf = io.BytesIO()
    if fmt == "JPEG" and gps:
        exif = Image.Exif()
        exif[0x010F] = "AppareilSecret"  # Make
        exif[0x8825] = {1: "N", 2: (50.0, 50.0, 0.0)}  # GPS
        img.save(buf, fmt, exif=exif)
    else:
        img.save(buf, fmt)
    return buf.getvalue()


def create_from_photo(merchant, rights="own", prompt="", photo=None, demo=""):
    r = merchant.post("/app/new", {"prompt": prompt, "photo_rights": rights, "demo": demo},
                      files={"photo_file": ("produit.jpg", photo or photo_bytes(), "image/jpeg")})
    assert r.status_code == 200, r.text[:400]
    return int(re.search(r"/app/shops/(\d+)/brief", str(r.url)).group(1)), r


def test_photo_only_without_ai(app, merchant, db):
    sid, r = create_from_photo(merchant)
    assert "Quels produits allez-vous vendre" in r.text, "sans IA : une seule question, le type de produit"
    b = db.scalar(select(Brief).where(Brief.shop_id == sid))
    ph = b.extracted["photo"]
    assert ph["rights"] and ph["vision"] is None and ph["url"].startswith(f"/media/{sid}/")
    # palette tirée de la photo, contrastes vérifiés
    assert ph["checks"]["button"] >= 4.5 and ph["checks"]["text_on_bg"] >= 7 and ph["checks"]["muted_on_bg"] >= 4.5
    acc = ph["palette"]["accent"]
    r_, g_, b_ = int(acc[1:3], 16), int(acc[3:5], 16), int(acc[5:7], 16)
    assert r_ > g_ + 40 and r_ > b_ + 40, f"l'accent doit reprendre le rouge de la photo ({acc})"
    # produit en brouillon avec la photo
    p = db.scalar(select(Product).where(Product.shop_id == sid))
    assert p.status == "draft" and p.images[0]["url"] == ph["url"] and p.images[0]["license"]
    assert p.variants[0].price_cents is None and p.brand == "", "prix et marque jamais inventés"
    merchant.post(f"/app/shops/{sid}/brief/questions", {"q_category": "Montres"})
    merchant.post(f"/app/shops/{sid}/generate", {"positioning": "0"})
    shop = shop_of(db, sid)
    snap = versions.snapshot(db, shop)
    assert snap["theme"]["palette"] == ph["palette"] and snap["theme"]["animations"] is True
    assert snap["home"]["hero_image"] == ph["url"] and snap["home"]["hero_product"] == p.handle
    s = Shopper(app, shop.slug, shop.preview_token)
    home = s.get("/").text
    assert ph["url"] in home and "hero--image" in home and "data-anim" in home and "marquee" in home
    assert s.c.get(ph["url"]).status_code == 200
    assert s.get(f"/product/{p.handle}").status_code == 200, "brouillon visible en prévisualisation"


def test_metadata_stripped_and_format_checked(app, merchant, db):
    sid, _ = create_from_photo(merchant)
    b = db.scalar(select(Brief).where(Brief.shop_id == sid))
    path = media.MEDIA_ROOT / b.extracted["photo"]["url"].split("/media/")[1]
    data = path.read_bytes()
    assert b"AppareilSecret" not in data
    img = Image.open(io.BytesIO(data))
    assert img.format == "WEBP" and not img.getexif()
    r = merchant.post("/app/new", {"prompt": ""}, files={"photo_file": ("x.jpg", b"<?php echo 1; ?>", "image/jpeg")})
    assert r.status_code == 400 and "pas une image valide" in r.text
    gif = io.BytesIO()
    Image.new("RGB", (10, 10)).save(gif, "GIF")
    r = merchant.post("/app/new", {"prompt": ""}, files={"photo_file": ("x.gif", gif.getvalue(), "image/gif")})
    assert r.status_code == 400 and "JPEG, PNG ou WebP" in r.text
    r = merchant.post("/app/new", {"prompt": ""}, files={"photo_file": ("x.jpg", b"0" * 8_100_000, "image/jpeg")})
    assert r.status_code == 400 and "trop lourde" in r.text


def test_photo_without_rights_is_not_shown(app, merchant, db):
    sid, r = create_from_photo(merchant, rights="", prompt="Boutique de montres pour la France")
    assert "droit d" in r.text
    merchant.post(f"/app/shops/{sid}/generate", {"positioning": "0"})
    shop = shop_of(db, sid)
    p = db.scalar(select(Product).where(Product.shop_id == sid))
    assert p.images[0]["license"] == ""
    s = Shopper(app, shop.slug, shop.preview_token)
    url = p.images[0]["url"]
    assert url not in s.get("/").text and url not in s.get(f"/product/{p.handle}").text
    merchant.post(f"/app/shops/{sid}/brief/questions", {"q_photo_rights": "Oui, c'est ma photo"})
    assert url in s.get("/").text, "droit déclaré : la photo devient la couverture"
    db.expire_all()
    assert db.get(Product, p.id).images[0]["license"] == "photo personnelle du commerçant"


def test_text_prompt_wins_over_photo_defaults(app, merchant, db):
    sid, _ = create_from_photo(merchant, prompt="Boutique de bijoux en argent pour la Belgique")
    b = db.scalar(select(Brief).where(Brief.shop_id == sid))
    assert next(a for a in b.assumptions if a["key"] == "category")["value"] == "jewelry"
    assert not any(q["key"] == "category" for q in b.questions)


def test_palette_contrast_on_varied_photos():
    for color, bg in [((250, 240, 120), (255, 255, 250)), ((20, 20, 25), (30, 30, 35)), ((90, 160, 230), (240, 245, 250)),
                      ((128, 128, 128), (128, 128, 128)), ((0, 150, 70), (10, 10, 10))]:
        img = media.load_clean(photo_bytes(color, bg, fmt="PNG", gps=False))
        ch = media.palette_from_image(img)["checks"]
        assert ch["button"] >= 4.5 and ch["text_on_bg"] >= 7 and ch["muted_on_bg"] >= 4.5, (color, bg, ch)


def test_photo_with_claude_vision(app, merchant, db, monkeypatch):
    from test_integrations import _fake_claude
    vision = {"is_product": True, "vertical": "watches", "product_type": "montre de plongée", "title_suggestion": "Montre de plongée cadran rouge",
              "description_draft": "Boîtier en acier, cadran rouge, lunette tournante.", "colors": ["rouge", "acier"], "materials": ["acier"],
              "style_keywords": ["sport"], "gender": "homme", "usage": "plongee", "visible_brand": "Seiko",
              "attributes": {"movement": "automatique", "strap_material": "acier", "metal": "or-18k"},
              "confidence": "medium", "uncertainties": ["mouvement déduit du logo du cadran"], "security_flags": []}
    cap = _fake_claude(monkeypatch, response_text=json.dumps(vision))
    sid, r = create_from_photo(merchant)
    content = cap["body"]["messages"][0]["content"]
    assert content[0]["type"] == "image" and content[0]["source"]["media_type"] == "image/jpeg"
    b = db.scalar(select(Brief).where(Brief.shop_id == sid))
    a = {x["key"]: x for x in b.assumptions}
    assert a["category"]["value"] == "watches" and "à vérifier" in a["category"]["origin"]
    assert a["brands"]["value"] == ["Seiko"] and "à confirmer" in a["brands"]["origin"]
    sup = next(q for q in b.questions if q["key"] == "supply")
    assert "Ce n'est pas cette marque" in sup["options"] and "pas une preuve" in sup["why"]
    p = db.scalar(select(Product).where(Product.shop_id == sid))
    assert p.title == "Montre de plongée cadran rouge" and p.category == "watches" and p.brand == ""
    assert p.attributes == {"movement": "automatique", "strap_material": "acier"}, "attribut hors verticale écarté"
    assert "[À COMPLÉTER" in p.description
    merchant.post(f"/app/shops/{sid}/brief/questions", {"q_supply": "Ce n'est pas cette marque"})
    db.expire_all()
    b = db.scalar(select(Brief).where(Brief.shop_id == sid))
    assert not any(x["key"] == "brands" for x in b.assumptions)


def test_animations_toggle(app, merchant, db):
    sid, _ = create_from_photo(merchant, prompt="Boutique de montres pour la France")
    merchant.post(f"/app/shops/{sid}/generate", {"positioning": "0"})
    shop = shop_of(db, sid)
    s = Shopper(app, shop.slug, shop.preview_token)
    assert "<body data-anim" in s.get("/").text
    merchant.post(f"/app/shops/{sid}/content/chat", {"message": "Désactive les animations"})
    from app.models import ShopVersion
    v = db.scalars(select(ShopVersion).where(ShopVersion.shop_id == sid, ShopVersion.status == "proposed")).first()
    merchant.post(f"/app/shops/{sid}/content/versions/{v.id}/apply")
    page = s.get("/").text
    assert "<body data-anim" not in page and "prefers-reduced-motion" in page
