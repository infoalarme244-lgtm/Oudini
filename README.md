# ShopForge — MVP

Plateforme SaaS multi-boutiques : d'un prompt, d'une URL concurrente ou d'un catalogue à une boutique exploitable, différenciée et mesurable, sans promesse de ventes.

Architecture, services nécessaires, coûts estimés et critères de réussite : [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).
Brancher Stripe, l'hébergement, les emails et Claude : [`docs/BRANCHEMENT.md`](docs/BRANCHEMENT.md) (scripts `scripts/verify_*.py`, `docker-compose.yml`).

## Types de produits pris en charge

Tout ce qui dépend du type de produit est regroupé dans `app/services/verticals.py`. Chaque profil définit les variantes, les caractéristiques, les filtres, les collections, l'aide au choix de taille, les points de conformité, les thèmes d'avis, les concurrents candidats et les données de démo :

| Verticale | Variantes | Caractéristiques (exemples) | Aide au choix | Conformité signalée |
|---|---|---|---|---|
| Chaussures | pointure, couleur | — | longueur du pied → pointure | étiquetage des matériaux |
| Montres | taille de bracelet, cadran/bracelet | mouvement, diamètre, étanchéité, verre, garantie | tour de poignet → bracelet | piles/DEEE, garantie constructeur, distribution sélective |
| Bijoux | taille, métal | métal *(obligatoire)*, pierre, poinçon, nickel | tour de doigt → taille de bague | poinçons, nickel (REACH) |
| Accessoires | taille, couleur | matière, dimensions, contenance, catégorie UV | tour de taille → ceinture | dénomination « cuir », lunettes CE |
| Vêtements | taille, couleur | composition *(obligatoire)*, coupe, entretien | tour de poitrine → taille | fibres (UE 1007/2011), AGEC |
| Beauté | contenance, teinte | INCI *(obligatoire)*, type de peau, PAO, personne responsable | — | règlement 1223/2009, allégations |
| Électronique | capacité/modèle, couleur | compatibilité, autonomie, CE *(obligatoire)*, garantie | — | CE, DEEE, réparabilité |
| Maison | dimensions, finition | matière, dimensions, poids | — | contact alimentaire, bougies |
| Générique | taille/format, couleur | matière, dimensions | — | — |

Une boutique peut mêler plusieurs verticales, par exemple « montres et sacs en cuir ». Dans ce cas, la plateforme crée une collection par verticale et réunit les filtres de chacune. Les concurrents candidats sont alternés entre les verticales. Chaque fiche produit utilise le profil de sa propre catégorie. À l'import, la colonne `category` accepte « montres », « bijoux », etc., et les colonnes `attr_<clé>` alimentent les caractéristiques. Une caractéristique obligatoire manquante bloque la publication. Ajouter une verticale revient à ajouter un profil.

## Démarrer

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
export SHOPFORGE_SECRET_KEY=$(python -c "import secrets;print(secrets.token_hex(32))")
export SHOPFORGE_BASE_URL=http://localhost:8000
uvicorn app.main:create_app --factory --port 8000      # application
python -m app.worker                                     # tâches de fond (imports, analyses, veille, expirations)
python -m pytest tests -q                                # tests
```

Variables facultatives : `DATABASE_URL` (PostgreSQL en production), `ANTHROPIC_API_KEY` (synthèses et traductions IA, budget plafonné par boutique), `SENTRY_DSN`, `SHOPFORGE_ENV=prod` (rend la clé secrète obligatoire).

## Parcours simple (par défaut)

Le client n'a que trois choses à faire : fournir ses photos, connecter son compte bancaire et valider. Tout le reste est pré-rempli :

1. **Créer** (`/app/new`) : une photo, le type de produit (ou « laisser l'IA deviner ») et les pays de vente. La boutique est générée immédiatement : contenu, thème, TVA, tarifs de livraison proposés et analyse des concurrents.
2. **Votre produit** : titre et description proposés par l'IA (si une clé Claude est configurée) et prix suggéré (médiane des concurrents analysés, sinon fourchette du positionnement). Le client indique son stock et corrige si besoin.
3. **Votre entreprise** : le numéro BCE/TVA ou SIREN suffit. Nom, adresse, forme juridique et n° de TVA sont repris des registres publics (VIES pour la Belgique, Annuaire des entreprises pour la France), et les pages légales sont rédigées avec ces informations.
4. **Recevoir les paiements** : bouton « Connecter mon compte bancaire » ; l'identité et l'IBAN se règlent chez Stripe, via Stripe Connect.
5. **Vérifier et publier** : récapitulatif de ce qui a été préparé, une case à cocher, publication. La case cochée vaut validation explicite et est tracée.

Le mode expert (toutes les pages détaillées) reste accessible depuis le menu.

## Démarrer par une photo

Sur `/app/new`, une seule photo du produit suffit. La boutique est alors construite autour d'elle :
- **palette tirée des couleurs de la photo**, avec des contrastes lisibles vérifiés (texte ≥ 7:1, boutons ≥ 4,5:1) ;
- **page d'accueil avec la photo en grand visuel et des animations** : apparitions au défilement, bandeau défilant, halo, effets au survol. Les animations sont automatiquement désactivées pour les visiteurs qui ont demandé à les réduire, et le commerçant peut les couper (« Désactive les animations ») ;
- **produit créé en brouillon** avec la photo : le prix, le stock, la marque et la provenance restent à renseigner par le commerçant.

Avec une clé Claude, la photo est analysée : type de produit, catégorie, caractéristiques visibles, titre et description à relire. Une marque visible est présentée « à confirmer » ; elle ne prouve ni l'authenticité ni le droit de revendre. Sans clé, une seule question est posée : le type de produit.

La photo est vérifiée (JPEG, PNG ou WebP, 8 Mo maximum) et ré-encodée sans métadonnées (GPS, appareil). Elle n'est affichée publiquement que si le commerçant déclare en avoir le droit.

## Parcours

1. `/signup` → `/app/new` : prompt, URL concurrente ou CSV/XLSX (une seule entrée suffit).
2. **Demande & hypothèses** : marché, pays, langues, devise, catégorie, marques, positionnement, style, avec l'origine de chaque valeur (demande / défaut / modifié). Seules les questions bloquantes sont posées : langue NL pour la Belgique, mode d'approvisionnement de la marque.
3. Choix d'un positionnement parmi 3, identité visuelle originale, structure de catalogue → **prévisualisation privée** (lien secret, `noindex`, renouvelable).
4. Analyse concurrentielle en tâche de fond, catalogue, livraison, TVA, paiements, pages légales.
5. **Lancement** : score de préparation avec les blocages réels → demande de publication → validation explicite.

Chaque étape est marquée *Automatique*, *Connexion externe* ou *Validation du commerçant*.

## État à la livraison

### Fonctionne et est testé (117 tests automatisés, sur SQLite et PostgreSQL 16, tous verts)

| Domaine | Vérifié par |
|---|---|
| Commande test complète (panier → paiement simulé signé → payée → stock décrémenté → expédiée → suivi client) | `test_complete_test_order` |
| Paiement échoué : stock libéré, nouvelle tentative, puis succès | `test_failed_payment_releases_stock_and_retry` |
| Remboursement partiel puis total, remise en stock, dépassement refusé, remboursement d'une commande impayée refusé | `test_refunds_partial_then_full_with_restock`, `test_refund_unpaid_order_refused` |
| Événements dupliqués ignorés, échec tardif sans effet sur une commande payée, signature invalide rejetée | `test_duplicate_events_processed_once` |
| Double soumission du paiement → une seule commande | `test_checkout_idempotent` |
| Montant reçu ≠ montant attendu → la commande n'est pas validée | `test_amount_mismatch_not_marked_paid` |
| Cohérence du stock : pas de survente concurrente, expiration des réservations, contrainte stock ≥ 0 en base | `test_stock_never_oversold`, `test_stock_cannot_go_below_reservations` |
| Webhook Stripe signé (HMAC + horodatage), doublon, mauvais secret, incohérence test/live | `test_stripe_webhook_signed_flow` |
| Isolation entre deux commerçants (back-office, vitrine, webhooks, export), rôles, CSRF, boutique non publiée invisible | `test_isolation.py` |
| Mobile : achat complet dans Chromium à 375 px, aide pointure, assistant, pages commerçant, aucun défilement horizontal | `test_mobile.py` (captures dans `docs/screenshots/`) |
| Aucun chiffre concurrentiel inventé : financiers « non disponible » par défaut, validation des sources, groupe ≠ boutique en ligne, simulations séparées | `test_analysis.py` |
| Anti-SSRF (IP privées, métadonnées cloud, IPv6 mappée, rebinding DNS, redirections, ports, schémas), robots.txt, 403 non contourné, injections d'instructions détectées et neutralisées | `test_url_guard.py`, `test_analysis_sourced_and_honest` |
| Publication, paiements réels, prix en masse, emails, campagnes : validation obligatoire ; autorisations automatiques limitées et révocables (jamais pour la publication) | `test_governance.py` |
| Copilote de marge (calcul vérifié au centime), promotion déficitaire signalée, tableau de bord CA / marge brute / contributive / résultat net « non disponible » | `test_unit_economics_and_missing_data`, `test_dashboard_separates_metrics` |
| Import CSV/XLSX avec rapport ligne par ligne, ré-import idempotent, export | `test_csv_import_validation_and_export`, `test_xlsx_import` |
| Multi-catégories : détection (montres, bijoux, accessoires, beauté…), boutique mixte, filtres par caractéristique, aide au choix poignet/doigt/taille, INCI obligatoire, import `attr_*`, recommandations par verticale, boutique générique | `test_verticals.py`, `test_mobile_watch_shop` |
| Parcours simple : création en un écran, pré-remplissage, recherche d'entreprise FR/BE (clés de contrôle, registres simulés), Stripe Connect (inscription, webhook par compte, paiement encaissé sur le compte du commerçant), validation et publication en un clic | `test_simple_launch.py`, `test_simple_launch_screens` |
| Démarrage par photo : palette et contrastes, nettoyage des métadonnées, droits d'utilisation, analyse par Claude (simulée), animations et réduction des animations | `test_photo.py`, `test_photo_shop_animated` |
| Édition par conversation (style, collection, traduction NL, pack rentable), prévisualisation, application, retour arrière | `test_chat_edit_preview_apply_rollback` |
| Assistant d'achat limité aux données réelles, transfert au commerçant en cas de doute ; aide pointure avec incertitude ; A/B sans gagnant sur données insuffisantes ; veille avec suggestions qui protègent la marge | `test_features.py` |

### Nécessite une connexion externe ou une action du commerçant

- **Stripe** : la plateforme gère la connexion des clés (chiffrées), Checkout hébergé, webhooks et remboursements. L'ouverture du compte, la vérification d'identité, l'IBAN et l'activation de Bancontact ou PayPal se font **chez Stripe**. Les appels à l'API Stripe réelle n'ont pas été exécutés ici (pas de compte de test) ; la vérification des signatures de webhook l'a été.
- **Analyse de sites réels** : dans ce conteneur, il n'y a pas de résolution DNS directe et la garde anti-SSRF refuse donc les URL publiques. L'analyse a été validée sur de faux sites en local. En production, elle demande une sortie réseau directe ou un proxy de sortie dédié. Les listes de concurrents candidats par verticale sont **indicatives** et doit être confirmée par le commerçant.
- **IA (Claude)** : facultative. Sans clé, des moteurs à règles prennent le relais. Avec une clé, elle sert aux synthèses de positionnement et à la traduction NL, avec budget et traçabilité. Le chemin IA n'a pas été exécuté ici.
- **Emails** : envoi via Postmark, Brevo ou SMTP déjà implémenté et testé au niveau du protocole. Il reste à créer le compte, à vérifier le domaine (SPF/DKIM/DMARC) et à lancer `scripts/verify_email.py`.
- **Transporteurs** : tarifs et délais saisis à la main, numéro de suivi manuel. Pas encore d'intégration Sendcloud, bpost ou Colissimo.
- **Domaine et régies publicitaires** : demande validée, puis action manuelle chez le registrar ou dans la régie.
- **À valider par le commerçant (idéalement avec un juriste ou un comptable)** : pages légales, taux de TVA (pré-remplis avec les taux normaux, régime OSS), statut de revendeur, droits sur les images, provenance des produits.

### Limites connues du MVP

Recherche et filtres en mémoire (prévoir un index pour les gros catalogues). Isolation applicative (ajouter la RLS PostgreSQL en défense en profondeur). Pas de pinning d'IP lors du fetch : le rebinding DNS est filtré à la résolution, mais il faut un proxy de sortie en production. Pas de Stripe Connect OAuth : connexion par clés restreintes. Comptes clients sans réinitialisation de mot de passe.
