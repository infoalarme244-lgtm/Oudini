"""Essai réel de génération de visuels avec VOTRE photo et VOTRE clé (IMAGE_PROVIDER=openai|gemini + OPENAI_API_KEY ou GEMINI_API_KEY).
Usage : python scripts/verify_images.py chemin\\vers\\photo.jpg [watches|footwear|jewelry|beauty|…]
Les visuels sont enregistrés dans le dossier ./visuels-test pour que vous les regardiez."""
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("SHOPFORGE_SECRET_KEY", "verify-images")
OUT = Path("visuels-test").resolve()
os.environ["SHOPFORGE_MEDIA_DIR"] = str(OUT / "_media")


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    from app.services import imagegen, media
    if not imagegen.enabled():
        sys.exit("Définissez IMAGE_PROVIDER=openai (avec OPENAI_API_KEY) ou IMAGE_PROVIDER=gemini (avec GEMINI_API_KEY).")
    from app import db as dbm
    dbm.configure("sqlite:///:memory:")
    dbm.init_db()
    from app.models import Product, Shop
    db = dbm.SessionLocal()
    shop = Shop(slug="verif-images", name="Essai", ai_budget_usd=1.0)
    db.add(shop)
    db.flush()
    url = media.save(shop.id, media.load_clean(Path(sys.argv[1]).read_bytes()))
    p = Product(shop_id=shop.id, handle="essai", title="Produit", category=sys.argv[2] if len(sys.argv) > 2 else "",
                images=[{"url": url, "alt": "photo", "license": "photo du commerçant"}])
    db.add(p)
    db.flush()
    res = imagegen.generate(db, shop, p)
    OUT.mkdir(exist_ok=True)
    for i in [i for i in p.images if i.get("ai")]:
        src = media.MEDIA_ROOT / i["url"].split("/media/")[1]
        shutil.copy(src, OUT / f"{i['scene']}.webp")
    print(f"✅ {res['created']} visuel(s) dans {OUT}" if res["created"] else "❌ aucun visuel")
    for e in res["errors"]:
        print("   ", e)
    print(f"   coût estimé : {shop.ai_spent_usd:.2f} $")


if __name__ == "__main__":
    main()
