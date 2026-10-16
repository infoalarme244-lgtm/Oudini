"""Actions engageantes : validation explicite, autorisations limitées et révocables."""
from sqlalchemy import select

from app.models import ApprovalRequest, Customer, EmailOutbox, MarketingDraft, PaymentAccount, Product, Shop, Variant
from app.services.security import encrypt_secret
from conftest import Shopper, make_ready, shop_of


def fill_for_launch(merchant, db, sid):
    make_ready(db, sid)
    merchant.post(f"/app/shops/{sid}/catalog/purge-demo")
    legal = {"legal_company_name": "Pointure SRL", "legal_legal_form": "SRL", "legal_address": "Rue Neuve 1, 1000 Bruxelles",
             "legal_registration": "BCE 0123.456.789", "legal_vat_number": "BE0123456789", "legal_email": "contact@pointure.example",
             "legal_phone": "+32 2 000 00 00", "legal_publisher": "A. Martin", "legal_host": "Hébergeur SA, Paris",
             "legal_mediator": "Médiateur XYZ", "name": "Pointure"}
    merchant.post(f"/app/shops/{sid}/settings", legal)
    merchant.post(f"/app/shops/{sid}/content/regenerate-legal")
    shop = shop_of(db, sid)
    p = Product(shop_id=sid, handle="air-test", title="Sneaker Test", brand="Nike", model="Test", usage="lifestyle", gender="homme",
                origin="Grossiste UE, facture n°42", authenticity="Facture fournisseur conservée", images=[{"url": "https://img.example/a.jpg", "alt": "a", "license": "photo personnelle"}])
    db.add(p)
    db.add(Variant(shop_id=sid, product=p, sku="NK-42", size="42", color="Blanc", price_cents=12900, cost_cents=6000, stock=5))
    db.commit()
    from app.services import versions
    snap = versions.snapshot(db, shop)
    for k, page in snap["pages"].items():
        if page.get("legal"):
            page["validated"] = True
        page["body"] = page["body"].replace("[À COMPLÉTER", "[completé")
    versions.create(db, shop, snap, "validation", None)
    db.commit()
    return shop


def test_publish_requires_readiness_and_explicit_approval(app, merchant, db):
    sid = merchant.create_shop(demo=True)
    r = merchant.post(f"/app/shops/{sid}/launch/request")
    assert "blocage" in r.text
    assert shop_of(db, sid).status == "draft"
    launch = merchant.get(f"/app/shops/{sid}/launch").text
    for blocker in ("Informations légales", "Aucun produit de démonstration", "TVA validée", "Livraison configurée", "Paiement réel"):
        assert blocker in launch
    fill_for_launch(merchant, db, sid)
    # paiement live : connexion externe + validation
    db.add(PaymentAccount(shop_id=sid, provider="stripe", mode="live", status="connected", secret_enc=encrypt_secret("sk_live_x"),
                          webhook_secret_enc=encrypt_secret("whsec_x")))
    db.commit()
    merchant.post(f"/app/shops/{sid}/payments/live/request")
    req = db.scalar(select(ApprovalRequest).where(ApprovalRequest.shop_id == sid, ApprovalRequest.action == "enable_live_payments"))
    assert req.status == "pending" and shop_of(db, sid).payment_mode == "test"
    merchant.post(f"/app/shops/{sid}/approvals/{req.id}", {"decision": "approve"})
    assert shop_of(db, sid).payment_mode == "live"
    r = merchant.post(f"/app/shops/{sid}/launch/request")
    assert "Demande de publication créée" in r.text, merchant.get(f"/app/shops/{sid}/launch").text[-3000:]
    assert shop_of(db, sid).status == "draft", "pas de publication sans validation"
    req = db.scalar(select(ApprovalRequest).where(ApprovalRequest.shop_id == sid, ApprovalRequest.action == "publish_store"))
    merchant.post(f"/app/shops/{sid}/approvals/{req.id}", {"decision": "approve"})
    shop = shop_of(db, sid)
    assert shop.status == "published" and shop.published_version_id
    from fastapi.testclient import TestClient
    r = TestClient(app).get(f"/s/{shop.slug}/")
    assert r.status_code == 200 and "Prévisualisation" not in r.text and "noindex" not in r.text


def test_publish_cannot_be_auto_permitted(app, merchant, db):
    sid = merchant.create_shop(demo=False)
    r = merchant.post(f"/app/shops/{sid}/permissions", {"action": "publish_store", "max_products": "1"})
    assert "ne peut pas être automatisée" in r.text


def test_bulk_price_change_respects_margin_floor(app, merchant, db):
    sid = merchant.create_shop(demo=True)
    make_ready(db, sid)
    merchant.post(f"/app/shops/{sid}/settings", {"cost_min_contribution_pct": "10", "cost_shipping_cost_cents": "5", "cost_packaging_cents": "1",
                                                 "cost_payment_fee_bp": "1.5", "name": "X"})
    vs = db.scalars(select(Variant).where(Variant.shop_id == sid)).all()
    ids = [v.id for v in vs[:3]]
    before = {v.id: v.price_cents for v in vs[:3]}
    r = merchant.post(f"/app/shops/{sid}/prices/bulk", {"variant_ids": ids, "change_pct": "-50"})
    assert "attente de validation" in r.text
    db.expire_all()
    assert all(db.get(Variant, i).price_cents == before[i] for i in ids), "rien avant validation"
    req = db.scalar(select(ApprovalRequest).where(ApprovalRequest.shop_id == sid, ApprovalRequest.action == "bulk_price_change"))
    r = merchant.post(f"/app/shops/{sid}/approvals/{req.id}", {"decision": "approve"})
    db.expire_all()
    assert "refusé" in db.get(ApprovalRequest, req.id).result
    assert all(db.get(Variant, i).price_cents == before[i] for i in ids), "-50 % détruirait la marge : refusé"
    # hausse de 5 % : appliquée
    merchant.post(f"/app/shops/{sid}/prices/bulk", {"variant_ids": ids, "change_pct": "5"})
    req2 = db.scalars(select(ApprovalRequest).where(ApprovalRequest.shop_id == sid).order_by(ApprovalRequest.id.desc())).first()
    merchant.post(f"/app/shops/{sid}/approvals/{req2.id}", {"decision": "approve"})
    db.expire_all()
    assert all(db.get(Variant, i).price_cents == round(before[i] * 1.05) for i in ids)


def test_auto_permission_limited_and_revocable(app, merchant, db):
    sid = merchant.create_shop(demo=True)
    make_ready(db, sid)
    vs = db.scalars(select(Variant).where(Variant.shop_id == sid)).all()
    merchant.post(f"/app/shops/{sid}/permissions", {"action": "bulk_price_change", "max_change_pct": "3", "max_products": "2"})
    r = merchant.post(f"/app/shops/{sid}/prices/bulk", {"variant_ids": [vs[0].id], "change_pct": "2"})
    assert "autorisation automatique" in r.text
    r = merchant.post(f"/app/shops/{sid}/prices/bulk", {"variant_ids": [vs[0].id], "change_pct": "8"})
    assert "attente de validation" in r.text, "hors limite → validation manuelle"
    from app.models import AutoPermission
    p = db.scalar(select(AutoPermission).where(AutoPermission.shop_id == sid))
    merchant.post(f"/app/shops/{sid}/permissions/{p.id}/revoke")
    r = merchant.post(f"/app/shops/{sid}/prices/bulk", {"variant_ids": [vs[0].id], "change_pct": "1"})
    assert "attente de validation" in r.text


def test_marketing_needs_approval_and_consent(app, merchant, db):
    sid = merchant.create_shop(demo=True)
    make_ready(db, sid)
    shop = shop_of(db, sid)
    v = db.scalars(select(Variant).where(Variant.shop_id == sid, Variant.stock > 1)).first()
    Shopper(app, shop.slug, shop.preview_token).buy(v.id, email="oui@example.com", consent=True)
    Shopper(app, shop.slug, shop.preview_token).buy(v.id, email="non@example.com", consent=False)
    merchant.post(f"/app/shops/{sid}/marketing/generate")
    d = db.scalar(select(MarketingDraft).where(MarketingDraft.shop_id == sid, MarketingDraft.kind == "email"))
    merchant.post(f"/app/shops/{sid}/marketing/{d.id}/send")
    assert not db.scalars(select(EmailOutbox).where(EmailOutbox.kind == "marketing")).all(), "rien sans validation"
    req = db.scalar(select(ApprovalRequest).where(ApprovalRequest.action == "send_marketing"))
    merchant.post(f"/app/shops/{sid}/approvals/{req.id}", {"decision": "approve"})
    sent = db.scalars(select(EmailOutbox).where(EmailOutbox.kind == "marketing")).all()
    assert [m.to for m in sent] == ["oui@example.com"]
    assert "/unsubscribe/" in sent[0].body
    # désabonnement
    c = db.scalar(select(Customer).where(Customer.email == "oui@example.com"))
    s = Shopper(app, shop.slug, shop.preview_token)
    s.post(f"/unsubscribe/{c.unsubscribe_token}")
    db.expire_all()
    c = db.get(Customer, c.id)
    assert c.unsubscribed_at and not c.marketing_consent


def test_paid_campaign_requires_budget(app, merchant, db):
    sid = merchant.create_shop(demo=False)
    merchant.post(f"/app/shops/{sid}/marketing/generate")
    d = db.scalar(select(MarketingDraft).where(MarketingDraft.shop_id == sid, MarketingDraft.kind == "paid_campaign"))
    r = merchant.post(f"/app/shops/{sid}/marketing/{d.id}/campaign", {"budget": "0"})
    assert "Budget explicite obligatoire" in r.text
    merchant.post(f"/app/shops/{sid}/marketing/{d.id}/campaign", {"budget": "150"})
    req = db.scalar(select(ApprovalRequest).where(ApprovalRequest.action == "launch_paid_campaign"))
    assert req.status == "pending" and req.payload["budget_cents"] == 15000
