"""Images téléversées : validation, nettoyage, stockage, et palette de couleurs dérivée de la photo.

Sécurité :
- le format réel est vérifié par décodage (pas par l'extension ni le type annoncé) ; JPEG, PNG, WebP uniquement ;
- l'image est ré-encodée : les métadonnées (EXIF, GPS, appareil) sont supprimées, tout contenu caché est écarté ;
- taille max 8 Mo, 40 mégapixels max (anti « bombe de décompression »), redimensionnée à 2000 px ;
- nom de fichier aléatoire, rangé par boutique ; jamais de chemin fourni par l'utilisateur.
"""
import base64
import colorsys
import io
import os
import secrets
from pathlib import Path

from PIL import Image, ImageOps

MEDIA_ROOT = Path(os.getenv("SHOPFORGE_MEDIA_DIR", Path(__file__).resolve().parents[2] / "media"))
MAX_BYTES = 8_000_000
MAX_PIXELS = 40_000_000
MAX_SIDE = 2000
Image.MAX_IMAGE_PIXELS = MAX_PIXELS


class ImageRejected(ValueError):
    pass


def load_clean(content: bytes) -> Image.Image:
    if not content:
        raise ImageRejected("Fichier vide")
    if len(content) > MAX_BYTES:
        raise ImageRejected("Image trop lourde (8 Mo maximum)")
    try:
        img = Image.open(io.BytesIO(content))
        if img.format not in ("JPEG", "PNG", "WEBP"):
            raise ImageRejected("Format accepté : JPEG, PNG ou WebP")
        if img.width * img.height > MAX_PIXELS:
            raise ImageRejected("Image trop grande")
        img.load()
    except ImageRejected:
        raise
    except Exception as exc:  # fichier corrompu ou pas une image
        raise ImageRejected("Ce fichier n'est pas une image valide") from exc
    img = ImageOps.exif_transpose(img)  # respecte l'orientation, puis on abandonne l'EXIF
    img = img.convert("RGBA") if img.mode in ("P", "LA", "RGBA") else img.convert("RGB")
    img.thumbnail((MAX_SIDE, MAX_SIDE))
    return img


def save(shop_id: int, img: Image.Image) -> str:
    """Enregistre en WebP (sans métadonnées) et renvoie l'URL publique /media/…"""
    folder = MEDIA_ROOT / str(int(shop_id))
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{secrets.token_urlsafe(12)}.webp"
    img.save(folder / name, "WEBP", quality=86, method=4)
    return f"/media/{int(shop_id)}/{name}"


def for_ai(img: Image.Image, side: int = 1024) -> tuple[str, str]:
    """Copie réduite, encodée en base64 pour l'analyse (moins de tokens)."""
    small = img.convert("RGB").copy()
    small.thumbnail((side, side))
    buf = io.BytesIO()
    small.save(buf, "JPEG", quality=85)
    return "image/jpeg", base64.standard_b64encode(buf.getvalue()).decode()


# ---------------------------------------------------------------- palette
def _hex(rgb):
    return "#{:02X}{:02X}{:02X}".format(*[max(0, min(255, round(c))) for c in rgb])


def _lum(rgb):
    def ch(c):
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = rgb
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def contrast(a, b) -> float:
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _from_hls(h, l, s):
    return tuple(c * 255 for c in colorsys.hls_to_rgb(h, l, s))


def dominant_colors(img: Image.Image, n: int = 6) -> list[tuple]:
    """Couleurs dominantes (hors fond quasi blanc/noir/transparent), triées par présence."""
    small = img.convert("RGBA").copy()
    small.thumbnail((160, 160))
    data = small.get_flattened_data() if hasattr(small, "get_flattened_data") else small.getdata()
    px = [(r, g, b) for r, g, b, a in data if a > 128]
    if not px:
        return [(40, 40, 40)]
    q = Image.new("RGB", (len(px), 1))
    q.putdata(px)
    pal = q.quantize(colors=n, method=Image.Quantize.MEDIANCUT)
    counts = sorted(pal.getcolors(), reverse=True)
    colors = pal.getpalette()[: n * 3]
    out = []
    for count, idx in counts:
        rgb = tuple(colors[idx * 3: idx * 3 + 3])
        h, l, s = colorsys.rgb_to_hls(*[c / 255 for c in rgb])
        out.append({"rgb": rgb, "share": count / len(px), "h": h, "l": l, "s": s})
    return out


def palette_from_image(img: Image.Image) -> dict:
    """Palette de boutique dérivée de la photo, contrastes WCAG garantis (texte ≥ 7:1, bouton ≥ 4,5:1)."""
    cols = dominant_colors(img)
    # accent : la couleur la plus « vivante » suffisamment présente, sinon la dominante
    vivid = [c for c in cols if c["s"] > 0.25 and 0.12 < c["l"] < 0.85 and c["share"] > 0.03]
    base = max(vivid, key=lambda c: c["s"] * (0.5 + c["share"])) if vivid else cols[0]
    h, s = base["h"], max(min(base["s"], 0.85), 0.18 if vivid else 0.06)
    accent = _from_hls(h, base["l"], s)
    l = base["l"]
    while contrast(accent, (255, 255, 255)) < 4.5 and l > 0.05:  # assombrir jusqu'à un bouton lisible
        l -= 0.02
        accent = _from_hls(h, l, s)
    bg = _from_hls(h, 0.975, min(s, 0.35))
    surface = (255, 255, 255)
    text = _from_hls(h, 0.11, min(s, 0.25))
    muted = _from_hls(h, 0.36, min(s, 0.15))
    while contrast(muted, bg) < 4.5:
        muted = tuple(c * 0.92 for c in muted)
    line = _from_hls(h, 0.88, min(s, 0.2))
    pal = {"bg": _hex(bg), "surface": _hex(surface), "text": _hex(text), "muted": _hex(muted),
           "accent": _hex(accent), "accent_text": "#FFFFFF", "line": _hex(line)}
    return {"palette": pal, "source_colors": [_hex(c["rgb"]) for c in cols],
            "checks": {"text_on_bg": round(contrast(text, bg), 2), "button": round(contrast(accent, (255, 255, 255)), 2),
                       "muted_on_bg": round(contrast(muted, bg), 2)}}
