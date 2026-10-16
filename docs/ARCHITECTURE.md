# ShopForge — architecture, services, coûts et critères du MVP

Document rédigé **avant** le développement, puis mis à jour à la livraison.

## 1. Choix d'architecture (argumentés)

| Décision | Choix MVP | Pourquoi | Évolution prévue |
|---|---|---|---|
| Langage / framework | Python 3.12+, FastAPI | Typage (Pydantic), écosystème scraping/data (BeautifulSoup, openpyxl), SDK Stripe et Anthropic officiels, tests simples (TestClient). | Inchangé. |
| Rendu | HTML rendu serveur (Jinja2) + JS minimal | Boutiques rapides (pas de bundle JS lourd), SEO natif, accessibles sans JS, une seule base de code à tester. | Thèmes supplémentaires ; front headless possible via API JSON. |
| Base de données | SQLAlchemy 2 ; SQLite en dev/test, **PostgreSQL** en production (`DATABASE_URL`) | Même code ; SQLite rend les tests reproductibles. | Postgres + Row Level Security en défense en profondeur. |
| Multi-boutiques | Une base partagée, **`shop_id` sur chaque table métier**, accès uniquement via `scoped(db, Model, shop)` et `get_owned(...)` | Isolation applicative centralisée et testée (une seule porte d'accès). | RLS Postgres (`SET app.shop_id`). |
| Auth / rôles | Sessions signées (itsdangerous), mots de passe scrypt, CSRF par formulaire, rôles `owner/admin/staff/viewer` par boutique | Pas de dépendance externe, suffisant pour un MVP. | SSO/OAuth, 2FA. |
| Secrets | Variables d'environnement ; clés marchandes chiffrées en base (Fernet, clé dérivée de `SHOPFORGE_SECRET_KEY`) | Jamais de secret en clair en base ni dans les logs. | KMS / Vault. |
| Paiement | Interface `PaymentProvider` : **Stripe** (Checkout hébergé + webhooks signés + remboursements) et **simulateur de test intégré** | Stripe Checkout gère carte, Bancontact, iDEAL, PayPal (si activé sur le compte), Apple/Google Pay : aucune donnée carte ne transite par la plateforme. | PayPal direct, Mollie (fort au Benelux). |
| Tâches de fond | Table `jobs` + worker (`python -m app.worker`) ; exécution inline en test | Durable (survit au redémarrage), observable dans l'UI. | Celery/RQ + Redis si volumétrie. |
| IA | SDK Anthropic (`claude-opus-5-5`), **optionnel** ; repli déterministe (règles) si pas de clé | La plateforme reste utilisable et testable sans IA ; chaque appel est tracé et budgété par boutique. | Recherche web serveur pour découvrir les concurrents. |
| Analyse d'URL | Garde SSRF (schéma, ports, résolution DNS, IP privées/réservées, redirections revalidées, taille, type MIME), respect de `robots.txt`, contenu traité **comme donnée** | Exigence §8. | Proxy de sortie dédié (pinning IP). |
| Observabilité | Logs JSON avec `request_id`, table `audit_logs`, `ai_traces`, `jobs` ; hook Sentry si `SENTRY_DSN` | Traçabilité exigée §7-J. | OpenTelemetry. |

### Schéma logique

```
Navigateur commerçant ──► /app/* (FastAPI, rôles) ──┐
Navigateur client     ──► /s/{slug}/* (vitrine)  ──┼──► services/ ──► SQLAlchemy ──► SQLite/Postgres
Stripe                ──► /webhooks/stripe/{shop}──┘        │
                                                             ├──► jobs (import, analyse, veille)
                                                             ├──► url_guard → web public (robots.txt)
                                                             └──► ai.py → API Anthropic (optionnel, budgété)
```

### Modèle de données (principales tables)
`users, memberships, shops, briefs, shop_versions` (contenu versionné : thème, pages, collections, traductions),
`products, variants (stock, reserved, cost), size_guides, suppliers, stock_alerts`,
`customers (consentements), carts, orders, order_items, payments, payment_events (unicité provider+event_id), refunds, return_requests`,
`discount_codes, shipping_zones, tax_settings, payment_accounts (secrets chiffrés)`,
`competitor_analyses, competitors, sourced_facts, financial_figures, simulations, recommendations, review_insights`,
`approval_requests, auto_permissions, experiments, store_events, marketing_drafts, email_outbox, watch_targets, watch_alerts`,
`jobs, audit_logs, ai_traces, support_tickets, size_recommendations, channel_spend`.

## 2. Services et accès nécessaires

| Service | Nécessaire pour | Qui le fournit | Automatique ? |
|---|---|---|---|
| Hébergement (conteneur + Postgres managé) | Production | Plateforme | Oui (déploiement) |
| Compte **Stripe** (test puis live) | Paiements réels | **Commerçant** (KYC, IBAN, identité) | Non : guidage seulement |
| PayPal / Bancontact / iDEAL | Moyens locaux | Activés dans le dashboard Stripe du commerçant | Non |
| Clé **API Anthropic** | Analyse/rédaction assistée | Plateforme | Optionnel |
| Fournisseur email transactionnel (Postmark, Brevo…) | Emails réels | Plateforme + domaine du commerçant (SPF/DKIM) | MVP : boîte d'envoi simulée (`email_outbox`) |
| Transporteurs (bpost, Colissimo, Mondial Relay, Sendcloud) | Étiquettes, suivi | Commerçant | MVP : tarifs/délais saisis, numéro de suivi manuel |
| Registrar de domaine | Domaine propre | Commerçant (achat soumis à validation) | Non |
| Données concurrentielles payantes (Similarweb, Semrush) | Trafic estimé | Optionnel, abonnement | Non — sans elles, le trafic est « non disponible » |

## 3. Estimation des coûts de fonctionnement (hypothèses explicites)

Hypothèses : 100 boutiques actives, 1 analyse concurrentielle complète / boutique / mois, 10 cibles de veille / boutique contrôlées quotidiennement, 300 commandes / boutique / mois, IA activée.

| Poste | Hypothèse | Coût mensuel estimé |
|---|---|---|
| Hébergement app + worker | 2 petites instances (2 vCPU / 4 Go) | 40–80 € |
| Postgres managé | 1 instance 2 vCPU, sauvegardes 7 j | 30–60 € |
| Stockage objets (images) | 20 Go + trafic | 5–15 € |
| IA – analyse concurrentielle | ~10 pages × 10 concurrents, ~150 k tokens entrée + 15 k sortie / analyse à 4 $/20 $ par M (Opus 5.5) ≈ 0,90 $ | ≈ 90 $ |
| IA – génération/édition/assistant | ~50 requêtes de 5 k tokens / boutique / mois ≈ 1,5 $ | ≈ 150 $ |
| Veille (fetch HTTP, sans IA) | 30 000 requêtes / mois | négligeable (bande passante) |
| Emails transactionnels | 300 × 3 emails × 100 boutiques ≈ 90 k | 50–100 € |
| Frais de paiement | Supportés par le commerçant (Stripe : de l'ordre de 1,5 % + 0,25 € carte EEE ; à vérifier sur la grille Stripe en vigueur) | — |

Ordre de grandeur : **≈ 450–600 € / mois pour 100 boutiques**, soit 4,5–6 € / boutique. Le budget IA est plafonné par boutique (`ai_budget_usd`, défaut 5 $/mois) et visible dans l'interface. Ces chiffres sont des estimations à recaler sur la facturation réelle.

## 4. Critères de réussite du MVP

1. Un commerçant crée un compte et une boutique à partir d'un prompt en < 5 minutes, voit les hypothèses retenues et peut les modifier.
2. Les questions posées se limitent aux informations bloquantes (approvisionnement/statut revendeur, langues, pays de livraison).
3. L'analyse concurrentielle produit, pour chaque concurrent accessible, des faits **avec URL et date de collecte** ; tout chiffre financier sans source est affiché « non disponible » ; les simulations sont séparées et étiquetées.
4. Import CSV/XLSX avec rapport d'erreurs ligne par ligne ; stock par variante.
5. Commande test complète, paiement échoué, remboursement : statuts et stocks cohérents ; événements dupliqués ignorés.
6. Tableau de bord distinguant CA, marge brute, marge contributive ; résultat net « non disponible » si charges fixes absentes.
7. Publication impossible sans validation explicite ; score de préparation listant les blocages réels.
8. Isolation vérifiée entre deux boutiques ; vitrine utilisable à 375 px de large sans défilement horizontal.
