"""Créer une boutique depuis le lien d'un produit : faits repris, textes réécrits, photos jamais reprises."""
import json
import re

import httpx
from sqlalchemy import select

from app.models import Brief, Product
from app.services import autopilot, product_import
from app.services.url_guard import SafeFetcher
from conftest import fake_resolver, shop_of

SOURCE = ("Notre tondeuse est conçue pour un rasage sans irritation grâce à une lame céramique ultra fine et une batterie "
          "qui tient quatre-vingt-dix minutes sans faiblir même sous la douche.")
SHOPIFY = {"product": {"title": "Trimmer Pro 2", "vendor": "BrandX", "product_type": "Rasoir", "body_html": f"<p>{SOURCE}</p>",
                       "options": [{"name": "Couleur", "values": ["Noir", "Sable"]}],
                       "variants": [{"price": "59.00"}, {"price": "59.00"}],
                       "images": [{"src": "https://cdn.test-public/1.jpg"}, {"src": "https://cdn.test-public/2.jpg"}], "tags": []}}
HTML = ('<html><head><script type="application/ld+json">{"@type":"Product","name":"Sac Cabas Ville","brand":{"name":"MaisonY"},'
        '"description":"Un grand sac en cuir pour tous les jours.","image":["https://cdn.test-public/a.jpg"],'
        '"offers":{"price":"129.00","priceCurrency":"EUR"}}</script></head><body><h1>Sac</h1></body></html>')


def web(robots_block=False):
    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /") if robots_block else httpx.Response(404)
        if req.url.path == "/products/trimmer.json":
            return httpx.Response(200, text=json.dumps(SHOPIFY), headers={"content-type": "application/json"})
        if req.url.path == "/p/sac":
            return httpx.Response(200, text=HTML, headers={"content-type": "text/html"})
        return httpx.Response(404, text="", headers={"content-type": "text/html"})
    return lambda: SafeFetcher(transport=httpx.MockTransport(handler), resolver=fake_resolver)


def create(merchant, url, monkeypatch, robots_block=False, **extra):
    monkeypatch.setattr(product_import, "FETCHER_FACTORY", web(robots_block))
    monkeypatch.setattr("app.services.url_guard.resolve", fake_resolver)
    return merchant.post("/app/new", {"mode": "simple", "product_url": url, "countries": ["BE", "FR"], "language": "fr", **extra})


def test_shopify_product_link(app, merchant, db, monkeypatch):
    r = create(merchant, "https://shop.test-public/products/trimmer?variant=1", monkeypatch)
    assert "/lancement" in str(r.url), r.text[:300]
    sid = int(re.search(r"/app/shops/(\d+)/", str(r.url)).group(1))
    p = db.scalar(select(Product).where(Product.shop_id == sid))
    assert p.title == "Trimmer Pro 2" and p.brand == "BrandX" and p.status == "draft"
    assert p.images == [], "les photos du site source ne sont jamais reprises"
    assert p.description == "", "sans IA : aucune reprise du texte source"
    assert sorted(v.color for v in p.variants) == ["Noir", "Sable"] and all(v.price_cents is None for v in p.variants)
    b = db.scalar(select(Brief).where(Brief.shop_id == sid))
    brand = next(a for a in b.assumptions if a["key"] == "brands")
    assert brand["value"] == ["BrandX"] and "à confirmer" in brand["origin"]
    assert b.competitor_url.startswith("https://shop.test-public/") and b.extracted["source_product"]["image_count"] == 2
    sug = autopilot.price_suggestion(db, shop_of(db, sid), p)
    assert sug["cents"] == 5900 and "shop.test-public" in sug["basis"]
    page = merchant.get(f"/app/shops/{sid}/lancement?etape=1").text
    assert "photos du site source non reprises" in page and "Stock par variante" in page
    assert "Comment vous procurez-vous ce produit BrandX" in page


def test_html_product_link(app, merchant, db, monkeypatch):
    r = create(merchant, "https://maisony.test-public/p/sac", monkeypatch)
    sid = int(re.search(r"/app/shops/(\d+)/", str(r.url)).group(1))
    p = db.scalar(select(Product).where(Product.shop_id == sid))
    assert p.title == "Sac Cabas Ville" and p.category == "accessories" and p.images == []
    assert autopilot.price_suggestion(db, shop_of(db, sid), p)["cents"] == 12900


def test_ai_copy_is_original(app, merchant, db, monkeypatch):
    from test_integrations import _fake_claude
    copied = SOURCE.split(" même")[0] + "."
    ai_out = {"title": "Tondeuse Trimmer Pro 2", "description": f"Une tondeuse pensée pour le quotidien. {copied}\nLivrée avec son socle.",
              "highlights": ["Lame céramique", "Étanche"], "security_flags": []}
    cap = _fake_claude(monkeypatch, response_text=json.dumps(ai_out))
    r = create(merchant, "https://shop.test-public/products/trimmer", monkeypatch)
    sid = int(re.search(r"/app/shops/(\d+)/", str(r.url)).group(1))
    p = db.scalar(select(Product).where(Product.shop_id == sid))
    assert "Une tondeuse pensée pour le quotidien." in p.description and "Livrée avec son socle." in p.description
    assert "lame céramique ultra fine" not in p.description.lower(), "phrase trop proche de la source retirée"
    assert "- Lame céramique" in p.description
    sent = json.loads(cap["body"]["messages"][0]["content"][-1]["text"].split("<untrusted_data>\n")[1].split("\n</untrusted_data>")[0])
    assert "images" not in sent and sent["name"] == "Trimmer Pro 2", "seuls des faits sont transmis, jamais les images"
    from app.models import AiTrace
    t = db.scalar(select(AiTrace).where(AiTrace.shop_id == sid, AiTrace.feature == "product_copy_check"))
    assert t.output["phrases_retirees_car_trop_proches_de_la_source"] == 1


def test_unsafe_or_blocked_links(app, merchant, db, monkeypatch):
    r = create(merchant, "http://169.254.169.254/products/x", monkeypatch)
    assert r.status_code == 400 and "Lien produit inutilisable" in r.text
    r = create(merchant, "https://shop.test-public/p/sac", monkeypatch, robots_block=True)
    assert r.status_code == 400 and "robots.txt" in r.text


def test_strip_copied_unit():
    src = "la batterie tient quatre vingt dix minutes sans faiblir même sous la douche"
    text, n = product_import.strip_copied("Autonomie solide. La batterie tient quatre vingt dix minutes sans faiblir, promis.", src)
    assert n == 1 and text == "Autonomie solide."
