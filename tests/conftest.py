import os
import re
import sys
from pathlib import Path

os.environ["SHOPFORGE_JOBS_INLINE"] = "1"
os.environ.setdefault("SHOPFORGE_SECRET_KEY", "test-secret-key")
os.environ.pop("ANTHROPIC_API_KEY", None)  # tests déterministes : moteur à règles
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

FIX = Path(__file__).parent / "fixtures"
PROMPT = ("Crée une boutique de baskets Nike pour la Belgique et la France, avec un positionnement premium accessible. "
          "Analyse 10 concurrents pertinents et propose une boutique qui répond mieux aux attentes des clients.")


@pytest.fixture()
def app(tmp_path):
    """SQLite par défaut ; PostgreSQL si SHOPFORGE_TEST_DATABASE_URL est défini (schéma recréé à chaque test)."""
    from app import db as dbm
    url = os.getenv("SHOPFORGE_TEST_DATABASE_URL")
    if url:
        eng = dbm.configure(url)
        from app import models  # noqa: F401
        dbm.Base.metadata.drop_all(eng)
    else:
        dbm.configure(f"sqlite:///{tmp_path}/test.db")
    from app.main import create_app
    return create_app()


@pytest.fixture()
def db(app):
    from app import db as dbm
    s = dbm.SessionLocal()
    yield s
    s.close()


def csrf(client, url="/signup"):
    r = client.get(url)
    m = re.search(r'name="csrf_token" value="([^"]+)"', r.text)
    assert m, f"pas de jeton CSRF sur {url} ({r.status_code})"
    return m.group(1)


class Merchant:
    def __init__(self, app, email):
        self.c = TestClient(app)
        self.email = email
        tok = csrf(self.c)
        r = self.c.post("/signup", data={"email": email, "password": "motdepasse-solide", "csrf_token": tok})
        assert r.status_code == 200
        self.token = csrf(self.c, "/app/new")

    def post(self, url, data=None, **kw):
        data = dict(data or {})
        data["csrf_token"] = self.token
        return self.c.post(url, data=data, **kw)

    def get(self, url, **kw):
        return self.c.get(url, **kw)

    def create_shop(self, prompt=PROMPT, demo=True, analyse=False, name=""):
        r = self.post("/app/new", {"prompt": prompt, "demo": "1" if demo else "", "name": name})
        assert r.status_code == 200, r.text[:500]
        shop_id = int(re.search(r"/app/shops/(\d+)/brief", str(r.url)).group(1))
        r = self.post(f"/app/shops/{shop_id}/generate", {"positioning": "0", "analyse": "1" if analyse else ""})
        assert r.status_code == 200, r.text[:500]
        return shop_id


def make_ready(db, shop_id, rate_eur=6.9, free_over=None):
    """Configure livraison + TVA validée, comme le ferait le commerçant."""
    from sqlalchemy import select
    from app.models import ShippingZone, TaxSetting
    for z in db.scalars(select(ShippingZone).where(ShippingZone.shop_id == shop_id)):
        z.rates = [{"id": "std", "name": "Standard", "carrier": "bpost", "price_cents": int(rate_eur * 100),
                    "free_over_cents": free_over, "min_days": 2, "max_days": 4, "cost_cents": 520}]
    for t in db.scalars(select(TaxSetting).where(TaxSetting.shop_id == shop_id)):
        t.validated = True
    db.commit()


@pytest.fixture()
def merchant(app):
    return Merchant(app, "alice@example.com")


@pytest.fixture()
def merchant_b(app):
    return Merchant(app, "bob@example.com")


class Shopper:
    """Client de la vitrine (session distincte)."""

    def __init__(self, app, slug, preview_token=None):
        self.c = TestClient(app)
        self.base = f"/s/{slug}"
        q = f"?preview={preview_token}" if preview_token else ""
        r = self.c.get(self.base + "/" + q)
        assert r.status_code == 200, r.text[:300]
        self.token = re.search(r'name="csrf_token" value="([^"]+)"', r.text).group(1)

    def get(self, path, **kw):
        return self.c.get(self.base + path, **kw)

    def post(self, path, data=None, **kw):
        data = dict(data or {})
        data["csrf_token"] = self.token
        return self.c.post(self.base + path, data=data, **kw)

    def buy(self, variant_id, qty=1, country="BE", email="client@example.com", consent=False, idem=None):
        r = self.post("/cart/add", {"variant_id": variant_id, "qty": qty})
        assert r.status_code == 200, r.text[:300]
        r = self.get(f"/checkout?country={country}")
        idem = idem or re.search(r'name="idempotency_key" value="([^"]+)"', r.text).group(1)
        return self.post("/checkout", {"email": email, "name": "Client Test", "line1": "Rue de la Loi 1", "postal_code": "1000",
                                       "city": "Bruxelles", "country": country, "rate": "std", "idempotency_key": idem,
                                       "marketing_consent": "1" if consent else ""})


def shop_of(db, shop_id):
    from app.models import Shop
    db.expire_all()
    return db.get(Shop, shop_id)


# ---------------------------------------------------------------- faux web pour l'analyse concurrentielle
PUBLIC_IP = "93.184.216.34"


def fake_resolver(host):
    if host == "evil-rebind.test-public":
        return ["127.0.0.1"]
    if host.endswith(".test-public"):
        return [PUBLIC_IP]
    if host.startswith("internal"):
        return ["10.0.0.5"]
    raise ValueError(host)


def site_transport(pages: dict):
    def handler(request: httpx.Request):
        url = str(request.url)
        if url in pages:
            status, body, headers = pages[url]
            return httpx.Response(status, text=body, headers={"content-type": "text/html; charset=utf-8", **headers})
        if url.endswith("/robots.txt"):
            return httpx.Response(404, text="")
        return httpx.Response(404, text="not found", headers={"content-type": "text/html"})
    return httpx.MockTransport(handler)


def fixture(name):
    return (FIX / name).read_text(encoding="utf-8")
