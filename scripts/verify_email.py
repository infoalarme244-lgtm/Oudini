"""Envoie UN email de test via le fournisseur configuré (EMAIL_PROVIDER=postmark|brevo|smtp, EMAIL_FROM, clé).
Usage : python scripts/verify_email.py destinataire@exemple.com"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("SHOPFORGE_SECRET_KEY", "verify-email")


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    from app import db as dbm
    dbm.configure("sqlite:///:memory:")
    dbm.init_db()
    from app.config import settings
    from app.models import EmailOutbox, Shop
    from app.services import mailer
    if settings.email_provider == "outbox":
        sys.exit("EMAIL_PROVIDER=outbox : aucun fournisseur configuré (postmark, brevo ou smtp).")
    db = dbm.SessionLocal()
    shop = Shop(slug="verif-email", name="Vérification ShopForge")
    db.add(shop)
    db.flush()
    m = EmailOutbox(shop_id=shop.id, to=sys.argv[1], subject="Test d'envoi ShopForge", body="Si vous lisez ceci, l'envoi fonctionne.")
    db.add(m)
    db.commit()
    stats = mailer.send_pending(db)
    db.refresh(m)
    print(stats, "|", m.status, m.provider_id or m.error)
    sys.exit(0 if m.status == "sent" else 1)


if __name__ == "__main__":
    main()
