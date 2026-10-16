"""Marketing assisté : brouillons à valider, envoi uniquement aux contacts consentants, désabonnement en un clic."""
from sqlalchemy import select

from ..config import settings
from ..models import Customer, EmailOutbox, MarketingDraft, Product
from . import approvals, verticals
from .approvals import executor


def draft_templates(db, shop, snapshot) -> list[dict]:
    brand = snapshot.get("brand", {}).get("name", shop.name)
    prods = db.scalars(select(Product).where(Product.shop_id == shop.id, Product.status == "active", Product.is_demo.is_(False)).limit(3)).all()
    names = ", ".join(p.title for p in prods) or "[produits à choisir]"
    return [
        {"kind": "email", "title": f"Bienvenue chez {brand}",
         "body": f"Bonjour,\n\nMerci de votre inscription. Chez {brand}, chaque fiche indique le stock réel par variante, "
                 f"le délai de livraison vers votre pays et la provenance des produits.\n\nÀ découvrir : {names}.\n"},
        *[{"kind": "seo", "title": verticals.get(k)["seo"][0], "body": verticals.get(k)["seo"][1]}
          for k in (snapshot.get("verticals") or [None])],
        {"kind": "social", "title": "Publication : nouveaux arrivages",
         "body": f"Nouveautés en stock : {names}. Stock et délais affichés en temps réel sur le site."},
        {"kind": "paid_campaign", "title": "Campagne recherche — marque boutique", "channel": "google_ads",
         "body": "Ciblage : requêtes de marque de la boutique et catégories en FR/BE. Budget à définir et valider.", "budget_cents": None},
    ]


def audience(db, shop_id) -> list[Customer]:
    return db.scalars(select(Customer).where(Customer.shop_id == shop_id, Customer.marketing_consent.is_(True),
                                             Customer.unsubscribed_at.is_(None))).all()


def request_send(db, shop, draft: MarketingDraft, user_id):
    if draft.kind != "email":
        raise ValueError("Seuls les emails sont envoyés depuis la plateforme")
    n = len(audience(db, shop.id))
    draft.status = "pending_approval"
    return approvals.request(db, shop, "send_marketing", f"Envoi de « {draft.title} » à {n} contact(s) consentant(s)",
                             {"draft_id": draft.id, "recipients": n}, user_id)


def request_campaign(db, shop, draft: MarketingDraft, budget_cents: int, user_id):
    if not budget_cents or budget_cents <= 0:
        raise ValueError("Budget explicite obligatoire")
    draft.budget_cents, draft.status = budget_cents, "pending_approval"
    return approvals.request(db, shop, "launch_paid_campaign",
                             f"Campagne « {draft.title} » — budget total {budget_cents/100:.2f} €",
                             {"draft_id": draft.id, "budget_cents": budget_cents}, user_id)


@executor("send_marketing")
def _send(db, shop, payload, user_id):
    draft = db.get(MarketingDraft, payload["draft_id"])
    if draft is None or draft.shop_id != shop.id:
        raise ValueError("Brouillon introuvable")
    sent = 0
    for c in audience(db, shop.id):  # recalculé au moment de l'envoi : désabonnements pris en compte
        link = f"{settings.base_url}/s/{shop.slug}/unsubscribe/{c.unsubscribe_token}"
        one_click = f"{settings.base_url}/unsubscribe/{shop.slug}/{c.unsubscribe_token}"
        db.add(EmailOutbox(shop_id=shop.id, to=c.email, subject=draft.title, kind="marketing",
                           headers={"List-Unsubscribe": f"<{one_click}>", "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"},
                           body=draft.body + f"\n\n—\nVous recevez cet email car vous l'avez accepté. Se désabonner : {link}"))
        sent += 1
    draft.status, draft.sent_count = "sent", sent
    return f"{sent} email(s) placés dans la file d'envoi"


@executor("launch_paid_campaign")
def _campaign(db, shop, payload, user_id):
    draft = db.get(MarketingDraft, payload["draft_id"])
    draft.status = "approved"
    return (f"Budget validé ({payload['budget_cents']/100:.2f} €). Aucune régie publicitaire n'est connectée : "
            "créez la campagne dans votre compte publicitaire avec ce budget plafond.")
