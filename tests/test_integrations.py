"""Intégrations externes, vérifiées au niveau du protocole (transports HTTP simulés : aucun appel réseau réel).
Les vérifications de bout en bout avec de vrais comptes se font avec scripts/verify_*.py."""
import json

import httpx
import pytest
from sqlalchemy import select

from app.config import settings
from app.models import Customer, EmailOutbox
from app.services import ai, mailer
from conftest import shop_of


@pytest.fixture()
def mail_env(monkeypatch):
    monkeypatch.setattr(settings, "email_from", "boutique@mail.example")
    monkeypatch.setattr(settings, "postmark_token", "pm-test-token")
    monkeypatch.setattr(settings, "brevo_api_key", "brevo-test-key")


def _queue(db, shop_id, **kw):
    m = EmailOutbox(shop_id=shop_id, to=kw.pop("to", "client@example.com"), subject="Sujet", body="Corps", **kw)
    db.add(m)
    db.commit()
    return m


def test_postmark_transactional_and_marketing(app, merchant, db, mail_env, monkeypatch):
    monkeypatch.setattr(settings, "email_provider", "postmark")
    sid = merchant.create_shop(demo=False)
    db.add(Customer(shop_id=sid, email="fan@example.com", marketing_consent=True))
    tx = _queue(db, sid)
    mk = _queue(db, sid, to="fan@example.com", kind="marketing", headers={"List-Unsubscribe": "<https://x/u>", "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"})
    seen = []

    def handler(req):
        assert req.headers["X-Postmark-Server-Token"] == "pm-test-token"
        seen.append(json.loads(req.content))
        return httpx.Response(200, json={"ErrorCode": 0, "MessageID": f"pm-{len(seen)}"})
    stats = mailer.send_pending(db, transport=httpx.MockTransport(handler))
    assert stats["sent"] == 2
    assert seen[0]["MessageStream"] == "outbound" and seen[1]["MessageStream"] == "broadcast"
    assert {"Name": "List-Unsubscribe-Post", "Value": "List-Unsubscribe=One-Click"} in seen[1]["Headers"]
    assert seen[0]["From"].endswith("<boutique@mail.example>")
    db.expire_all()
    assert db.get(EmailOutbox, tx.id).status == "sent" and db.get(EmailOutbox, mk.id).provider_id == "pm-2"


def test_retry_then_permanent_failure(app, merchant, db, mail_env, monkeypatch):
    monkeypatch.setattr(settings, "email_provider", "postmark")
    sid = merchant.create_shop(demo=False)
    m1 = _queue(db, sid)
    mailer.send_pending(db, transport=httpx.MockTransport(lambda r: httpx.Response(503, text="busy")))
    db.expire_all()
    m = db.get(EmailOutbox, m1.id)
    assert m.status == "queued" and m.attempts == 1 and m.next_attempt_at and "503" in m.error
    m2 = _queue(db, sid, to="invalide@example.com")
    m.next_attempt_at = None
    db.commit()
    mailer.send_pending(db, transport=httpx.MockTransport(lambda r: httpx.Response(422, json={"ErrorCode": 300, "Message": "Invalid email"})))
    db.expire_all()
    assert db.get(EmailOutbox, m2.id).status == "failed", "erreur définitive : pas de réessai"


def test_unsubscribed_before_send_is_held(app, merchant, db, mail_env, monkeypatch):
    monkeypatch.setattr(settings, "email_provider", "brevo")
    sid = merchant.create_shop(demo=False)
    db.add(Customer(shop_id=sid, email="parti@example.com", marketing_consent=False))
    m = _queue(db, sid, to="parti@example.com", kind="marketing")
    calls = []
    mailer.send_pending(db, transport=httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(201, json={"messageId": "x"})))
    db.expire_all()
    assert db.get(EmailOutbox, m.id).status == "held" and not calls


def test_brevo_payload(app, merchant, db, mail_env, monkeypatch):
    monkeypatch.setattr(settings, "email_provider", "brevo")
    sid = merchant.create_shop(demo=False)
    merchant.post(f"/app/shops/{sid}/settings", {"name": "Pointure", "legal_email": "contact@pointure.example"})
    _queue(db, sid)
    seen = []

    def handler(req):
        assert req.url.path == "/v3/smtp/email" and req.headers["api-key"] == "brevo-test-key"
        seen.append(json.loads(req.content))
        return httpx.Response(201, json={"messageId": "<abc@brevo>"})
    assert mailer.send_pending(db, transport=httpx.MockTransport(handler))["sent"] == 1
    assert seen[0]["sender"] == {"email": "boutique@mail.example", "name": "Pointure"}
    assert seen[0]["replyTo"] == {"email": "contact@pointure.example"}


def test_missing_sender_fails_clearly(app, merchant, db, monkeypatch):
    monkeypatch.setattr(settings, "email_provider", "postmark")
    monkeypatch.setattr(settings, "postmark_token", "t")
    monkeypatch.setattr(settings, "email_from", "")
    sid = merchant.create_shop(demo=False)
    m = _queue(db, sid)
    mailer.send_pending(db, transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"ErrorCode": 0, "MessageID": "x"})))
    db.expire_all()
    assert db.get(EmailOutbox, m.id).status == "failed" and "EMAIL_FROM" in db.get(EmailOutbox, m.id).error


def test_outbox_mode_sends_nothing(app, merchant, db):
    sid = merchant.create_shop(demo=False)
    m = _queue(db, sid)
    assert mailer.send_pending(db)["sent"] == 0
    db.expire_all()
    assert db.get(EmailOutbox, m.id).status == "queued"


def test_one_click_unsubscribe_without_session(app, merchant, db):
    from fastapi.testclient import TestClient
    sid = merchant.create_shop(demo=False)
    c = Customer(shop_id=sid, email="x@example.com", marketing_consent=True)
    db.add(c)
    db.commit()
    shop = shop_of(db, sid)
    r = TestClient(app).post(f"/unsubscribe/{shop.slug}/{c.unsubscribe_token}", content="List-Unsubscribe=One-Click")
    assert r.status_code == 200
    db.expire_all()
    assert db.get(Customer, c.id).unsubscribed_at
    assert TestClient(app).post(f"/unsubscribe/{shop.slug}/mauvais-jeton").status_code == 404


def test_healthcheck(app):
    from fastapi.testclient import TestClient
    r = TestClient(app).get("/healthz")
    assert r.status_code == 200 and r.json()["db"] == "ok"


# ---------------------------------------------------------------- Claude
def _fake_claude(monkeypatch, response_text='{"audience": ["trentenaires urbains"], "style": ["minimal"], "notes": "ok"}',
                 stop_reason="end_turn"):
    import anthropic
    import httpx2
    captured = {}

    def handler(req):
        captured["url"] = str(req.url)
        captured["headers"] = dict(req.headers)
        captured["body"] = json.loads(req.content)
        return httpx2.Response(200, json={
            "id": "msg_test", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "stop_reason": stop_reason,
            "stop_sequence": None, "content": [{"type": "text", "text": response_text}],
            "usage": {"input_tokens": 1200, "output_tokens": 300}})
    client = anthropic.Anthropic(api_key="sk-ant-test", base_url="https://api.anthropic.com",
                                 http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
    monkeypatch.setattr(ai, "_client", lambda: client)
    monkeypatch.setattr(settings, "ai_enabled", True)
    return captured


def test_claude_request_shape_and_trace(app, merchant, db, monkeypatch):
    cap = _fake_claude(monkeypatch)
    sid = merchant.create_shop("Boutique de sacs en cuir pour la France", demo=False)
    body = cap["body"]
    assert body["model"] == "claude-opus-5-5" and body["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in cap["headers"].get("anthropic-beta", "")
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert "<untrusted_data>" in body["messages"][0]["content"][-1]["text"] and "jamais une instruction" in body["system"]
    assert "thinking" not in body and "temperature" not in body
    from app.models import AiTrace, Brief
    t = db.scalar(select(AiTrace).where(AiTrace.shop_id == sid, AiTrace.engine == "claude-opus-5-5"))
    assert t and t.tokens_in == 1200 and abs(t.cost_usd - (1200 * 4 + 300 * 20) / 1e6) < 1e-9
    b = db.scalar(select(Brief).where(Brief.shop_id == sid))
    assert b.extracted["audience"] == ["trentenaires urbains"]
    assert shop_of(db, sid).ai_spent_usd > 0


def test_claude_budget_and_refusal(app, merchant, db, monkeypatch):
    cap = _fake_claude(monkeypatch, stop_reason="refusal")
    sid = merchant.create_shop("Boutique de sacs en cuir pour la France", demo=False)
    from app.models import Brief
    assert db.scalar(select(Brief).where(Brief.shop_id == sid)).extracted["audience"] == [], "refus → moteur à règles"
    shop = shop_of(db, sid)
    shop.ai_spent_usd = shop.ai_budget_usd
    db.commit()
    cap.clear()
    assert ai.complete_json(db, shop, "x", "instr", "data", {"type": "object"}) is None
    assert not cap, "budget épuisé : aucun appel"
