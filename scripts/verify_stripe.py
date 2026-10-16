"""Vérification Stripe en MODE TEST, avec un vrai compte (aucun argent réel).

Usage :
    export STRIPE_TEST_SECRET_KEY=sk_test_...      # ou rk_test_...
    python scripts/verify_stripe.py

Vérifie, contre l'API Stripe réelle :
  1. la clé est bien une clé de test et le compte répond ;
  2. un paiement carte accepté (pm_card_visa) ;
  3. un paiement refusé (pm_card_chargeDeclined) → erreur card_declined ;
  4. un remboursement partiel puis total du paiement accepté ;
  5. Bancontact activé sur le compte (PaymentIntent EUR accepté) ;
  6. une session Checkout créée par le VRAI code de la boutique (lignes, réduction, livraison) — URL à ouvrir
     pour payer avec 4242 4242 4242 4242 ou 4000 0000 0000 0002 (refus), et Bancontact (page de test).
Le webhook se teste ensuite avec : stripe listen --forward-to localhost:8000/webhooks/stripe/<shop_id> --events checkout.session.completed,checkout.session.async_payment_succeeded,checkout.session.async_payment_failed,checkout.session.expired,payment_intent.payment_failed,refund.created,refund.updated
"""
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import stripe  # noqa: E402

OK, KO, WAIT = "✅", "❌", "⏳"
# Paiement confirmé côté serveur avec une carte de test : pas de redirection (3DS, Bancontact…) possible ici.
AUTO_NO_REDIRECT = {"enabled": True, "allow_redirects": "never"}
results = []


def check(label, fn):
    try:
        detail = fn()
        results.append((OK, label, detail or ""))
    except stripe.PermissionError as exc:
        from app.services.payments import approval_request_id
        apreq = approval_request_id(str(exc))
        if apreq:  # clé restreinte avec approbation humaine : ce n'est pas une erreur
            results.append((WAIT, label, f"en attente d'approbation humaine ({apreq}) : approuvez-la dans le dashboard Stripe, "
                                         "ou autorisez les remboursements sur la clé, puis relancez"))
        else:
            results.append((KO, label, f"PermissionError: {exc}"))
    except Exception as exc:  # on rapporte l'erreur Stripe telle quelle
        results.append((KO, label, f"{type(exc).__name__}: {getattr(exc, 'user_message', None) or exc}"))


def main():
    key = os.getenv("STRIPE_TEST_SECRET_KEY", "")
    if not key.startswith(("sk_test_", "rk_test_")):
        sys.exit("STRIPE_TEST_SECRET_KEY doit être une clé de TEST (sk_test_… ou rk_test_…). Aucune clé live n'est acceptée ici.")
    c = stripe.StripeClient(key)
    run = uuid.uuid4().hex[:8]
    state = {}

    def account():
        a = c.v1.accounts.retrieve_current()
        return f"compte {a.id}, pays {a.country}, paiements {'activés' if a.charges_enabled else 'non activés'}"
    check("1. Clé de test et compte", account)

    def card_ok():
        pi = c.v1.payment_intents.create(params={"amount": 12900, "currency": "eur", "payment_method": "pm_card_visa",
                                                 "automatic_payment_methods": AUTO_NO_REDIRECT, "confirm": True,
                                                 "metadata": {"shopforge_check": run}},
                                         options={"idempotency_key": f"chk-ok-{run}"})
        assert pi.status == "succeeded", pi.status
        state["pi"] = pi.id
        return f"{pi.id} succeeded (129,00 €)"
    check("2. Paiement carte accepté", card_ok)

    def card_declined():
        try:
            c.v1.payment_intents.create(params={"amount": 5000, "currency": "eur", "payment_method": "pm_card_chargeDeclined",
                                                "automatic_payment_methods": AUTO_NO_REDIRECT, "confirm": True},
                                        options={"idempotency_key": f"chk-ko-{run}"})
        except stripe.CardError as exc:
            assert exc.code == "card_declined", exc.code
            return "refus card_declined reçu comme attendu"
        raise AssertionError("le paiement aurait dû être refusé")
    check("3. Paiement refusé", card_declined)

    def refunds():
        r1 = c.v1.refunds.create(params={"payment_intent": state["pi"], "amount": 1000}, options={"idempotency_key": f"chk-r1-{run}"})
        r2 = c.v1.refunds.create(params={"payment_intent": state["pi"]}, options={"idempotency_key": f"chk-r2-{run}"})
        again = c.v1.refunds.create(params={"payment_intent": state["pi"]}, options={"idempotency_key": f"chk-r2-{run}"})
        assert again.id == r2.id, "la clé d'idempotence doit renvoyer le même remboursement"
        return f"partiel {r1.status} (10 €), solde {r2.status}, rejeu idempotent OK"
    check("4. Remboursements partiel + total", lambda: refunds() if "pi" in state else (_ for _ in ()).throw(RuntimeError("étape 2 en échec")))

    def bancontact():
        # Comptes récents : les moyens de paiement se règlent dans le dashboard ; on lit ceux que Stripe rend éligibles.
        pi = c.v1.payment_intents.create(params={"amount": 4900, "currency": "eur", "automatic_payment_methods": {"enabled": True}})
        c.v1.payment_intents.cancel(pi.id)
        methods = list(pi.payment_method_types or [])
        assert "bancontact" in methods, f"Bancontact absent des moyens éligibles : {methods}"
        return f"Bancontact éligible en EUR ; moyens actifs : {', '.join(methods)}"
    check("5. Bancontact activé", bancontact)

    def checkout_via_app():
        os.environ.setdefault("SHOPFORGE_SECRET_KEY", "verify-stripe")
        from app import db as dbm
        dbm.configure("sqlite:///:memory:")
        dbm.init_db()
        from app.models import Order, OrderItem, PaymentAccount, Shop
        from app.services.payments import StripeProvider
        from app.services.security import encrypt_secret
        db = dbm.SessionLocal()
        shop = Shop(slug=f"verif-{run}", name="Vérification", payment_mode="test")
        db.add(shop)
        db.flush()
        acc = PaymentAccount(shop_id=shop.id, provider="stripe", mode="test", status="connected", secret_enc=encrypt_secret(key))
        o = Order(shop_id=shop.id, number="VERIF-1", email="verif@example.com", currency="EUR", subtotal_cents=12900,
                  discount_cents=1290, shipping_cents=690, total_cents=12300, idempotency_key=f"verif-{run}",
                  shipping_method={"name": "Standard"}, discount_code="TEST10")
        db.add_all([acc, o])
        db.flush()
        db.add(OrderItem(shop_id=shop.id, order_id=o.id, title="Produit test", variant_label="M", qty=1, unit_price_cents=12900))
        db.flush()
        db.refresh(o)
        url = StripeProvider(shop, acc).create_checkout(o, os.getenv("SHOPFORGE_BASE_URL", "http://localhost:8000"))
        s = c.v1.checkout.sessions.retrieve(o.provider_ref)
        assert s.amount_total == 12300, s.amount_total
        return f"session {s.id}, total {s.amount_total/100:.2f} € (= total boutique), méthodes {s.payment_method_types}\n       → {url}"
    check("6. Checkout créé par le code de la boutique", checkout_via_app)

    print("\nVérification Stripe (mode test)\n")
    for status, label, detail in results:
        print(f"{status} {label}\n       {detail}")
    print("\nÉtape manuelle : ouvrir l'URL Checkout, payer avec 4242 4242 4242 4242 (accepté) puis recommencer avec "
          "4000 0000 0000 0002 (refusé) ; tester Bancontact sur la page de test Stripe.\n"
          "Webhook : stripe listen --forward-to localhost:8000/webhooks/stripe/<shop_id> --events checkout.session.completed,checkout.session.async_payment_succeeded,checkout.session.async_payment_failed,checkout.session.expired,payment_intent.payment_failed,refund.created,refund.updated puis vérifier le statut de la commande.")
    sys.exit(0 if all(s in (OK, WAIT) for s, _, _ in results) else 1)


if __name__ == "__main__":
    main()
