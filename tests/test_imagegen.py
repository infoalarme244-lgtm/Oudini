"""Visuels générés par IA : référence = photo du commerçant, validation obligatoire, mention IA, budget."""
import base64
import io
import json
import re

import httpx
import pytest
from PIL import Image
from sqlalchemy import select

from app.models import Product
from app.services import imagegen
from conftest import Shopper, make_ready, shop_of
from test_photo import photo_bytes
from test_simple_launch import simple_create


def png_b64(color=(30, 120, 200)):
    buf = io.BytesIO()
    Image.new("RGB", (256, 256), color).save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


@pytest.fixture()
def openai_fake(monkeypatch):
    calls = []

    def handler(req):
        calls.append(req)
        assert req.url.path == "/v1/images/edits" and req.headers["authorization"] == "Bearer sk-openai-test"
        return httpx.Response(200, json={"data": [{"b64_json": png_b64()}]})
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")
    monkeypatch.setattr(imagegen, "PROVIDER", "openai")
    monkeypatch.setattr(imagegen, "TRANSPORT", httpx.MockTransport(handler))
    return calls


def test_generation_on_simple_creation_and_review(app, merchant, db, openai_fake):
    sid, r = simple_create(merchant)
    p = db.scalar(select(Product).where(Product.shop_id == sid))
    ai = [i for i in p.images if i.get("ai")]
    assert len(ai) == 4 and all(i["status"] == "pending" for i in ai), "4 visuels créés, en attente de validation"
    assert {i["scene"] for i in ai} == {"studio", "wrist", "detail", "lifestyle"}, "scènes adaptées aux montres"
    body = openai_fake[0].content
    assert b"reference.png" in body and b"Keep the product strictly identical" in body and b"No nudity" in body
    assert b"real or famous person" in body
    assert "Visuels générés par IA" in r.text and "à vérifier" in r.text
    # tant que rien n'est validé : invisible sur la boutique
    p.status = "active"
    p.variants[0].price_cents, p.variants[0].stock = 9900, 2
    db.commit()
    shop = shop_of(db, sid)
    s = Shopper(app, shop.slug, shop.preview_token)
    page = s.get(f"/product/{p.handle}").text
    assert all(i["url"] not in page for i in ai) and "Visuel généré par IA" not in page
    idx = next(n for n, i in enumerate(p.images) if i.get("scene") == "wrist")
    r = merchant.post(f"/app/shops/{sid}/lancement/visuels/{p.id}/{idx}/approve")
    assert "Visuel ajouté à la boutique" in r.text
    rej = next(n for n, i in enumerate(p.images) if i.get("scene") == "detail")
    merchant.post(f"/app/shops/{sid}/lancement/visuels/{p.id}/{rej}/reject")
    db.expire_all()
    p = db.get(Product, p.id)
    page = s.get(f"/product/{p.handle}").text
    assert p.images[idx]["url"] in page and "Visuel généré par IA" in page
    assert p.images[rej]["url"] not in page
    assert shop_of(db, sid).ai_spent_usd == pytest.approx(4 * imagegen.COST_PER_IMAGE_USD)
    from app.models import AiTrace
    t = db.scalar(select(AiTrace).where(AiTrace.shop_id == sid, AiTrace.feature == "image_generation"))
    assert t.output["created"] == 4 and t.sources == [next(i for i in p.images if not i.get("ai"))["url"]]


def test_generated_files_are_cleaned(app, merchant, db, openai_fake):
    sid, _ = simple_create(merchant)
    p = db.scalar(select(Product).where(Product.shop_id == sid))
    from app.services import media
    for i in [i for i in p.images if i.get("ai")]:
        img = Image.open(media.MEDIA_ROOT / i["url"].split("/media/")[1])
        assert img.format == "WEBP" and not img.getexif()


def test_no_reference_photo_no_generation(app, merchant, db, openai_fake, monkeypatch):
    from test_product_url import create
    r = create(merchant, "https://shop.test-public/products/trimmer", monkeypatch)
    sid = int(re.search(r"/app/shops/(\d+)/", str(r.url)).group(1))
    p = db.scalar(select(Product).where(Product.shop_id == sid))
    assert not openai_fake, "jamais de génération à partir des images d'un autre site"
    r = merchant.post(f"/app/shops/{sid}/lancement/visuels/{p.id}")
    assert "abord une photo" in r.text
    # le commerçant ajoute SA photo → génération
    merchant.post(f"/app/shops/{sid}/lancement/produit/{p.id}", {"title": p.title, "description": "", "price": "59", "stock_" + str(p.variants[0].id): "2",
                                                                "photo_rights": "1", "supply": "Grossiste / fournisseur avec factures", "brand": "BrandX"},
                  files=[("photos", ("mine.jpg", photo_bytes(), "image/jpeg"))])
    db.expire_all()
    assert len([i for i in db.get(Product, p.id).images if i.get("ai")]) == 4 and len(openai_fake) == 4


def test_budget_limits_generation(app, merchant, db, openai_fake):
    sid, _ = simple_create(merchant)
    shop = shop_of(db, sid)
    shop.ai_spent_usd = shop.ai_budget_usd - imagegen.COST_PER_IMAGE_USD * 1.5
    db.commit()
    p = db.scalar(select(Product).where(Product.shop_id == sid))
    before = len(openai_fake)
    res = imagegen.generate(db, shop, p, ["studio", "wrist", "detail"])
    assert res["created"] == 1 and "Budget IA" in res["errors"][0] and len(openai_fake) == before + 1


def test_gemini_provider(app, merchant, db, monkeypatch):
    seen = {}

    def handler(req):
        seen["url"], seen["key"] = str(req.url), req.headers.get("x-goog-api-key")
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"inlineData": {"mimeType": "image/png", "data": png_b64()}}]}}]})
    monkeypatch.setenv("GEMINI_API_KEY", "g-test")
    monkeypatch.setattr(imagegen, "PROVIDER", "gemini")
    monkeypatch.setattr(imagegen, "TRANSPORT", httpx.MockTransport(handler))
    sid, _ = simple_create(merchant, product_type="beauty")
    p = db.scalar(select(Product).where(Product.shop_id == sid))
    assert {i["scene"] for i in p.images if i.get("ai")} == {"studio", "bathroom", "texture", "hands"}
    assert ":generateContent" in seen["url"] and seen["key"] == "g-test"
    parts = seen["body"]["contents"][0]["parts"]
    assert parts[0]["inline_data"]["mime_type"] == "image/png" and "strictly identical" in parts[1]["text"]
    assert seen["body"]["generationConfig"]["responseModalities"] == ["IMAGE"]


def test_provider_errors_are_reported(app, merchant, db, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk")
    monkeypatch.setattr(imagegen, "PROVIDER", "openai")
    monkeypatch.setattr(imagegen, "TRANSPORT", httpx.MockTransport(lambda r: httpx.Response(400, json={"error": {"message": "safety"}})))
    sid, _ = simple_create(merchant)
    p = db.scalar(select(Product).where(Product.shop_id == sid))
    assert not [i for i in p.images if i.get("ai")]
    r = merchant.post(f"/app/shops/{sid}/lancement/visuels/{p.id}")
    assert "OpenAI 400" in r.text


def test_disabled_without_provider(app, merchant, db):
    sid, r = simple_create(merchant)
    assert "pas encore activée" in r.text
