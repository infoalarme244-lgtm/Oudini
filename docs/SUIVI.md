# Suivi du projet

## Fait et vérifié
- MVP complet : parcours idée → boutique, analyse concurrentielle sourcée, multi-catégories (chaussures, montres, bijoux, accessoires, vêtements, beauté, électronique, maison), catalogue, commandes, marges, gouvernance.
- 100 tests automatisés, sur SQLite et PostgreSQL 16.
- **Stripe, compte de test réel** (`scripts/verify_stripe.py`) : clé et compte ✅, paiement carte accepté ✅, refus ✅, Bancontact disponible ✅, page de paiement créée par la boutique avec un total exact ✅. Remboursement : soumis à approbation humaine par la clé restreinte, désormais géré par la boutique (« en attente d'approbation »).
- **Test manuel en local** (Windows, Stripe CLI avec `--events`) : commandes payées par carte, refus et Bancontact. Résultat rapporté « semble bien fonctionner » ; détail des statuts non transmis.

- **Démarrage par une photo** (nouveau) : boutique construite autour de la photo du produit, avec une palette issue de l'image, une vitrine animée et un produit en brouillon. Analyse par Claude si une clé est configurée (vérifiée avec un client simulé : le vrai appel reste à tester avec `scripts/verify_ai.py` et une clé).

- **Parcours simple en 4 étapes** (nouveau) : tout est pré-rempli, le client fournit ses photos, connecte sa banque (Stripe Connect) et valide. Vérifié avec des registres et un Stripe simulés ; reste à tester avec une vraie clé de plateforme (Connect activé) et avec les vrais registres publics depuis un serveur.

- **Création depuis le lien d'un produit** (équivalent maison de « Store AI » de Copyfy, sans recopier) : vérifié avec des pages simulées ; à tester sur de vraies pages depuis un serveur ou votre ordinateur, car le réseau de l'environnement de développement bloque les sites.

- **Visuels générés par IA** (nouveau) : 4 mises en situation à partir de la photo du commerçant, validées une par une, avec la mention « Visuel généré par IA ». Vérifié avec des fournisseurs simulés ; à tester avec une vraie clé OpenAI ou Gemini (`scripts/verify_images.py`). Les faux avis, faux experts et fausses statistiques visibles chez Copyfy ne sont volontairement pas reproduits (interdits en Europe).

## À faire
1. ~~Renouveler la clé Stripe de test~~ : fait. Reste à désactiver EPS, MB Way et Satispay dans le dashboard.
2. Activer **Stripe Connect** sur votre compte Stripe (mode test), puis tester le parcours simple jusqu'à « compte connecté » (voir `docs/BRANCHEMENT.md`, section 0).
3. Hébergement : serveur Docker, domaine, `docker compose up -d` (voir `docs/BRANCHEMENT.md`), puis webhook Stripe déclaré sur le domaine.
3. Emails : compte Postmark ou Brevo, DNS SPF/DKIM/DMARC, puis `scripts/verify_email.py`.
4. Claude (facultatif) : `ANTHROPIC_API_KEY`, puis `scripts/verify_ai.py`.
5. Première analyse de vrais concurrents depuis le serveur : nécessite une sortie internet directe.

## Reprendre en local (Windows PowerShell)
Télécharger le ZIP de `main`, installer les dépendances, puis lancer la boutique et la Stripe CLI : voir les étapes A à D dans l'historique, ou `docs/BRANCHEMENT.md`.
