"""Appel réel à Claude (ANTHROPIC_API_KEY requis) : compréhension d'une demande, coût et trace.
Usage : python scripts/verify_ai.py"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("SHOPFORGE_SECRET_KEY", "verify-ai")


def main():
    if not os.getenv("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY manquant.")
    from app import db as dbm
    dbm.configure("sqlite:///:memory:")
    dbm.init_db()
    from app.models import AiTrace, Shop
    from app.services import ai, brief
    db = dbm.SessionLocal()
    shop = Shop(slug="verif-ai", name="Vérification", ai_budget_usd=1.0)
    db.add(shop)
    db.flush()
    res = brief.understand(db, shop, "Boutique de montres automatiques pour hommes trentenaires en Belgique, style élégant.")
    t = db.query(AiTrace).filter(AiTrace.engine != "rules").first()
    if not t:
        sys.exit("❌ Aucun appel IA abouti (clé invalide, refus, réseau ?). Voir les logs.")
    print("✅ Claude a répondu :", t.output)
    print(f"   tokens {t.tokens_in}/{t.tokens_out}, coût {t.cost_usd:.4f} $ ; clientèle extraite : {res['extracted']['audience']}")
    out = ai.complete_json(db, shop, "translate_check", "Traduis en néerlandais (Belgique) la valeur 'texte'.",
                           '{"texte": "Livraison offerte dès 50 €"}',
                           {"type": "object", "additionalProperties": False, "properties": {"texte": {"type": "string"}}, "required": ["texte"]})
    print("✅ Traduction :", out)


if __name__ == "__main__":
    main()
