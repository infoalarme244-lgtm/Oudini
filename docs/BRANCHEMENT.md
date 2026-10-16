# Brancher les vrais services

Chaque étape indique ce que **vous** faites (compte, identité, DNS) et ce qui est **déjà prêt** dans le code. Ne collez jamais une clé dans une conversation : mettez-la dans `.env` sur le serveur, ou dans les variables d'environnement de l'environnement cloud.

## 1. Stripe en mode test

**Vous :**
1. Créez un compte sur dashboard.stripe.com et restez en **mode test**. Aucune vérification d'identité n'est nécessaire pour tester.
2. Dans *Paramètres → Moyens de paiement*, activez **Cartes** et **Bancontact**, et **PayPal** si l'option est proposée pour votre pays.
3. Dans *Développeurs → Clés API*, créez une **clé restreinte** (`rk_test_…`) avec les droits en écriture sur Checkout Sessions, Payment Intents, Refunds et Coupons, et en lecture sur Account.

**Vérification automatique :** `STRIPE_TEST_SECRET_KEY=rk_test_… python scripts/verify_stripe.py`. Le script vérifie :
- un paiement accepté ;
- un paiement refusé ;
- un remboursement partiel puis total, avec un rejeu idempotent ;
- l'activation de Bancontact ;
- une session Checkout créée par le vrai code de la boutique, dont le total doit être identique à celui de la commande.

**Vérification de bout en bout (webhooks) :**
1. Installez la CLI Stripe, puis lancez `stripe listen --forward-to localhost:8000/webhooks/stripe/<shop_id> --events checkout.session.completed,checkout.session.async_payment_succeeded,checkout.session.async_payment_failed,checkout.session.expired,payment_intent.payment_failed,refund.created,refund.updated`. La commande affiche un secret `whsec_…`. Les versions récentes de la CLI exigent la liste `--events` ; sous Windows, utilisez `.\stripe.exe`.
2. Dans la boutique, ouvrez *Paiements → Connecter Stripe (test)* et saisissez la clé et ce `whsec_…`.
3. Passez une commande, puis payez avec `4242 4242 4242 4242` (accepté), avec `4000 0000 0000 0002` (refusé), puis avec Bancontact sur la page de test Stripe.
4. Remboursez depuis la fiche commande et vérifiez que les statuts et le stock suivent.

En production, remplacez `stripe listen` par un endpoint webhook déclaré dans le dashboard : `https://<domaine>/webhooks/stripe/<shop_id>`. La liste des événements à cocher est affichée dans la page Paiements.

**Passage en paiements réels :** activez le compte Stripe (identité, IBAN), connectez une clé `rk_live_…` et son `whsec_…`, puis faites la demande d'activation. Elle doit être validée explicitement dans *Validations*.

## 2. Hébergement

**Vous :** un serveur Linux avec Docker (2 vCPU / 4 Go suffisent pour démarrer, chez Hetzner, OVHcloud, Scaleway…), un nom de domaine, et un enregistrement DNS `A` pointant vers le serveur.

**Déjà prêt :** `docker-compose.yml` contient PostgreSQL 16, l'application, le worker, Caddy (HTTPS Let's Encrypt automatique) et une sauvegarde quotidienne conservée 14 jours.

```bash
git clone <dépôt> && cd shopforge
cp .env.example .env    # remplir DOMAIN, SHOPFORGE_BASE_URL, SHOPFORGE_SECRET_KEY, POSTGRES_PASSWORD
docker compose up -d --build
curl https://<domaine>/healthz      # {"ok": true, "db": "ok", ...}
```

`SHOPFORGE_SECRET_KEY` ne doit **jamais changer** une fois en production : elle chiffre les clés Stripe des commerçants. Copiez aussi les sauvegardes (`./backups`) hors du serveur.

**Analyse des concurrents :** le serveur doit avoir une sortie internet directe. La protection anti-SSRF refuse les URL dont le nom ne se résout pas vers une adresse publique. C'est pour cette raison qu'aucun site réel n'a encore été analysé dans l'environnement de développement actuel.

## 3. Emails (Postmark ou Brevo)

**Vous :**
1. Créez un compte Postmark (recommandé pour les emails transactionnels) ou Brevo.
2. Vérifiez un domaine d'envoi, par exemple `mail.mondomaine.be` : ajoutez chez votre registrar les enregistrements **SPF**, **DKIM** et **Return-Path** (ou CNAME) indiqués par le fournisseur, plus un enregistrement **DMARC** (`v=DMARC1; p=none; rua=mailto:…` pour commencer).
3. Avec Postmark, créez aussi un flux **Broadcast**, réservé aux emails marketing.

**Configuration (`.env`) :** `EMAIL_PROVIDER=postmark`, `EMAIL_FROM=boutique@mail.mondomaine.be`, `POSTMARK_SERVER_TOKEN=…` (ou `BREVO_API_KEY`, ou `SMTP_URL`).

**Vérification :** `python scripts/verify_email.py vous@exemple.com`.

**Déjà prêt :**
- Le worker envoie la file d'attente, réessaie jusqu'à 5 fois avec un délai croissant et conserve les erreurs.
- Les emails marketing passent par le flux broadcast, avec l'en-tête `List-Unsubscribe` et le désabonnement en un clic (RFC 8058).
- Le consentement est revérifié juste avant chaque envoi.

## 4. Claude (facultatif)

**Vous :** créez une clé API sur console.anthropic.com et fixez une limite de dépense mensuelle.

**Configuration :** `ANTHROPIC_API_KEY=…` et `SHOPFORGE_AI_BUDGET_USD=5`, qui est le budget par boutique.

**Vérification :** `python scripts/verify_ai.py` lance une compréhension de demande et une traduction en néerlandais, puis affiche les tokens et le coût.

**Déjà prêt :**
- Modèle `claude-opus-5-5`, avec bascule automatique vers un autre modèle en cas de refus.
- Sorties JSON contraintes par un schéma.
- Contenus tiers transmis comme données délimitées, jamais comme instructions.
- Budget et limite d'appels par boutique ; repli sur le moteur à règles en cas de refus, d'erreur ou de budget épuisé.
- Chaque appel est tracé dans *Traçabilité*.

## État des vérifications

| Service | Vérifié sans compte (tests automatisés) | Reste à vérifier avec votre compte |
|---|---|---|
| PostgreSQL | les 99 tests passent sur PostgreSQL 16 ; démarrage en mode production, healthcheck, worker | — |
| Stripe | signatures des webhooks, idempotence, machine à états, refus des clés live en mode test | `verify_stripe.py`, puis Checkout avec webhooks |
| Emails | format exact des requêtes Postmark et Brevo, réessais, consentement, désabonnement en un clic | `verify_email.py`, puis délivrabilité (SPF/DKIM/DMARC) |
| Claude | forme de la requête validée par le SDK officiel, calcul du coût, budget, repli en cas de refus | `verify_ai.py` (qualité réelle des réponses) |
| Docker | — (pas de démon Docker dans l'environnement de développement) | `docker compose up` sur le serveur |
