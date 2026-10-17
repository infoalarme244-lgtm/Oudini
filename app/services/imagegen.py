"""Visuels générés par IA à partir de la photo du commerçant (mises en situation, gros plans, ambiances).

Principes :
- la référence est TOUJOURS une photo du commerçant avec droit d'utilisation (jamais une image d'un autre site) ;
- consigne stricte : produit identique (forme, couleur, logo, proportions), aucun texte, prix, note, badge ou logo ajouté,
  aucune personne réelle ou célèbre, rien d'intime ;
- chaque visuel est soumis à la validation du commerçant avant d'apparaître sur la boutique ;
- les visuels publiés portent la mention « Visuel généré par IA » (transparence) ;
- coût suivi dans le budget IA de la boutique ; génération en tâche de fond.

Fournisseurs (au choix, via IMAGE_PROVIDER) : « openai » (API Images, édition à partir d'une image de référence) ou
« gemini » (Gemini, image en entrée et en sortie). Les noms de modèles sont configurables.
"""
import base64
import io
import logging
import os
from datetime import datetime, timezone

import httpx

from . import media, verticals
from .audit import trace
from .jobs import handler

log = logging.getLogger("shopforge.imagegen")

PROVIDER = os.getenv("IMAGE_PROVIDER", "")  # openai | gemini | (vide = désactivé)
OPENAI_MODEL = os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-1")
GEMINI_MODEL = os.getenv("GEMINI_IMAGE_MODEL", "gemini-2.5-flash-image")
COST_PER_IMAGE_USD = float(os.getenv("IMAGE_COST_USD", "0.05"))
MAX_PER_RUN = 4
TRANSPORT = None  # remplaçable en test

RULES = ("Use the provided photo as the exact product reference. Keep the product strictly identical: same shape, colours, "
         "materials, proportions, buttons and logo. Do not add any text, letters, price, rating, stars, badge, watermark or "
         "extra logo. Do not depict any real or famous person; any person must be anonymous and shown tastefully, fully "
         "clothed, preferably only hands or partial silhouette. No nudity, no intimate areas. Photorealistic, natural light, "
         "high-end e-commerce photography, square format.")

SCENES = {
    "_default": [("studio", "Studio", "Clean studio packshot on a soft neutral background with a subtle shadow."),
                 ("lifestyle", "Mise en situation", "The product in a tasteful, realistic everyday setting that matches its use."),
                 ("hands", "En main", "A close shot of anonymous hands holding or using the product naturally."),
                 ("detail", "Détail", "A macro close-up highlighting the product's materials and finish.")],
    "footwear": [("studio", "Studio", "Side view packshot of the shoe on a light seamless background."),
                 ("street", "En ville", "The shoes worn on a city sidewalk, framed from the knees down, no face."),
                 ("detail", "Détail", "Macro close-up of the sole, stitching and materials."),
                 ("flatlay", "Flat lay", "Top-down flat lay of the shoe with a few minimal accessories.")],
    "watches": [("studio", "Studio", "Elegant packshot of the watch on a neutral stone surface."),
                ("wrist", "Au poignet", "The watch worn on an anonymous wrist, shirt cuff visible, no face."),
                ("detail", "Détail", "Macro close-up on the dial and case finish."),
                ("lifestyle", "Ambiance", "The watch resting on a wooden desk with a notebook and coffee, warm light.")],
    "jewelry": [("studio", "Studio", "Jewelry packshot on a soft beige background with gentle reflections."),
                ("worn", "Porté", "The piece worn by an anonymous person, cropped to neck/hand, no face."),
                ("detail", "Détail", "Macro close-up showing the metal finish."),
                ("gift", "Cadeau", "The piece in an open minimal gift box on linen.")],
    "beauty": [("studio", "Studio", "Cosmetic packshot on a pastel background with soft shadows."),
               ("bathroom", "Salle de bain", "The product on a marble bathroom shelf with plants, natural light."),
               ("texture", "Texture", "Close-up of the product with a swatch of its texture beside it."),
               ("hands", "En main", "Anonymous hands holding the product.")],
}


class ImageGenError(Exception):
    pass


def enabled() -> bool:
    return PROVIDER == "openai" and bool(os.getenv("OPENAI_API_KEY")) or PROVIDER == "gemini" and bool(os.getenv("GEMINI_API_KEY"))


def scenes_for(product) -> list[tuple]:
    key = verticals.for_product(product)["key"]
    return SCENES.get(key, SCENES["_default"])


def reference_image(product) -> dict | None:
    """Photo du commerçant avec droit d'utilisation, jamais un visuel IA ni une image tierce."""
    return next((i for i in product.images or [] if i.get("license") and not i.get("ai") and i.get("url", "").startswith("/media/")), None)


def _read_media(url: str) -> bytes:
    path = media.MEDIA_ROOT / url.split("/media/", 1)[1]
    if media.MEDIA_ROOT.resolve() not in path.resolve().parents:
        raise ImageGenError("Chemin d'image invalide")
    img = media.load_clean(path.read_bytes())
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "PNG")
    return buf.getvalue()


def _prompt(product, scene_prompt: str) -> str:
    return f"{scene_prompt} Product: {product.title}. {RULES}"


def _openai(ref_png: bytes, prompt: str) -> bytes:
    key = os.getenv("OPENAI_API_KEY", "")
    with httpx.Client(timeout=180, transport=TRANSPORT) as c:
        r = c.post("https://api.openai.com/v1/images/edits", headers={"Authorization": f"Bearer {key}"},
                   data={"model": OPENAI_MODEL, "prompt": prompt, "size": "1024x1024", "n": "1"},
                   files={"image[]": ("reference.png", ref_png, "image/png")})
    if r.status_code != 200:
        raise ImageGenError(f"OpenAI {r.status_code} : {r.text[:300]}")
    data = r.json().get("data") or []
    if not data or not data[0].get("b64_json"):
        raise ImageGenError("OpenAI : aucune image renvoyée")
    return base64.b64decode(data[0]["b64_json"])


def _gemini(ref_png: bytes, prompt: str) -> bytes:
    key = os.getenv("GEMINI_API_KEY", "")
    body = {"contents": [{"role": "user", "parts": [
        {"inline_data": {"mime_type": "image/png", "data": base64.b64encode(ref_png).decode()}}, {"text": prompt}]}],
        "generationConfig": {"responseModalities": ["IMAGE"]}}
    with httpx.Client(timeout=180, transport=TRANSPORT) as c:
        r = c.post(f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
                   headers={"x-goog-api-key": key}, json=body)
    if r.status_code != 200:
        raise ImageGenError(f"Gemini {r.status_code} : {r.text[:300]}")
    for cand in r.json().get("candidates", []):
        for part in (cand.get("content") or {}).get("parts", []):
            inline = part.get("inline_data") or part.get("inlineData")
            if inline and inline.get("data"):
                return base64.b64decode(inline["data"])
    raise ImageGenError("Gemini : aucune image renvoyée (refus ou filtre de sécurité possible)")


def generate(db, shop, product, scenes: list[str] | None = None) -> dict:
    """Génère jusqu'à 4 visuels « en attente de validation ». Renvoie {created, errors, skipped}."""
    if not enabled():
        return {"created": 0, "errors": [], "skipped": "Génération d'images non configurée sur la plateforme"}
    ref = reference_image(product)
    if not ref:
        return {"created": 0, "errors": [], "skipped": "Ajoutez d'abord une photo de votre produit (avec droit d'utilisation)"}
    existing = {i.get("scene") for i in product.images or [] if i.get("ai") and i.get("status") != "rejected"}
    todo = [s for s in scenes_for(product) if (scenes and s[0] in scenes) or (not scenes and s[0] not in existing)][:MAX_PER_RUN]
    ref_png = _read_media(ref["url"])
    created, errors = 0, []
    for key, label, scene_prompt in todo:
        if shop.ai_spent_usd + COST_PER_IMAGE_USD > shop.ai_budget_usd:
            errors.append("Budget IA de la boutique atteint")
            break
        prompt = _prompt(product, scene_prompt)
        try:
            raw = _openai(ref_png, prompt) if PROVIDER == "openai" else _gemini(ref_png, prompt)
            url = media.save(shop.id, media.load_clean(raw))  # ré-encodé, sans métadonnées
        except (ImageGenError, media.ImageRejected, httpx.HTTPError) as exc:
            errors.append(f"{label} : {exc}")
            continue
        shop.ai_spent_usd = (shop.ai_spent_usd or 0) + COST_PER_IMAGE_USD
        product.images = (product.images or []) + [{
            "url": url, "alt": f"{product.title} — {label.lower()}", "ai": True, "status": "pending", "scene": key,
            "license": f"visuel généré par IA ({PROVIDER}) à partir de la photo du commerçant",
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}]
        created += 1
    trace(db, shop.id, "image_generation", engine=f"{PROVIDER}:{OPENAI_MODEL if PROVIDER == 'openai' else GEMINI_MODEL}",
          input_summary=product.title, sources=[ref["url"]], assumptions=[{"consignes": RULES}],
          output={"created": created, "errors": errors}, cost_usd=created * COST_PER_IMAGE_USD)
    return {"created": created, "errors": errors, "skipped": ""}


@handler("image_generation")
def _job(db, job):
    from ..models import Product, Shop
    shop, product = db.get(Shop, job.shop_id), db.get(Product, job.payload["product_id"])
    if product is None or product.shop_id != shop.id:
        return {"created": 0, "errors": ["produit introuvable"]}
    return generate(db, shop, product, job.payload.get("scenes"))


def public_images(product) -> list[dict]:
    """Images affichables : photos avec droit d'utilisation + visuels IA validés par le commerçant."""
    return [i for i in product.images or [] if i.get("license") and (not i.get("ai") or i.get("status") == "approved")]
