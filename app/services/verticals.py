"""Profils de verticale : tout ce qui dépend du type de produit, au même endroit.

Un profil décrit : mots-clés de détection, axes de variantes (taille, couleur…), attributs techniques,
filtres, collections, outil d'aide au choix (mesure → taille), positionnements, points de conformité,
thèmes d'avis spécifiques et données de démonstration. Ajouter une verticale = ajouter un profil.

Les listes de concurrents candidats sont INDICATIVES (enseignes visibles et pertinentes, non classées
par ventes) et doivent être confirmées par le commerçant.
"""
import re
import unicodedata

PRICE_BANDS_DEFAULT = {"value": (15, 60), "premium_accessible": (60, 180), "premium": (150, 400), "luxury": (400, 3000)}


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFD", (s or "").lower())
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


PROFILES: dict[str, dict] = {
    "footwear": {
        "label": "Chaussures", "keywords": ["basket", "sneaker", "chaussure", "escarpin", "botte", "sandale", "mocassin"],
        "axes": {"size": "Pointure", "color": "Couleur"}, "size_prefix": "EU ",
        "usage_label": "Usage", "usages": ["running", "lifestyle", "basketball", "trail", "ville"],
        "attributes": [], "required_attributes": [],
        "filters": ["size", "color", "usage", "gender", "price", "brand", "in_stock"],
        "collections": [("homme", "Homme", "gender", "homme"), ("femme", "Femme", "gender", "femme"), ("enfant", "Enfant", "gender", "enfant"),
                        ("running", "Running", "usage", "running"), ("lifestyle", "Lifestyle", "usage", "lifestyle"),
                        ("basketball", "Basketball", "usage", "basketball")],
        "fit": {"label": "Guide des tailles", "measure_label": "Longueur du pied (mm), talon → plus long orteil",
                "unit": "mm", "range": (150, 340), "allowance": 5,
                "allowance_note": "Aisance de 5 mm ajoutée à la longueur du pied ; la forme et la largeur du pied ne sont pas prises en compte.",
                "eq_label": "UK / US"},
        "price_bands": {"value": (40, 90), "premium_accessible": (90, 180), "premium": (140, 260), "luxury": (300, 900)},
        "names": ["Foulée", "Pointure Juste", "Semelle Club", "Pas de Côté", "Lacet & Co"],
        "fit_promise": ("La bonne pointure du premier coup", "Choisir sa taille sans hésiter : guide par modèle, conseil de pointure avec niveau de certitude, échange de taille simple."),
        "review_themes": {"taille": ["taille", "pointure", "trop petit", "trop grand", "maat"]},
        "compliance": ["Étiquetage des matériaux (dessus, doublure, semelle) — directive 94/11/CE"],
        "seo": ("Comment choisir sa pointure en ligne", "## Mesurer son pied\nTalon contre un mur, mesurez jusqu'au plus long orteil, en fin de journée.\n\n## Lire un guide des tailles\nLes correspondances EU/UK/US varient selon les marques et parfois selon les modèles.\n"),
        "demo": {"brand": "Marque Démo", "products": [
            ("Runner démo Aéro", "running", "homme", "Bleu nuit", 129, 62, ["40", "41", "42", "43", "44"], {}),
            ("Court démo Classic", "lifestyle", "femme", "Blanc", 109, 51, ["36", "37", "38", "39", "40"], {}),
            ("Hoop démo Pro", "basketball", "homme", "Noir", 149, 78, ["41", "42", "43", "44", "45"], {}),
            ("Trail démo Terra", "running", "femme", "Vert sauge", 139, 66, ["37", "38", "39", "40"], {}),
            ("Junior démo Swift", "lifestyle", "enfant", "Rouge", 69, 30, ["30", "31", "32", "33", "34", "35"], {})],
            "guide": [{"size": str(s), "eq": f"UK {s - 33.5 if s > 34 else s - 33} / US {s - 33}", "measure": round(s * 6.667 - 15, 1)} for s in range(30, 47)]},
        "candidates": [
            ("Zalando", "https://www.zalando.fr/", "Généraliste mode en ligne, très présent en FR et BE"),
            ("Courir", "https://www.courir.com/", "Spécialiste sneakers, réseau de magasins en France"),
            ("Foot Locker", "https://www.footlocker.fr/", "Spécialiste sneakers international, FR et BE"),
            ("JD Sports", "https://www.jdsports.fr/", "Spécialiste sport/sneakers, FR et BE"),
            ("Snipes", "https://www.snipes.com/", "Spécialiste sneakers/streetwear, présent en FR et BE"),
            ("Sarenza", "https://www.sarenza.com/", "Spécialiste chaussures en ligne (France)"),
            ("Spartoo", "https://www.spartoo.com/", "Spécialiste chaussures en ligne (France)"),
            ("Torfs", "https://www.torfs.be/", "Chausseur belge, site FR/NL"),
            ("Avantisport", "https://www.avantisport.be/", "Distributeur sport belge"),
            ("Nike (boutique de marque)", "https://www.nike.com/fr/", "Vente directe de la marque : référence de prix et de services"),
            ("size?", "https://www.sizeofficial.fr/", "Spécialiste sneakers premium"),
            ("Kickz", "https://www.kickz.com/", "Spécialiste basket/sneakers européen")],
    },
    "watches": {
        "label": "Montres", "keywords": ["montre", "horlogerie", "chronographe", "smartwatch"],
        "axes": {"size": "Taille de bracelet", "color": "Cadran / bracelet"}, "size_prefix": "",
        "usage_label": "Style", "usages": ["sport", "habillee", "plongee", "connectee", "casual"],
        "attributes": [("movement", "Mouvement", ["quartz", "automatique", "mecanique", "solaire", "connecte"]),
                       ("case_diameter_mm", "Diamètre du boîtier (mm)", None), ("water_resistance_m", "Étanchéité (m)", None),
                       ("case_material", "Matériau du boîtier", None), ("strap_material", "Bracelet", ["acier", "cuir", "silicone", "nylon", "titane"]),
                       ("glass", "Verre", ["mineral", "saphir", "acrylique"]), ("warranty_months", "Garantie (mois)", None)],
        "required_attributes": [],
        "recommended_attributes": ["movement", "case_diameter_mm", "water_resistance_m", "warranty_months"],
        "filters": ["attr:movement", "attr:strap_material", "attr:case_diameter_mm", "color", "usage", "gender", "price", "brand", "in_stock"],
        "collections": [("homme", "Homme", "gender", "homme"), ("femme", "Femme", "gender", "femme"),
                        ("automatiques", "Automatiques", "attr:movement", "automatique"), ("sport", "Sport", "usage", "sport"),
                        ("habillees", "Habillées", "usage", "habillee"), ("connectees", "Connectées", "usage", "connectee")],
        "fit": {"label": "Taille du bracelet", "measure_label": "Tour de poignet (mm)", "unit": "mm", "range": (120, 260), "allowance": 10,
                "allowance_note": "10 mm ajoutés au tour de poignet pour le confort ; un bracelet métallique peut aussi être ajusté par retrait de maillons.",
                "eq_label": "Longueur"},
        "price_bands": {"value": (30, 120), "premium_accessible": (120, 450), "premium": (400, 1500), "luxury": (1500, 20000)},
        "names": ["Heure Juste", "Cadran Clair", "Atelier Seconde", "Le Comptoir Horloger", "Aiguille & Co"],
        "fit_promise": ("Le bon modèle pour votre poignet", "Diamètre, épaisseur et taille de bracelet expliqués ; comparaison des mouvements ; ajustement du bracelet."),
        "review_themes": {"bracelet": ["bracelet", "maillon", "boucle", "strap"], "pile": ["pile", "batterie", "autonomie"],
                          "etancheite": ["étanche", "etanche", "buée", "eau"], "garantie": ["garantie", "sav", "réparation", "warranty"],
                          "precision": ["retard", "avance", "précision", "s'arrête"]},
        "compliance": ["Piles et batteries : obligations de reprise (filière piles / DEEE pour les montres connectées)",
                       "Garantie constructeur : ne l'annoncer que si le fournisseur la transfère réellement (revendeur non agréé = souvent sans garantie constructeur)",
                       "Distribution sélective fréquente dans l'horlogerie : vérifier le droit de revendre du neuf"],
        "seo": ("Quartz ou automatique : comment choisir sa montre", "## Quartz\nPrécis, peu d'entretien, pile à changer.\n\n## Automatique\nRemontage par le mouvement du poignet, entretien périodique.\n"),
        "demo": {"brand": "Horlogerie Démo", "products": [
            ("Diver démo 200", "plongee", "homme", "Noir / acier", 349, 160, ["Unique"], {"movement": "automatique", "case_diameter_mm": "42", "water_resistance_m": "200", "strap_material": "acier", "glass": "saphir", "warranty_months": "24"}),
            ("Classic démo 36", "habillee", "femme", "Blanc / cuir", 189, 78, ["Unique"], {"movement": "quartz", "case_diameter_mm": "36", "water_resistance_m": "30", "strap_material": "cuir", "glass": "mineral", "warranty_months": "24"}),
            ("Field démo 40", "casual", "homme", "Vert / nylon", 149, 61, ["S", "M", "L"], {"movement": "quartz", "case_diameter_mm": "40", "water_resistance_m": "100", "strap_material": "nylon", "glass": "mineral", "warranty_months": "24"}),
            ("Smart démo One", "connectee", "mixte", "Graphite", 229, 120, ["S", "L"], {"movement": "connecte", "case_diameter_mm": "44", "water_resistance_m": "50", "strap_material": "silicone", "warranty_months": "24"})],
            "guide": [{"size": "S", "eq": "155–175 mm", "measure": 175}, {"size": "M", "eq": "170–195 mm", "measure": 195}, {"size": "L", "eq": "190–215 mm", "measure": 215}]},
        "candidates": [
            ("Ocarat", "https://www.ocarat.com/", "Spécialiste montres et bijoux en ligne (France)"),
            ("Histoire d'Or", "https://www.histoiredor.com/", "Bijouterie-horlogerie, réseau de magasins (France)"),
            ("Marc Orian", "https://www.marc-orian.fr/", "Bijouterie-horlogerie (France)"),
            ("Maty", "https://www.maty.com/", "Bijoux et montres en ligne (France)"),
            ("Bijourama", "https://www.bijourama.com/", "Montres et bijoux en ligne (France)"),
            ("Chrono24", "https://www.chrono24.fr/", "Place de marché horlogère (neuf et occasion)"),
            ("Zalando", "https://www.zalando.be/", "Généraliste mode, rayon montres, FR/BE"),
            ("Galeries Lafayette", "https://www.galerieslafayette.com/", "Grand magasin, rayon horlogerie"),
            ("Inno", "https://www.inno.be/", "Grand magasin belge"),
            ("Montre de marque en direct (ex. Seiko, Casio)", "https://www.seikowatches.com/fr-fr", "Vente directe de la marque : référence de prix et de garantie")],
    },
    "jewelry": {
        "label": "Bijoux", "keywords": ["bijou", "bague", "collier", "boucle d'oreille", "boucles d'oreilles", "pendentif", "joaillerie"],
        "axes": {"size": "Taille", "color": "Métal / finition"}, "size_prefix": "",
        "usage_label": "Type", "usages": ["bague", "collier", "bracelet", "boucles", "pendentif"],
        "attributes": [("metal", "Métal", ["or-18k", "or-9k", "argent-925", "acier", "plaque-or", "laiton"]), ("stone", "Pierre", None),
                       ("weight_g", "Poids (g)", None), ("hallmark", "Poinçon", None), ("nickel_free", "Sans nickel", ["oui", "non"])],
        "required_attributes": ["metal"], "recommended_attributes": ["hallmark", "weight_g"],
        "filters": ["usage", "attr:metal", "attr:stone", "size", "price", "brand", "in_stock"],
        "collections": [("bagues", "Bagues", "usage", "bague"), ("colliers", "Colliers", "usage", "collier"), ("bracelets", "Bracelets", "usage", "bracelet"),
                        ("boucles", "Boucles d'oreilles", "usage", "boucles"), ("or", "Or", "attr:metal", "or-18k"), ("argent", "Argent", "attr:metal", "argent-925")],
        "fit": {"label": "Taille de bague", "measure_label": "Tour de doigt (mm)", "unit": "mm", "range": (40, 75), "allowance": 0,
                "allowance_note": "Mesurez en fin de journée ; un doigt est plus fin le matin et par temps froid.", "eq_label": "Diamètre"},
        "price_bands": {"value": (15, 60), "premium_accessible": (60, 250), "premium": (250, 1200), "luxury": (1200, 15000)},
        "names": ["Éclat Juste", "Atelier Carat", "Maison Fil d'Or", "Le Joli Métal"],
        "fit_promise": ("La bonne taille, le bon métal", "Baguier en ligne, métal et poinçon affichés, gravure et ajustement expliqués."),
        "review_themes": {"taille": ["taille", "trop petit", "trop grand", "serré"], "noircit": ["noirci", "terni", "verdi", "allergie", "nickel"]},
        "compliance": ["Métaux précieux : poinçon de titre et de responsabilité obligatoires en France pour l'or et l'argent au-delà des seuils de poids",
                       "Nickel : limite de migration (REACH, annexe XVII)", "Dénomination exacte du métal (plaqué ≠ massif)"],
        "seo": ("Comment connaître sa taille de bague", "## Mesurer\nEnroulez un fil autour du doigt, marquez, mesurez la longueur en mm.\n"),
        "demo": {"brand": "Atelier Démo", "products": [
            ("Bague démo Fil", "bague", "femme", "Argent", 59, 18, ["50", "52", "54", "56", "58"], {"metal": "argent-925", "hallmark": "925", "nickel_free": "oui"}),
            ("Collier démo Goutte", "collier", "femme", "Doré", 89, 27, ["Unique"], {"metal": "plaque-or", "nickel_free": "oui"}),
            ("Jonc démo Ligne", "bracelet", "mixte", "Acier", 45, 12, ["S", "M"], {"metal": "acier", "nickel_free": "oui"})],
            "guide": [{"size": str(s), "eq": f"Ø {s / 3.1416:.1f} mm", "measure": s} for s in range(46, 66)]},
        "candidates": [
            ("Histoire d'Or", "https://www.histoiredor.com/", "Bijouterie, réseau de magasins (France)"),
            ("Maty", "https://www.maty.com/", "Bijoux en ligne (France)"),
            ("Ocarat", "https://www.ocarat.com/", "Bijoux et montres en ligne"),
            ("Marc Orian", "https://www.marc-orian.fr/", "Bijouterie (France)"),
            ("Bijourama", "https://www.bijourama.com/", "Bijoux en ligne"),
            ("Agatha", "https://www.agatha.fr/", "Marque de bijoux fantaisie (vente directe)"),
            ("Zalando", "https://www.zalando.be/", "Généraliste mode, rayon bijoux"),
            ("Inno", "https://www.inno.be/", "Grand magasin belge")],
    },
    "accessories": {
        "label": "Accessoires", "keywords": ["accessoire", "sac", "maroquinerie", "ceinture", "portefeuille", "lunettes", "casquette", "echarpe", "bonnet", "porte-cartes"],
        "axes": {"size": "Taille", "color": "Couleur"}, "size_prefix": "",
        "usage_label": "Type", "usages": ["sac", "ceinture", "portefeuille", "lunettes", "chapeau", "echarpe"],
        "attributes": [("material", "Matière", ["cuir", "cuir-vegan", "toile", "nylon", "metal", "acetate"]), ("dimensions", "Dimensions (cm)", None),
                       ("capacity_l", "Contenance (L)", None), ("uv_category", "Catégorie UV (lunettes)", ["0", "1", "2", "3", "4"]),
                       ("origin_country", "Pays de fabrication", None)],
        "required_attributes": [], "recommended_attributes": ["material", "dimensions"],
        "filters": ["usage", "attr:material", "color", "gender", "price", "brand", "in_stock"],
        "collections": [("sacs", "Sacs", "usage", "sac"), ("petite-maroquinerie", "Petite maroquinerie", "usage", "portefeuille"),
                        ("ceintures", "Ceintures", "usage", "ceinture"), ("lunettes", "Lunettes", "usage", "lunettes"),
                        ("femme", "Femme", "gender", "femme"), ("homme", "Homme", "gender", "homme")],
        "fit": {"label": "Taille de ceinture", "measure_label": "Tour de taille (cm)", "unit": "cm", "range": (50, 160), "allowance": 0,
                "allowance_note": "Mesurez à l'endroit où vous portez la ceinture ; la taille se lit au trou central.", "eq_label": "Longueur"},
        "price_bands": {"value": (10, 40), "premium_accessible": (40, 160), "premium": (150, 500), "luxury": (500, 5000)},
        "names": ["Maison Sangle", "Le Bon Fermoir", "Atelier Poche", "Comptoir Cuir"],
        "fit_promise": ("Les dimensions réelles, sans surprise", "Dimensions, contenance et matière vérifiées, photos à l'échelle, ce qui rentre dedans."),
        "review_themes": {"dimensions": ["plus petit que", "dimension", "taille", "ne rentre pas"], "matiere": ["cuir", "simili", "odeur", "décousu", "fermeture"]},
        "compliance": ["Composition et matière exactes (« cuir » réservé au cuir véritable)", "Lunettes de soleil : marquage CE et catégorie de filtre obligatoires"],
        "seo": ("Comment choisir la taille de son sac", "## Ce qui doit y entrer\nOrdinateur 13 ou 15 pouces, bouteille, documents A4 : vérifiez les dimensions intérieures.\n"),
        "demo": {"brand": "Maroquinerie Démo", "products": [
            ("Cabas démo Ville", "sac", "femme", "Camel", 129, 48, ["Unique"], {"material": "cuir", "dimensions": "38 × 30 × 14", "capacity_l": "14"}),
            ("Porte-cartes démo Slim", "portefeuille", "mixte", "Noir", 35, 9, ["Unique"], {"material": "cuir", "dimensions": "10 × 7 × 0,5"}),
            ("Ceinture démo Boucle", "ceinture", "homme", "Marron", 49, 15, ["85", "90", "95", "100", "105"], {"material": "cuir"}),
            ("Solaires démo Sun", "lunettes", "mixte", "Écaille", 79, 22, ["Unique"], {"material": "acetate", "uv_category": "3"})],
            "guide": [{"size": str(s), "eq": f"{s + 15} cm au total", "measure": s} for s in range(75, 125, 5)]},
        "candidates": [
            ("Zalando", "https://www.zalando.fr/", "Généraliste mode, large rayon accessoires, FR/BE"),
            ("ASOS", "https://www.asos.com/fr/", "Mode en ligne, accessoires"),
            ("Galeries Lafayette", "https://www.galerieslafayette.com/", "Grand magasin, maroquinerie"),
            ("Printemps", "https://www.printemps.com/", "Grand magasin, maroquinerie"),
            ("Inno", "https://www.inno.be/", "Grand magasin belge"),
            ("La Redoute", "https://www.laredoute.fr/", "Généraliste en ligne (France/Belgique)"),
            ("Sarenza", "https://www.sarenza.com/", "Chaussures et maroquinerie en ligne"),
            ("Spartoo", "https://www.spartoo.com/", "Chaussures, sacs et accessoires en ligne")],
    },
    "apparel": {
        "label": "Vêtements", "keywords": ["vetement", "t-shirt", "sweat", "hoodie", "robe", "jean", "veste", "pull", "pantalon", "chemise", "pret-a-porter"],
        "axes": {"size": "Taille", "color": "Couleur"}, "size_prefix": "",
        "usage_label": "Type", "usages": ["t-shirt", "sweat", "pantalon", "veste", "robe", "chemise"],
        "attributes": [("composition", "Composition", None), ("fit", "Coupe", ["slim", "regular", "oversize"]), ("care", "Entretien", None),
                       ("origin_country", "Pays de fabrication", None)],
        "required_attributes": ["composition"], "recommended_attributes": ["fit", "care"],
        "filters": ["usage", "size", "color", "attr:fit", "gender", "price", "brand", "in_stock"],
        "collections": [("femme", "Femme", "gender", "femme"), ("homme", "Homme", "gender", "homme"), ("hauts", "Hauts", "usage", "t-shirt"),
                        ("sweats", "Sweats", "usage", "sweat"), ("pantalons", "Pantalons", "usage", "pantalon"), ("vestes", "Vestes", "usage", "veste")],
        "fit": {"label": "Guide des tailles", "measure_label": "Tour de poitrine (cm)", "unit": "cm", "range": (60, 160), "allowance": 0,
                "allowance_note": "Mesure prise sous les bras, ruban horizontal. La coupe (slim, oversize) modifie le tombé.", "eq_label": "Équivalence"},
        "price_bands": {"value": (10, 40), "premium_accessible": (40, 120), "premium": (100, 350), "luxury": (350, 3000)},
        "names": ["Maille Claire", "Atelier Ligne", "Bon Tombé", "Coupe Juste"],
        "fit_promise": ("La bonne taille du premier coup", "Mesures réelles de chaque pièce, coupe expliquée, échange de taille simple."),
        "review_themes": {"taille": ["taille", "taille petit", "taille grand", "trop petit", "trop grand", "maat"], "qualite": ["bouloche", "rétréci", "délavé", "transparent"]},
        "compliance": ["Composition des fibres obligatoire (règlement UE 1007/2011)", "France : informations environnementales (loi AGEC) selon le chiffre d'affaires"],
        "seo": ("Comment trouver sa taille en ligne", "## Prendre ses mesures\nTour de poitrine, de taille et de hanches, ruban bien horizontal.\n"),
        "demo": {"brand": "Atelier Démo", "products": [
            ("Tee démo Essentiel", "t-shirt", "mixte", "Écru", 35, 9, ["XS", "S", "M", "L", "XL"], {"composition": "100 % coton biologique", "fit": "regular"}),
            ("Sweat démo Doux", "sweat", "mixte", "Gris chiné", 69, 22, ["S", "M", "L", "XL"], {"composition": "80 % coton, 20 % polyester", "fit": "oversize"}),
            ("Veste démo Toile", "veste", "homme", "Kaki", 119, 41, ["S", "M", "L"], {"composition": "100 % coton", "fit": "regular"})],
            "guide": [{"size": s, "eq": e, "measure": m} for s, e, m in [("XS", "34", 84), ("S", "36-38", 90), ("M", "40", 96), ("L", "42-44", 104), ("XL", "46", 112)]]},
        "candidates": [
            ("Zalando", "https://www.zalando.fr/", "Généraliste mode en ligne, FR/BE"),
            ("ASOS", "https://www.asos.com/fr/", "Mode en ligne"),
            ("Kiabi", "https://www.kiabi.com/", "Mode à petits prix (France/Belgique)"),
            ("JBC", "https://www.jbc.be/", "Enseigne mode belge"),
            ("La Redoute", "https://www.laredoute.fr/", "Généraliste en ligne"),
            ("Galeries Lafayette", "https://www.galerieslafayette.com/", "Grand magasin"),
            ("Inno", "https://www.inno.be/", "Grand magasin belge")],
    },
    "beauty": {
        "label": "Beauté & cosmétiques", "keywords": ["cosmetique", "maquillage", "soin", "parfum", "skincare", "serum", "creme"],
        "axes": {"size": "Contenance", "color": "Teinte"}, "size_prefix": "",
        "usage_label": "Type", "usages": ["visage", "corps", "cheveux", "maquillage", "parfum"],
        "attributes": [("ingredients", "Ingrédients (INCI)", None), ("skin_type", "Type de peau", ["toutes", "seche", "grasse", "mixte", "sensible"]),
                       ("pao_months", "Durée après ouverture (mois)", None), ("vegan", "Vegan", ["oui", "non"]), ("responsible_person", "Personne responsable UE", None)],
        "required_attributes": ["ingredients"], "recommended_attributes": ["pao_months", "responsible_person"],
        "filters": ["usage", "attr:skin_type", "attr:vegan", "color", "price", "brand", "in_stock"],
        "collections": [("visage", "Visage", "usage", "visage"), ("corps", "Corps", "usage", "corps"), ("cheveux", "Cheveux", "usage", "cheveux"),
                        ("maquillage", "Maquillage", "usage", "maquillage"), ("vegan", "Vegan", "attr:vegan", "oui")],
        "fit": None,
        "price_bands": {"value": (5, 20), "premium_accessible": (20, 60), "premium": (50, 150), "luxury": (150, 600)},
        "names": ["Peau Claire", "Atelier Rituel", "Le Bon Soin", "Formule Juste"],
        "fit_promise": ("Le bon produit pour votre peau", "Liste INCI complète, type de peau indiqué, conseils d'utilisation, sans promesse miracle."),
        "review_themes": {"reaction": ["irritation", "allergie", "boutons", "rougeur", "brûle"], "efficacite": ["aucun effet", "efficace", "résultat"],
                          "date": ["périmé", "date", "abîmé", "fuit"]},
        "compliance": ["Règlement (CE) 1223/2009 : personne responsable dans l'UE, notification CPNP, liste INCI",
                       "Aucune allégation thérapeutique ; allégations conformes au règlement (UE) 655/2013"],
        "seo": ("Lire une liste INCI", "## Ordre des ingrédients\nLes ingrédients sont listés par concentration décroissante (au-delà de 1 %).\n"),
        "demo": {"brand": "Laboratoire Démo", "products": [
            ("Sérum démo Éclat", "visage", "mixte", "", 32, 9, ["30 ml"], {"ingredients": "Aqua, Glycerin, Niacinamide (liste fictive)", "skin_type": "toutes", "pao_months": "6", "vegan": "oui"}),
            ("Crème démo Riche", "visage", "mixte", "", 28, 7, ["50 ml"], {"ingredients": "Aqua, Butyrospermum Parkii Butter (liste fictive)", "skin_type": "seche", "pao_months": "12", "vegan": "non"})],
            "guide": []},
        "candidates": [
            ("Sephora", "https://www.sephora.fr/", "Spécialiste beauté (France)"),
            ("Nocibé", "https://www.nocibe.fr/", "Parfumerie (France)"),
            ("Marionnaud", "https://www.marionnaud.fr/", "Parfumerie (France)"),
            ("Ici Paris XL", "https://www.iciparisxl.be/", "Parfumerie (Belgique)"),
            ("Kruidvat", "https://www.kruidvat.be/", "Droguerie-beauté (Belgique)"),
            ("Douglas", "https://www.douglas.be/", "Parfumerie (Belgique)")],
    },
    "electronics": {
        "label": "Électronique & high-tech", "keywords": ["ecouteur", "casque audio", "smartphone", "electronique", "gaming", "chargeur", "enceinte", "high-tech"],
        "axes": {"size": "Capacité / modèle", "color": "Couleur"}, "size_prefix": "",
        "usage_label": "Type", "usages": ["audio", "telephonie", "gaming", "accessoire", "maison-connectee"],
        "attributes": [("compatibility", "Compatibilité", None), ("battery_h", "Autonomie (h)", None), ("connectivity", "Connectivité", None),
                       ("warranty_months", "Garantie (mois)", None), ("ce_marking", "Marquage CE", ["oui", "non"]), ("repairability", "Indice de réparabilité", None)],
        "required_attributes": ["ce_marking"], "recommended_attributes": ["warranty_months", "compatibility"],
        "filters": ["usage", "attr:connectivity", "color", "price", "brand", "in_stock"],
        "collections": [("audio", "Audio", "usage", "audio"), ("telephonie", "Téléphonie", "usage", "telephonie"),
                        ("gaming", "Gaming", "usage", "gaming"), ("accessoires", "Accessoires", "usage", "accessoire")],
        "fit": None,
        "price_bands": {"value": (10, 50), "premium_accessible": (50, 250), "premium": (200, 800), "luxury": (800, 4000)},
        "names": ["Branché Juste", "Le Bon Câble", "Atelier Signal", "Comptoir Tech"],
        "fit_promise": ("Compatible, sans mauvaise surprise", "Compatibilité vérifiée, fiches techniques complètes, garantie et SAV expliqués."),
        "review_themes": {"panne": ["panne", "ne marche plus", "défectueux", "ne s'allume", "batterie"], "compatibilite": ["compatible", "incompatible", "ne fonctionne pas avec"],
                          "garantie": ["garantie", "sav", "retour"]},
        "compliance": ["Marquage CE, DEEE (éco-participation affichée en France)", "France : indice de réparabilité/durabilité pour les catégories concernées",
                       "Garantie légale de conformité de 2 ans à rappeler"],
        "seo": ("Bien choisir ses écouteurs sans fil", "## Autonomie, réduction de bruit, compatibilité\nVérifiez le codec et la compatibilité avec votre téléphone.\n"),
        "demo": {"brand": "Tech Démo", "products": [
            ("Écouteurs démo Air", "audio", "mixte", "Blanc", 79, 31, ["Unique"], {"compatibility": "Bluetooth 5.3 (iOS, Android)", "battery_h": "6", "connectivity": "bluetooth", "warranty_months": "24", "ce_marking": "oui"}),
            ("Chargeur démo 65W", "accessoire", "mixte", "Noir", 39, 13, ["Unique"], {"compatibility": "USB-C PD", "connectivity": "usb-c", "warranty_months": "24", "ce_marking": "oui"})],
            "guide": []},
        "candidates": [
            ("Fnac", "https://www.fnac.com/", "Distributeur culture et high-tech (France/Belgique)"),
            ("Darty", "https://www.darty.com/", "Électroménager et high-tech (France)"),
            ("Boulanger", "https://www.boulanger.com/", "Électroménager et high-tech (France)"),
            ("Coolblue", "https://www.coolblue.be/", "E-commerçant high-tech (Belgique)"),
            ("MediaMarkt", "https://www.mediamarkt.be/", "Distributeur high-tech (Belgique)"),
            ("Vanden Borre", "https://www.vandenborre.be/", "Distributeur électroménager/high-tech (Belgique)"),
            ("Krëfel", "https://www.krefel.be/", "Distributeur électroménager/high-tech (Belgique)")],
    },
    "home": {
        "label": "Maison & déco", "keywords": ["deco", "mobilier", "maison", "bougie", "vaisselle", "luminaire", "linge de maison"],
        "axes": {"size": "Dimensions", "color": "Couleur / finition"}, "size_prefix": "",
        "usage_label": "Pièce", "usages": ["salon", "chambre", "cuisine", "salle-de-bain", "exterieur"],
        "attributes": [("material", "Matière", None), ("dimensions", "Dimensions (cm)", None), ("weight_kg", "Poids (kg)", None), ("care", "Entretien", None)],
        "required_attributes": [], "recommended_attributes": ["material", "dimensions"],
        "filters": ["usage", "attr:material", "color", "price", "brand", "in_stock"],
        "collections": [("salon", "Salon", "usage", "salon"), ("chambre", "Chambre", "usage", "chambre"), ("cuisine", "Cuisine", "usage", "cuisine")],
        "fit": None,
        "price_bands": {"value": (5, 40), "premium_accessible": (40, 200), "premium": (150, 800), "luxury": (800, 8000)},
        "names": ["Maison Claire", "L'Atelier du Logis", "Comptoir Intérieur"],
        "fit_promise": ("Ce que vous voyez, à la bonne échelle", "Dimensions réelles, photos en situation, délais et livraison des pièces volumineuses annoncés."),
        "review_themes": {"casse": ["cassé", "fêlé", "abîmé", "rayé"], "dimensions": ["plus petit", "dimension", "taille"]},
        "compliance": ["Bougies : avertissements de sécurité", "Contact alimentaire : conformité des matériaux (règlement 1935/2004)"],
        "seo": ("Choisir la bonne taille de tapis", "## Mesurer la pièce\nLaissez 20 à 40 cm entre le tapis et les murs.\n"),
        "demo": {"brand": "Maison Démo", "products": [
            ("Vase démo Grès", "salon", "mixte", "Sable", 39, 12, ["Petit", "Grand"], {"material": "grès émaillé", "dimensions": "15 × 25"}),
            ("Plaid démo Laine", "chambre", "mixte", "Ocre", 69, 24, ["130 × 170"], {"material": "laine", "care": "lavage à froid"})],
            "guide": []},
        "candidates": [
            ("Maisons du Monde", "https://www.maisonsdumonde.com/", "Mobilier et décoration (France/Belgique)"),
            ("La Redoute Intérieurs", "https://www.laredoute.fr/", "Généraliste en ligne, maison"),
            ("Westwing", "https://www.westwing.fr/", "Décoration en ligne"),
            ("Fleux", "https://www.fleux.com/", "Décoration (France)"),
            ("Inno", "https://www.inno.be/", "Grand magasin belge")],
    },
}

GENERIC = {
    "label": "Produits", "keywords": [], "axes": {"size": "Taille / format", "color": "Couleur / variante"}, "size_prefix": "",
    "usage_label": "Type", "usages": [], "attributes": [("material", "Matière", None), ("dimensions", "Dimensions", None)],
    "required_attributes": [], "recommended_attributes": [], "filters": ["usage", "color", "price", "brand", "in_stock"],
    "collections": [("tout", "Tous les produits", "all", "")], "fit": None, "price_bands": PRICE_BANDS_DEFAULT,
    "names": ["Maison Nova", "Comptoir Clair", "L'Atelier Juste"],
    "fit_promise": ("Le bon choix, expliqué", "Fiches complètes, conseils honnêtes et réponses rapides."),
    "review_themes": {}, "compliance": [], "seo": ("Bien choisir", "## Nos conseils\n[À COMPLÉTER]\n"),
    "demo": {"brand": "Marque Démo", "products": [("Produit démo A", "", "mixte", "Bleu", 49, 20, ["Unique"], {}),
                                                  ("Produit démo B", "", "mixte", "Vert", 79, 31, ["Unique"], {})], "guide": []},
    "candidates": [],
}

ALIASES = {"chaussures": "footwear", "shoes": "footwear", "montres": "watches", "watch": "watches", "bijoux": "jewelry",
           "accessoires": "accessories", "vetements": "apparel", "clothing": "apparel", "beaute": "beauty", "cosmetiques": "beauty",
           "high-tech": "electronics", "maison": "home"}


def get(key: str | None) -> dict:
    key = ALIASES.get(_norm(key or ""), key)
    return {**GENERIC, **PROFILES[key], "key": key} if key in PROFILES else {**GENERIC, "key": key or "generic"}


def detect(text: str) -> list[str]:
    """Verticales mentionnées, par ordre d'importance (nombre de mots-clés trouvés)."""
    t = _norm(text)
    scores = []
    for key, p in PROFILES.items():
        n = sum(1 for w in p["keywords"] if re.search(rf"\b{re.escape(_norm(w))}(s|x)?\b", t))
        if n:
            scores.append((n, key))
    return [k for _, k in sorted(scores, key=lambda x: -x[0])]


def for_product(product) -> dict:
    """Profil d'un produit (catégorie produit ; à défaut, détection sur le titre)."""
    if product.category and (product.category in PROFILES or _norm(product.category) in ALIASES):
        return get(product.category)
    found = detect(f"{product.category} {product.title}")
    return get(found[0]) if found else get(None)


def attribute_label(profile: dict, key: str) -> str:
    return next((lbl for k, lbl, _ in profile["attributes"] if k == key), key)
