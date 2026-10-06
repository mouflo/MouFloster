#!/usr/bin/env python3
"""
Application Flask : Interface manuelle de personnalisation de posters
Similaire à mouflanimexer - choix manuel avec checkboxes et inputs texte
"""

from flask import Flask, render_template_string, request, jsonify, send_file
import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps
from io import BytesIO
import os
import sys
import re
import json
from datetime import datetime
import base64
import uuid
import threading
import time
import numpy as np
import logging
from pathlib import Path

# Configuration locale: .env (réglages) puis data/secrets.env (clés, JAMAIS versionné, prime sur le reste)
BASE_DIR = Path(__file__).parent
_SECRETS_FILE = BASE_DIR / "data" / "secrets.env"


def _migrate_secrets():
    """Une seule fois: recopie les clés présentes dans l'ancien .env vers data/secrets.env"""
    old = BASE_DIR / ".env"
    if _SECRETS_FILE.exists() or not old.exists():
        return
    try:
        kept = []
        for line in old.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip().endswith(("_KEY", "_TOKEN", "_SECRET")) and value.strip().strip('"'):
                kept.append(f'{key.strip()}="{value.strip().strip(chr(34))}"')
        if kept:
            _SECRETS_FILE.parent.mkdir(parents=True, exist_ok=True)
            _SECRETS_FILE.write_text("\n".join(kept) + "\n", encoding="utf-8")
            os.chmod(_SECRETS_FILE, 0o600)
    except OSError:
        pass


_migrate_secrets()

try:
    from dotenv import load_dotenv
    for _f in (BASE_DIR / ".env", _SECRETS_FILE):
        if _f.exists():
            load_dotenv(_f, override=True)
except ImportError:
    pass  # python-dotenv non installé, utiliser les variables d'environnement système

import library
import mouflanga_link
import emby

app = Flask(__name__)

# Version: base manuelle + numéro de déploiement (nombre de commits) + hash court
BASE_VERSION = "0.3"


def get_version():
    try:
        import subprocess
        cwd = str(Path(__file__).parent)
        count = subprocess.check_output(["git", "rev-list", "--count", "HEAD"], cwd=cwd, text=True, stderr=subprocess.DEVNULL).strip()
        short = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=cwd, text=True, stderr=subprocess.DEVNULL).strip()
        return f"v{BASE_VERSION}.{count} ({short})"
    except Exception:
        return f"v{BASE_VERSION}"


APP_VERSION = get_version()
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB max

import auth
auth.init_app(app, APP_VERSION)  # page de connexion + protection de toutes les routes

# ============================================================================
# CONFIG
# ============================================================================

TMDB_API_KEY = os.getenv("TMDB_API_KEY", "TON_CLE_TMDB_ICI")
MEDIATHEQUE_DIR = os.getenv("MEDIATHEQUE_DIR", "/mnt/mouflosyno/Emby-Media")
OUTPUT_BASE = os.getenv("OUTPUT_DIR", "/mnt/mouflosyno/MouFloster")
try:
    os.makedirs(OUTPUT_BASE, exist_ok=True)
except OSError as _e:
    # Partage réseau pas monté / non inscriptible: on ne plante pas, on se rabat sur un dossier local et on le dit
    _fallback = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "posters")
    print(f"⚠️ Dossier de sortie {OUTPUT_BASE} inutilisable ({_e}): repli sur {_fallback}", file=sys.stderr)
    OUTPUT_BASE = _fallback
    os.makedirs(OUTPUT_BASE, exist_ok=True)

# Image dimensions (1000x1500 px)
IMG_WIDTH = 1000
IMG_HEIGHT = 1500

# Border sizes (will be configurable)
BORDER_ANIME = 45  # px (mesuré sur les exemples)
BORDER_STANDARD = 22  # px (mesuré sur les exemples)

# Logging
import diag
diag.setup_logging()  # journal détaillé: console + data/moufloster.log (lisible depuis la fenêtre « Journal »)
logger = logging.getLogger(__name__)

diag.init_app(
    app, APP_VERSION, OUTPUT_BASE, library.LIBRARY_ROOT,
    tmdb_ok_fn=lambda: bool(TMDB_API_KEY and TMDB_API_KEY != "TON_CLE_TMDB_ICI"),
    emby_ok_fn=emby.configured,
)

import emby_settings
emby_settings.init_app(app, BASE_DIR, emby.base_url)


def _set_tmdb_key(key):
    global TMDB_API_KEY
    TMDB_API_KEY = key


import tmdb_settings
tmdb_settings.init_app(app, BASE_DIR, _set_tmdb_key)
import settings_page
settings_page.init_app(app, BASE_DIR, lambda: APP_VERSION, lambda: {"library": str(library.LIBRARY_ROOT), "output": OUTPUT_BASE})

import notif     # alertes Telegram (redémarrage inattendu, erreurs, lot terminé, mise à jour)
notif.init_app(app, BASE_DIR, APP_VERSION, Path(BASE_DIR) / "data" / "moufloster.log", emby_settings._write_secret)

# ============================================================================
# TMDB Functions
# ============================================================================

def tmdb_get(url, params=None):
    """Appel TMDB: accepte la clé v3 (api_key) ou le jeton v4 (eyJ..., en-tête Bearer)"""
    params = dict(params or {})
    headers = {}
    if TMDB_API_KEY.startswith("eyJ"):
        headers["Authorization"] = f"Bearer {TMDB_API_KEY}"
    else:
        params["api_key"] = TMDB_API_KEY
    return requests.get(url, params=params, headers=headers, timeout=10)


def search_tmdb(query: str):
    """Cherche un film/série sur TheMovieDB"""
    # Vérifier que la clé API est configurée
    if not TMDB_API_KEY or TMDB_API_KEY == "TON_CLE_TMDB_ICI":
        return {
            "error": "❌ Clé API TMDB non configurée. Veuillez définir la variable d'environnement TMDB_API_KEY avec votre clé depuis https://www.themoviedb.org/settings/api"
        }

    url = "https://api.themoviedb.org/3/search/multi"
    params = {
        "query": query,
        "language": "fr-FR"
    }

    try:
        response = tmdb_get(url, params)

        # Vérifier les erreurs API
        if response.status_code == 401:
            return {"error": "❌ Clé API TMDB invalide ou expirée"}
        if response.status_code == 429:
            return {"error": "⏱️ Trop de requêtes à TMDB. Attendez quelques secondes..."}

        response.raise_for_status()
        data = response.json()

        results = []
        for item in data.get("results", [])[:10]:
            if item.get("media_type") not in ("movie", "tv"):
                continue
            title = item.get("title") or item.get("name")
            if not title:
                continue

            result = {
                "id": item.get("id"),
                "title": title,
                "year": item.get("release_date", "")[:4] or item.get("first_air_date", "")[:4] or "?",
                "media_type": item.get("media_type", "unknown"),
                "original_title": item.get("original_title") or item.get("original_name") or "",
            }
            results.append(result)

        # Sagas (collections TMDB : « Harry Potter - Saga »…) : absentes de la recherche « multi », cherchées à part
        try:
            r2 = tmdb_get("https://api.themoviedb.org/3/search/collection", {"query": query, "language": "fr-FR"})
            if r2.ok:
                for item in r2.json().get("results", [])[:5]:
                    if item.get("id") and item.get("name"):
                        results.append({"id": item["id"], "title": item["name"], "year": "?", "media_type": "collection",
                                        "original_title": item.get("original_name") or ""})
        except Exception as e:
            logger.warning(f"Recherche des sagas TMDB impossible: {diag.redact(str(e))}")

        return {"results": results}
    except Exception as e:
        logger.error(f"Erreur TMDB search: {diag.redact(str(e))}")
        return {"error": "❌ Erreur de connexion à TMDB (détails : bouton Journal)"}


def get_posters_for_item(tmdb_id: str, media_type: str):
    """Récupère TOUS les posters (toutes langues) d'un film/série, avec leur langue"""
    if media_type not in ("movie", "tv", "collection") or not str(tmdb_id).isdigit():
        return {"error": "Titre invalide"}
    url = f"https://api.themoviedb.org/3/{media_type}/{tmdb_id}/images"
    try:
        response = tmdb_get(url)  # sans filtre de langue: TMDB renvoie toutes les langues
        response.raise_for_status()
        data = response.json()

        posters = []
        counts = {}
        for poster in data.get("posters", []):
            poster_path = poster.get("file_path")
            if not poster_path:
                continue
            lang = (poster.get("iso_639_1") or "").strip()  # "" = sans texte
            counts[lang] = counts.get(lang, 0) + 1
            posters.append({
                "path": poster_path,
                "url": f"https://image.tmdb.org/t/p/w500{poster_path}",
                "vote": poster.get("vote_average", 0),
                "lang": lang,
                "w": poster.get("width"),
                "h": poster.get("height"),
            })
        posters.sort(key=lambda x: x["vote"], reverse=True)

        languages = [{"code": c, "count": n} for c, n in counts.items()]
        # sans texte d'abord, puis les langues les mieux fournies
        languages.sort(key=lambda l: (l["code"] != "", -l["count"], l["code"]))

        # Langue proposée par défaut: sans texte, sinon français, sinon anglais, sinon la plus fournie
        default = "all"
        for code in ("", "fr", "en"):
            if code in counts:
                default = code
                break
        else:
            if languages:
                default = languages[0]["code"]
        return {"posters": posters, "languages": languages, "default": default}
    except Exception as e:
        logger.error(f"Erreur TMDB posters: {diag.redact(str(e))}")
        return {"error": "❌ Affiches indisponibles sur TMDB (détails : bouton Journal)"}


def download_image(url: str):
    """Télécharge une image depuis une URL"""
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        img = Image.open(BytesIO(response.content))
        return img
    except Exception as e:
        logger.error(f"Erreur download image: {e}")
        return None


UPLOAD_DIR = os.path.join(OUTPUT_BASE, "_uploads")
_UPLOAD_RE = re.compile(r"^[0-9a-f]{32}\.jpg$")
UPLOAD_URL_PREFIX = "/api/upload/"
UPLOAD_KEEP_DAYS = 7
UPLOAD_MAX_SIDE = 4000                    # une image envoyée plus grande est réduite (assez pour un poster 1000x1500 zoomé)
Image.MAX_IMAGE_PIXELS = 60_000_000       # refuse les images géantes (mémoire du conteneur)


def _clean_uploads():
    """Les images envoyées et les étapes de « Effacer un élément » ne servent que le temps de la retouche : nettoyées après 7 jours."""
    import time as _t
    n = 0
    try:
        for f in os.scandir(UPLOAD_DIR):
            if f.is_file() and _UPLOAD_RE.match(f.name) and _t.time() - f.stat().st_mtime > UPLOAD_KEEP_DAYS * 86400:
                os.unlink(f.path)
                n += 1
    except OSError:
        pass
    if n:
        logger.info(f"🧹 {n} image(s) temporaire(s) de plus de {UPLOAD_KEEP_DAYS} jours supprimée(s) dans _uploads")


threading.Thread(target=_clean_uploads, daemon=True).start()


_source_cache = {}  # (url, taille) -> image: évite de retélécharger à chaque déplacement du poster
_SOURCE_CACHE_MAX = 6


def load_source_image(poster_url: str, tmdb_size: str):
    key = (poster_url, tmdb_size)
    cached = _source_cache.get(key)
    if cached is not None:
        return cached.copy()
    img = _load_source_image(poster_url, tmdb_size)
    if img is not None and tmdb_size != "original":  # l'original est lourd: on ne le garde pas en mémoire
        if len(_source_cache) >= _SOURCE_CACHE_MAX:
            _source_cache.pop(next(iter(_source_cache)))
        img.load()
        _source_cache[key] = img.copy()
    return img


def _load_source_image(poster_url: str, tmdb_size: str):
    """Image de départ: poster TMDB (taille w780/original) ou fichier envoyé par l'utilisateur"""
    if poster_url.startswith(UPLOAD_URL_PREFIX):
        name = os.path.basename(poster_url[len(UPLOAD_URL_PREFIX):])
        if not _UPLOAD_RE.match(name):
            return None
        try:
            img = Image.open(os.path.join(UPLOAD_DIR, name))
            img.load()
            return img
        except Exception as e:
            logger.error(f"Erreur lecture upload: {e}")
            return None
    if not poster_url.startswith("https://image.tmdb.org/t/p/"):
        return None  # on ne télécharge que depuis TMDB
    return download_image(re.sub(r"/t/p/[^/]+/", f"/t/p/{tmdb_size}/", poster_url, count=1))


# ============================================================================
# Image Composition
# ============================================================================

FONT_CANDIDATES = [
    str(Path(__file__).parent / "data" / "fonts" / "arialbd.ttf"),  # police à toi : à déposer dans data/fonts/ (voir INSTALL.md)
    str(Path(__file__).parent / "fonts" / "arialbd.ttf"),  # Arial Bold (fournie)
    str(Path(__file__).parent / "fonts" / "LiberationSans-Bold.ttf"),  # repli, métriques identiques à Arial
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/msttcorefonts/Arial_Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]


def load_font(size: int):
    for path in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default(size)
    except Exception:
        return ImageFont.load_default()


def fit_font(draw, text: str, base_size: int, max_width: int, min_size: int = 24):
    """Réduit la taille de police jusqu'à ce que le texte tienne dans max_width"""
    size = base_size
    font = load_font(size)
    while size > min_size and draw.textlength(text, font=font) > max_width:
        size -= 2
        font = load_font(size)
    return font, size


# Positions FIXES (coordonnées absolues sur 1000x1500, mesurées sur les exemples).
# Chaque calque a toujours la même hauteur, qu'il y ait 1 ou 2 lignes de titre.
BASELINE_L1 = 1268   # petite ligne au-dessus du titre (49 px)
BASELINE_L2 = 1342   # titre, 1re ligne (89 px)
BASELINE_L3 = 1414   # titre, 2e ligne (89 px)
BASELINE_L4 = 1397   # PSD « 2eme ligne 49px »: sous un titre d'1 ligne
BASELINE_L5 = 1457   # PSD « 3eme ligne 49px »: sous un titre de 2 lignes
SEASON_BASELINE = 1418       # grand chiffre gris derrière le titre
SEASON_SIZE = 294            # PSD: 89 pt x 3.304
SEASON_COLOR = (255, 255, 255, 56)  # PSD: blanc à 22 % d'opacité
# Dégradé noir: courbe mesurée dans le PSD (hauteur absolue sur 1500 px → opacité 0-1)
GRADIENT_CURVE = [(972, 0.0), (998, 0.05), (1066, 0.25), (1133, 0.5), (1249, 0.75),
                  (1365, 0.95), (1405, 0.999), (1420, 1.0)]


def _clamp_zoom(value):
    try:
        return min(max(float(value), 0.5), 3.0)
    except (TypeError, ValueError):
        return 1.0


def cover_overflow(size, border_style, zoom=1.0):
    """Combien de pixels (sur 1000x1500) l'image dépasse du cadre une fois agrandie: c'est la marge de recadrage"""
    border = BORDER_ANIME if border_style == "anime" else BORDER_STANDARD
    iw, ih = IMG_WIDTH - 2 * border, IMG_HEIGHT - 2 * border
    sw, sh = size
    scale = max(iw / sw, ih / sh) * _clamp_zoom(zoom)
    return [round(max(0.0, sw * scale - iw), 1), round(max(0.0, sh * scale - ih), 1)]


def _clamp01(value, default=0.5):
    try:
        return min(max(float(value), 0.0), 1.0)
    except (TypeError, ValueError):
        return default


def compose_poster(
    poster_image: Image.Image,
    border_style: str,  # "anime" ou "standard"
    show_gradient: bool,
    text_layer_1: str = "",  # 49px, au-dessus du titre
    text_layer_2: str = "",  # 89px, titre ligne 1
    text_layer_3: str = "",  # 89px, titre ligne 2
    text_layer_4: str = "",  # 49px, 2eme ligne du PSD (y=1397)
    text_layer_5: str = "",  # 49px, 3eme ligne du PSD (y=1457)
    season_number: str = "",   # grand chiffre gris derrière le titre
    crop_x: float = 0.5,       # 0 = bord gauche, 1 = bord droit (utile si l'image est trop large)
    crop_y: float = 0.5,       # 0 = haut, 1 = bas (utile si l'image est trop haute)
    zoom: float = 1.0          # 1 = l'image remplit le cadre; >1 = zoom avant; <1 = dézoom (bords flous)
) -> Image.Image:
    """
    Compose l'image finale (1000x1500) :
    1. Poster agrandi SANS déformation (ratio conservé), puis recadré pour remplir l'intérieur du cadre
    2. Dégradé noir (transparent -> noir complet), limité au poster
    3. Cadre blanc (anime 45 px / standard 22 px)
    4. Grand chiffre gris (saison) centré derrière le titre
    5. Textes centrés à hauteur fixe
    """
    border_width = BORDER_ANIME if border_style == "anime" else BORDER_STANDARD
    inner_w = IMG_WIDTH - (border_width * 2)
    inner_h = IMG_HEIGHT - (border_width * 2)

    cx, cy, zoom = _clamp01(crop_x), _clamp01(crop_y), _clamp_zoom(zoom)
    src = poster_image.convert("RGB")
    poster = ImageOps.fit(src, (inner_w, inner_h), method=Image.Resampling.LANCZOS, centering=(cx, cy))
    if abs(zoom - 1.0) > 0.005:
        scale = max(inner_w / src.width, inner_h / src.height) * zoom
        nw, nh = max(1, round(src.width * scale)), max(1, round(src.height * scale))
        resized = src.resize((nw, nh), Image.Resampling.LANCZOS)
        if nw < inner_w or nh < inner_h:
            # dézoom: les zones vides sont remplies par l'image elle-même, agrandie, floutée et assombrie
            canvas = poster.filter(ImageFilter.GaussianBlur(40)).point(lambda v: int(v * 0.6))
        else:
            canvas = Image.new("RGB", (inner_w, inner_h))
        ox = round(-(nw - inner_w) * cx) if nw >= inner_w else (inner_w - nw) // 2
        oy = round(-(nh - inner_h) * cy) if nh >= inner_h else (inner_h - nh) // 2
        canvas.paste(resized, (ox, oy))
        poster = canvas

    if show_gradient:
        gradient = Image.new("RGBA", (inner_w, inner_h), (0, 0, 0, 0))
        gdraw = ImageDraw.Draw(gradient)
        for y in range(inner_h):
            abs_y = y + border_width
            alpha = int(round(255 * float(np.interp(abs_y, [p[0] for p in GRADIENT_CURVE], [p[1] for p in GRADIENT_CURVE]))))
            if alpha:
                gdraw.line([(0, y), (inner_w, y)], fill=(0, 0, 0, alpha))
        poster = Image.alpha_composite(poster.convert("RGBA"), gradient).convert("RGB")

    final = Image.new("RGB", (IMG_WIDTH, IMG_HEIGHT), "white")
    final.paste(poster, (border_width, border_width))

    center_x = IMG_WIDTH // 2
    max_text_w = inner_w - 60

    # Grand chiffre/texte gris (saison) derrière le titre
    if season_number and season_number.strip() and season_number.strip() != "0":
        overlay = Image.new("RGBA", (IMG_WIDTH, IMG_HEIGHT), (0, 0, 0, 0))
        odraw = ImageDraw.Draw(overlay)
        season_txt = season_number.strip().upper()
        sfont, _ = fit_font(odraw, season_txt, SEASON_SIZE, max_text_w - 40, min_size=60)
        odraw.text((center_x, SEASON_BASELINE), season_txt, fill=SEASON_COLOR, font=sfont, anchor="ms")
        final = Image.alpha_composite(final.convert("RGBA"), overlay).convert("RGB")

    draw = ImageDraw.Draw(final)

    t1, t2, t3, t4, t5 = (
        (t or "").strip().upper() for t in (text_layer_1, text_layer_2, text_layer_3, text_layer_4, text_layer_5)
    )

    # Chaque ligne a sa position EXACTE du PSD (aucun décalage automatique)
    for text, base_size, baseline in (
        (t1, 49, BASELINE_L1),
        (t2, 89, BASELINE_L2),
        (t3, 89, BASELINE_L3),
        (t4, 49, BASELINE_L4),
        (t5, 49, BASELINE_L5),
    ):
        if text:
            font, _ = fit_font(draw, text, base_size, max_text_w)
            draw.text((center_x, baseline), text, fill="white", font=font, anchor="ms")

    return final


# ============================================================================
# Flask Routes
# ============================================================================

@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE, version=APP_VERSION)


@app.route("/api/search", methods=["POST"])
def api_search():
    """Recherche sur TMDB"""
    data = request.get_json(silent=True) or {}
    query = data.get("query", "").strip()

    if not query:
        return jsonify({"error": "Query vide"}), 400

    result = search_tmdb(query)
    return jsonify(result)


@app.route("/api/posters", methods=["POST"])
def api_posters():
    """Récupère les posters pour un film/série"""
    data = request.get_json(silent=True) or {}
    tmdb_id = data.get("id")
    media_type = data.get("media_type", "movie")

    if not tmdb_id:
        return jsonify({"error": "ID manquant"}), 400

    result = get_posters_for_item(str(tmdb_id), media_type)
    return jsonify(result)


@app.route("/api/upload", methods=["POST"])
def api_upload():
    """Poster envoyé manuellement par l'utilisateur (image quelconque)"""
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "Aucun fichier reçu"}), 400
    try:
        img = Image.open(f.stream)
        img.load()
        img = ImageOps.exif_transpose(img)
        if img.mode in ("RGBA", "LA", "P"):
            rgba = img.convert("RGBA")
            bg = Image.new("RGB", rgba.size, "white")
            bg.paste(rgba, mask=rgba.split()[3])
            img = bg
        else:
            img = img.convert("RGB")
    except Exception:
        return jsonify({"error": "Ce fichier n'est pas une image lisible (JPG, PNG, WebP...)"}), 400
    if img.width < 100 or img.height < 100:
        return jsonify({"error": "Image trop petite"}), 400
    if max(img.size) > UPLOAD_MAX_SIDE:
        img.thumbnail((UPLOAD_MAX_SIDE, UPLOAD_MAX_SIDE), Image.LANCZOS)

    os.makedirs(UPLOAD_DIR, exist_ok=True)
    name = uuid.uuid4().hex + ".jpg"
    img.save(os.path.join(UPLOAD_DIR, name), quality=95)
    logger.info(f"⬆️ Upload: {name} ({img.width}x{img.height})")
    return jsonify({"url": UPLOAD_URL_PREFIX + name, "w": img.width, "h": img.height})


@app.route("/api/upload/<name>")
def api_upload_file(name):
    if not _UPLOAD_RE.match(name):
        return jsonify({"error": "Fichier introuvable"}), 404
    path = os.path.join(UPLOAD_DIR, name)
    if not os.path.isfile(path):
        return jsonify({"error": "Fichier introuvable"}), 404
    return send_file(path, mimetype="image/jpeg")


def inpaint_region(img: Image.Image, mask_img: Image.Image):
    """
    Efface la zone peinte et la remplit avec son environnement (comme un remplissage d'après le contenu).
    Retourne (image, moteur, note): LaMa si installé, sinon OpenCV (et la note dit pourquoi).
    """
    import numpy as np
    import cv2

    img = img.convert("RGB")
    if max(img.size) > 4500:
        img.thumbnail((4500, 4500))
    w, h = img.size

    mask = np.array(mask_img.convert("L").resize((w, h), Image.Resampling.BILINEAR))
    mask = (mask > 40).astype("uint8") * 255
    if not mask.any():
        raise ValueError("Rien n'a été peint : colorie d'abord la zone à effacer")

    # On élargit un peu la zone: les contours (halo, anti-crénelage) d'un logo dépassent toujours du tracé
    grow = max(3, int(min(w, h) * 0.005))
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * grow + 1, 2 * grow + 1))
    mask = cv2.dilate(mask, kernel)

    note = ""
    try:
        import lama
        ok, reason = lama.check()
        if ok:
            t0 = time.time()
            result = lama.inpaint(img, mask)
            logger.info(f"LaMa: zone effacée en {time.time() - t0:.1f} s (image {w}x{h})")
            return result, "LaMa", ""
        note = f"LaMa indisponible: {reason}"
        logger.warning(note)
    except Exception as e:  # jamais bloquant: on retombe sur le moteur léger
        logger.exception("LaMa a échoué pendant l'effacement, repli sur OpenCV")
        note = f"LaMa a échoué: {type(e).__name__}: {e}"

    ys, xs = np.where(mask > 0)
    # On ne travaille que sur la zone concernée + une marge (plus rapide, et le contexte reste proche)
    margin = max(60, int(0.15 * max(xs.max() - xs.min(), ys.max() - ys.min())))
    x0, x1 = max(0, xs.min() - margin), min(w, xs.max() + margin + 1)
    y0, y1 = max(0, ys.min() - margin), min(h, ys.max() + margin + 1)

    arr = np.array(img)
    crop = cv2.cvtColor(arr[y0:y1, x0:x1], cv2.COLOR_RGB2BGR)
    mcrop = mask[y0:y1, x0:x1]
    radius = max(3, int(min(w, h) * 0.004))
    filled = cv2.inpaint(crop, mcrop, radius, cv2.INPAINT_TELEA)
    arr[y0:y1, x0:x1] = cv2.cvtColor(filled, cv2.COLOR_BGR2RGB)
    return Image.fromarray(arr), "OpenCV", note


@app.route("/api/erase", methods=["POST"])
def api_erase():
    """Efface un élément (logo, texte...) peint par l'utilisateur sur le poster"""
    data = request.get_json(silent=True) or {}
    poster_url = data.get("poster_url") or ""
    mask_data = data.get("mask") or ""
    try:
        import cv2  # noqa: F401
    except ImportError:
        return jsonify({"error": "Outil d'effacement pas encore installé sur le serveur (attends le prochain redémarrage)"}), 503
    if not mask_data.startswith("data:image/png;base64,") or len(mask_data) > 20_000_000:
        return jsonify({"error": "Tracé invalide"}), 400

    img = load_source_image(poster_url, "original")
    if not img:
        return jsonify({"error": "Impossible de charger l'image"}), 400
    try:
        mask_img = Image.open(BytesIO(base64.b64decode(mask_data.split(",", 1)[1])))
        result, engine, note = inpaint_region(img, mask_img)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.exception("Erreur effacement")
        return jsonify({"error": f"Effacement impossible: {e}"}), 500

    os.makedirs(UPLOAD_DIR, exist_ok=True)
    name = uuid.uuid4().hex + ".jpg"
    result.save(os.path.join(UPLOAD_DIR, name), quality=95)
    logger.info(f"🧽 Effacement ({engine}): {name} ({result.width}x{result.height})")
    return jsonify({"url": UPLOAD_URL_PREFIX + name, "w": result.width, "h": result.height, "engine": engine, "note": note})


@app.route("/api/preview", methods=["POST"])
def api_preview():
    """Génère un aperçu du poster composé"""
    data = request.get_json(silent=True) or {}
    poster_url = data.get("poster_url")

    border_style = data.get("border_style", "standard")
    show_gradient = data.get("show_gradient", True)

    text_1 = data.get("text_1", "")
    text_2 = data.get("text_2", "")
    text_3 = data.get("text_3", "")
    text_4 = data.get("text_4", "")
    text_5 = data.get("text_5", "")
    season = data.get("season", "")

    if not poster_url:
        return jsonify({"error": "Poster URL manquante"}), 400

    # Image (w780 pour l'aperçu: bon compromis qualité/vitesse)
    img = load_source_image(poster_url, "w780")
    if not img:
        return jsonify({"error": "Impossible de charger l'image"}), 400
    img.thumbnail((1600, 2400))  # accélère l'aperçu pour les grosses images envoyées

    # Composer
    try:
        composed = compose_poster(
            img,
            border_style=border_style,
            show_gradient=show_gradient,
            text_layer_1=text_1,
            text_layer_2=text_2,
            text_layer_3=text_3,
            text_layer_4=text_4,
            text_layer_5=text_5,
            season_number=season,
            crop_x=data.get("crop_x", 0.5),
            crop_y=data.get("crop_y", 0.5),
            zoom=data.get("zoom", 1.0)
        )

        # Convertir en base64 pour l'affichage
        buffer = BytesIO()
        composed.save(buffer, format="JPEG", quality=95)
        buffer.seek(0)
        img_base64 = base64.b64encode(buffer.getvalue()).decode()

        return jsonify({
            "preview": f"data:image/jpeg;base64,{img_base64}",
            "overflow": cover_overflow(img.size, border_style, data.get("zoom", 1.0)),
        })
    except Exception as e:
        logger.error(f"Erreur composition: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/save", methods=["POST"])
def api_save():
    """Sauvegarde le poster composé"""
    data = request.get_json(silent=True) or {}
    poster_url = data.get("poster_url")
    title = data.get("title", "poster")

    border_style = data.get("border_style", "standard")
    show_gradient = data.get("show_gradient", True)

    text_1 = data.get("text_1", "")
    text_2 = data.get("text_2", "")
    text_3 = data.get("text_3", "")
    text_4 = data.get("text_4", "")
    text_5 = data.get("text_5", "")
    season = data.get("season", "")

    if not poster_url:
        return jsonify({"error": "Poster URL manquante"}), 400

    # Image en résolution originale pour le fichier final
    img = load_source_image(poster_url, "original")
    if not img:
        return jsonify({"error": "Impossible de charger l'image"}), 400

    # Composer
    try:
        composed = compose_poster(
            img,
            border_style=border_style,
            show_gradient=show_gradient,
            text_layer_1=text_1,
            text_layer_2=text_2,
            text_layer_3=text_3,
            text_layer_4=text_4,
            text_layer_5=text_5,
            season_number=season,
            crop_x=data.get("crop_x", 0.5),
            crop_y=data.get("crop_y", 0.5),
            zoom=data.get("zoom", 1.0)
        )

        # Sauvegarder
        safe_title = "".join(c for c in title if c.isalnum() or c in " -_").strip() or "poster"
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{safe_title}_{timestamp}.jpg"
        n = 2
        while os.path.exists(os.path.join(OUTPUT_BASE, filename)):      # jamais d'écrasement (deux enregistrements dans la même seconde)
            filename = f"{safe_title}_{timestamp}_{n}.jpg"
            n += 1
        filepath = os.path.join(OUTPUT_BASE, filename)

        composed.save(filepath, quality=95)
        logger.info(f"✅ Sauvegardé: {filepath}")

        return jsonify({
            "success": True,
            "filename": filename,
            "path": filepath,
            "download_url": f"/api/download/{filename}"
        })
    except Exception as e:
        logger.error(f"Erreur save: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/download/<path:filename>")
def api_download(filename):
    """Envoie le poster sauvegardé au navigateur (téléchargement)"""
    safe_name = os.path.basename(filename)
    filepath = os.path.join(OUTPUT_BASE, safe_name)
    if not os.path.isfile(filepath):
        return jsonify({"error": "Fichier introuvable"}), 404
    inline = request.args.get("inline") == "1"
    resp = send_file(filepath, mimetype="image/jpeg", as_attachment=not inline, download_name=safe_name)
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ============================================================================
# Envoi vers la médiathèque
# ============================================================================

BACKUP_ROOT = os.path.join(OUTPUT_BASE, "_anciens-posters")


# ---------- Sagas : l'affiche est envoyée directement à Emby (pas de dossier dans la médiathèque) ----------

@app.route("/api/saga/match", methods=["POST"])
def api_saga_match():
    data = request.get_json(silent=True) or {}
    if not emby.configured():
        return jsonify({"error": "Emby n'est pas configuré : ajoute sa clé API dans ⚙️ Réglages."}), 400
    title = str(data.get("title") or "").strip()
    q = str(data.get("q") or "").strip()
    # « Harry Potter - Saga » chez TMDB, souvent « Harry Potter Collection » ou « Harry Potter » chez Emby
    base = re.sub(r"\s*[-–:]?\s*(saga|collection|la collection|trilogie|trilogy)\s*$", "", title, flags=re.I).strip()
    terms = [q] if q else [title, base, str(data.get("original_title") or "").strip()]
    try:
        cands = emby.find_boxsets(None if q else _to_int(data.get("tmdb_id")), terms)
    except emby.EmbyError as e:
        return jsonify({"error": str(e)}), 502
    for c in cands:
        c["score"] = 1.0 if c["sure"] else max(library.difflib.SequenceMatcher(None, library.fold(t), library.fold(c["name"])).ratio() for t in terms if t)
    cands.sort(key=lambda c: -c["score"])
    sel = cands[0]["id"] if cands and cands[0]["score"] >= 0.6 else (cands[0]["id"] if cands else None)
    return jsonify({"candidates": cands, "selected": sel, "sure": bool(cands and cands[0]["sure"])})


@app.route("/api/saga/image/<item_id>")
def api_saga_image(item_id):
    if not re.fullmatch(r"[0-9a-fA-F]{1,64}", item_id or ""):
        return jsonify({"error": "Identifiant invalide"}), 400
    img = emby.get_primary_image(item_id)
    if not img:
        return jsonify({"error": "Pas d'affiche"}), 404
    resp = app.response_class(img, mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/saga/apply", methods=["POST"])
def api_saga_apply():
    data = request.get_json(silent=True) or {}
    item_id = str(data.get("item_id") or "")
    name = str(data.get("name") or "saga")
    saved = os.path.join(OUTPUT_BASE, os.path.basename(str(data.get("saved_filename") or "")))
    if not re.fullmatch(r"[0-9a-fA-F]{1,64}", item_id):
        return jsonify({"error": "Choisis la collection Emby"}), 400
    if not os.path.isfile(saved):
        return jsonify({"error": "Poster sauvegardé introuvable"}), 404
    backup = None
    try:
        old = emby.get_primary_image(item_id)
        if old:                                   # l'ancienne affiche est gardée avant d'être remplacée
            folder = os.path.join(BACKUP_ROOT, "_sagas", re.sub(r"[^\w .()-]+", "_", name).strip() or item_id)
            os.makedirs(folder, exist_ok=True)
            backup = os.path.join(folder, f"poster.{datetime.now():%Y%m%d-%H%M%S}.jpg")
            n = 2
            while os.path.exists(backup):
                backup = os.path.join(folder, f"poster.{datetime.now():%Y%m%d-%H%M%S}-{n}.jpg")
                n += 1
            with open(backup, "wb") as f:
                f.write(old)
        with open(saved, "rb") as f:
            emby.set_primary_image(item_id, f.read())
    except emby.EmbyError as e:
        return jsonify({"error": f"Emby a refusé l'affiche : {e}"}), 502
    except OSError as e:
        return jsonify({"error": f"Sauvegarde de l'ancienne affiche impossible ({e.strerror or e}) : rien n'a été remplacé"}), 500
    logger.info(f"🖼️ Affiche de la saga « {name} » remplacée dans Emby" + (f" (ancienne : {backup})" if backup else ""))
    return jsonify({"ok": True, "backup": backup, "message": f"Affiche de « {name} » remplacée dans Emby" + (" (ancienne affiche sauvegardée)" if backup else "")})


def _to_int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


@app.route("/api/library/match", methods=["POST"])
def api_library_match():
    """Trouve le dossier de la médiathèque correspondant au titre choisi"""
    data = request.get_json(silent=True) or {}
    media_type = "tv" if data.get("media_type") == "tv" else "movie"
    if not library.LIBRARY_ROOT.is_dir():
        return jsonify({"error": f"Médiathèque introuvable: {library.LIBRARY_ROOT}"}), 500

    titles = [t for t in (data.get("title"), data.get("original_title")) if t]
    year = _to_int(data.get("year"))
    tmdb_id = _to_int(data.get("tmdb_id"))
    library.clear_cache()

    candidates = library.find_candidates(media_type, titles, year=year, tmdb_id=tmdb_id)
    memo = library.remembered(media_type, tmdb_id)

    confidence, selected = "aucun", None
    if memo:
        confidence, selected = "memorise", memo
        if not any(c["rel"] == memo for c in candidates):
            family, _, name = memo.partition("/")
            candidates.insert(0, {"rel": memo, "family": family, "name": name, "score": 3.0,
                                  "media_type": "tv" if family in library.FAMILIES["tv"] else "movie"})
    elif candidates:
        best = candidates[0]
        gap = best["score"] - (candidates[1]["score"] if len(candidates) > 1 else 0)
        selected = best["rel"]
        confidence = "sur" if best["score"] >= 0.9 and (gap >= 0.05 or best["score"] >= 2) else "incertain"

    folder_type = media_type
    if selected:                                    # un film rangé dans une série (spéciaux) : on raisonne avec le type du DOSSIER
        folder_type = "tv" if selected.partition("/")[0] in library.FAMILIES["tv"] else "movie"
    season_text = data.get("season_text")
    if folder_type == "tv" and media_type == "movie" and not str(season_text or "").strip():
        season_text = "0"
    name = library.target_name(folder_type, season_text)
    return jsonify({
        "candidates": candidates,
        "selected": selected,
        "confidence": confidence,
        "target_name": name,
        "inspect": library.inspect_target(selected, name) if selected else None,
        "emby_configured": emby.configured(),
    })


@app.route("/api/library/search", methods=["POST"])
def api_library_search():
    data = request.get_json(silent=True) or {}
    text = (data.get("q") or "").strip()
    if len(text) < 2:
        return jsonify({"candidates": []})
    media_type = data.get("media_type") if data.get("media_type") in ("movie", "tv") else None
    return jsonify({"candidates": library.search_folders(text, media_type)})


@app.route("/api/library/inspect", methods=["POST"])
def api_library_inspect():
    data = request.get_json(silent=True) or {}
    media_type = "tv" if data.get("media_type") == "tv" else "movie"
    try:
        tgt = library.resolve_target(data.get("rel"), media_type, data.get("season_text"), data.get("episode_text"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    info = library.inspect_target(data.get("rel"), tgt["name"], tgt["sub"], tgt["episode"])
    if info is None:
        return jsonify({"error": "Dossier invalide"}), 400
    return jsonify({"target_name": tgt["name"], "inspect": info, "video": tgt["video"]})


@app.route("/api/library/file")
def api_library_file():
    """Affiche un poster déjà présent dans la médiathèque (aperçu avant remplacement)"""
    rel, name = request.args.get("rel", ""), request.args.get("name", "")
    sub, episode = request.args.get("sub", ""), request.args.get("ep") == "1"
    folder = library.resolve_dir(rel, sub)
    ok_name = library.valid_episode_target(name) if episode else library.valid_target(name)
    if folder is None or not ok_name or not (folder / name).is_file():
        return jsonify({"error": "Fichier introuvable"}), 404
    resp = send_file(folder / name, mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/library/apply", methods=["POST"])
def api_library_apply():
    """Copie le poster sauvegardé dans le dossier de la médiathèque"""
    data = request.get_json(silent=True) or {}
    media_type = "tv" if data.get("media_type") == "tv" else "movie"
    # type du DOSSIER choisi (ex: un film TMDB rangé dans les spéciaux d'une série)
    target_type = "tv" if data.get("target_type") == "tv" else ("movie" if data.get("target_type") == "movie" else media_type)
    rel = data.get("rel")
    action = data.get("action")
    saved = os.path.basename(data.get("saved_filename") or "")
    try:
        tgt = library.resolve_target(rel, target_type, data.get("season_text"), data.get("episode_text"))
        result = library.apply_poster(os.path.join(OUTPUT_BASE, saved), rel, tgt["name"], action, BACKUP_ROOT,
                                      sub=tgt["sub"], episode=tgt["episode"])
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except OSError as e:
        logger.error(f"Erreur copie médiathèque: {e}")
        return jsonify({"error": f"Écriture impossible: {e}"}), 500

    library.remember(media_type, _to_int(data.get("tmdb_id")), rel)
    logger.info(f"📁 Médiathèque: {result['written']} (action={action}, sauvegarde={result['backup']})")

    emby_result = None
    # "Garder les deux" ne change pas le poster officiel: rien à actualiser
    if data.get("refresh_emby") and action != "both" and emby.configured():
        titles = [data.get("title"), data.get("original_title")]
        # l'identifiant TMDB n'a de sens que si le type du dossier est celui du poster choisi
        tmdb_for_emby = _to_int(data.get("tmdb_id")) if target_type == media_type else None
        emby_result = emby.refresh_for(target_type, tmdb_for_emby, rel, titles, data.get("season_text"),
                                       data.get("episode_text") if tgt["episode"] else None)
        logger.info(f"🔄 Emby: {emby_result}")
    return jsonify({"success": True, **result, "emby": emby_result})


# ---------- MouFlanga : le poster devient la couverture d'une série de mangas ----------

@app.route("/api/mouflanga/series", methods=["POST"])
def api_mouflanga_series():
    """Séries de MouFlanga, avec celle qui ressemble le plus au titre du poster."""
    data = request.get_json(silent=True) or {}
    if not mouflanga_link.mode():
        return jsonify({"error": "MouFlanga n'est pas relié : règle son adresse et sa clé API dans ⚙️ Réglages → MouFlanga."}), 404
    if mouflanga_link.mode() == "local" and not mouflanga_link.manga_dir().is_dir():
        return jsonify({"error": f"Dossier des mangas introuvable : {mouflanga_link.manga_dir()} (le partage est-il monté ?)"}), 500
    try:
        liste = mouflanga_link.series()
    except mouflanga_link.ErreurMouflanga as e:
        return jsonify({"error": str(e)}), 502
    noms = [x["name"] for x in liste]
    cible = data.get("cible")          # série demandée par MouFlanga (bouton « Créer avec MouFloster »)
    titres = [t for t in (data.get("title"), data.get("original_title")) if t]
    choisie = cible if cible in noms else mouflanga_link.meilleure(noms, titres)
    return jsonify({"series": liste, "selected": choisie})


@app.route("/api/mouflanga/cover")
def api_mouflanga_cover():
    """Couverture actuelle d'une série MouFlanga (aperçu avant remplacement)."""
    try:
        image = mouflanga_link.couverture(request.args.get("name", ""))
    except mouflanga_link.ErreurMouflanga as e:
        return jsonify({"error": str(e)}), 502
    if image is None:
        return jsonify({"error": "Pas de couverture choisie"}), 404
    resp = send_file(BytesIO(image), mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/mouflanga/apply", methods=["POST"])
def api_mouflanga_apply():
    data = request.get_json(silent=True) or {}
    saved = os.path.basename(data.get("saved_filename") or "")
    try:
        result = mouflanga_link.appliquer(os.path.join(OUTPUT_BASE, saved), data.get("name", ""), BACKUP_ROOT)
    except (ValueError, mouflanga_link.ErreurMouflanga) as e:
        return jsonify({"error": str(e)}), 400
    except OSError as e:
        logger.error(f"Erreur copie MouFlanga: {e}")
        return jsonify({"error": f"Écriture impossible: {e}"}), 500
    logger.info(f"📚 MouFlanga: {result['written']} (sauvegarde={result['backup']})")
    return jsonify({"success": True, **result})


# ---------- anciens posters d'un emplacement : voir, restaurer ----------

def _backup_folder(rel, tgt):
    shown = f"{rel}/{tgt['sub']}" if tgt["sub"] else rel
    root = Path(BACKUP_ROOT).resolve()
    d = (root / shown).resolve()
    return d if (d == root or root in d.parents) else None


def _poster_backups(rel, tgt):
    d = _backup_folder(rel, tgt)
    if d is None or not d.is_dir():
        return []
    pat = re.compile(re.escape(tgt["name"][:-4]) + r"\.(\d{8})-(\d{6})(?:-\d+)?\.jpg$")
    out = []
    for f in d.iterdir():
        m = pat.match(f.name)
        if m and f.is_file():
            day, t = m.group(1), m.group(2)
            out.append({"id": str(f.relative_to(Path(BACKUP_ROOT).resolve())), "date": f"{day[6:8]}/{day[4:6]}/{day[:4]} {t[:2]}:{t[2:4]}", "key": f.name})
    out.sort(key=lambda b: b["key"], reverse=True)
    return out


def _backup_path(backup_id):
    root = Path(BACKUP_ROOT).resolve()
    p = (root / str(backup_id or "")).resolve()
    if root not in p.parents or p.suffix.lower() != ".jpg" or not p.is_file():
        return None
    return p


@app.route("/api/library/backups", methods=["POST"])
def api_library_backups():
    data = request.get_json(silent=True) or {}
    media_type = "tv" if data.get("media_type") == "tv" else "movie"
    try:
        tgt = library.resolve_target(data.get("rel"), media_type, data.get("season_text"), data.get("episode_text"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"backups": _poster_backups(data.get("rel"), tgt)})


@app.route("/api/library/backup-file")
def api_library_backup_file():
    p = _backup_path(request.args.get("id"))
    if p is None:
        return jsonify({"error": "Fichier introuvable"}), 404
    return send_file(p, mimetype="image/jpeg")


@app.route("/api/library/restore", methods=["POST"])
def api_library_restore():
    """Remet un ancien poster en place (le poster actuel est sauvegardé avant, comme pour un remplacement)."""
    data = request.get_json(silent=True) or {}
    target_type = "tv" if data.get("target_type") == "tv" else "movie"
    media_type = "tv" if data.get("media_type") == "tv" else "movie"
    rel = data.get("rel")
    try:
        tgt = library.resolve_target(rel, target_type, data.get("season_text"), data.get("episode_text"))
        if not any(b["id"] == data.get("id") for b in _poster_backups(rel, tgt)):
            raise ValueError("Cet ancien poster n'appartient pas à cet emplacement")
        src = _backup_path(data.get("id"))
        if src is None:
            raise ValueError("Ancien poster introuvable")
        exists = (library.resolve_dir(rel, tgt["sub"]) / tgt["name"]).exists()
        result = library.apply_poster(str(src), rel, tgt["name"], "replace" if exists else "copy", BACKUP_ROOT,
                                      sub=tgt["sub"], episode=tgt["episode"])
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except OSError as e:
        logger.error(f"Erreur restauration médiathèque: {e}")
        return jsonify({"error": f"Écriture impossible: {e}"}), 500
    logger.info(f"↩️ Médiathèque: ancien poster restauré {result['written']} (depuis {data.get('id')})")
    emby_result = None
    if data.get("refresh_emby") and emby.configured():
        tmdb_for_emby = _to_int(data.get("tmdb_id")) if target_type == media_type else None
        emby_result = emby.refresh_for(target_type, tmdb_for_emby, rel, [data.get("title"), data.get("original_title")],
                                       data.get("season_text"), data.get("episode_text") if tgt["episode"] else None)
    return jsonify({"success": True, **result, "emby": emby_result})


# ---------- mises en page mémorisées (pour reprendre un poster plus tard) ----------
LAYOUTS_FILE = BASE_DIR / "data" / "layouts.json"
_layouts_lock = threading.Lock()


def _read_layouts():
    try:
        data = json.loads(LAYOUTS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


@app.route("/api/layout")
def api_layout_get():
    key = f"{request.args.get('media_type', '')}:{request.args.get('tmdb_id', '')}"
    return jsonify({"layout": _read_layouts().get(key)})


@app.route("/api/layout", methods=["POST"])
def api_layout_save():
    data = request.get_json(silent=True) or {}
    mt, tid, layout = str(data.get("media_type", "")), str(data.get("tmdb_id", "")), data.get("layout")
    if mt not in ("movie", "tv", "collection") or not tid.isdigit() or not isinstance(layout, dict):
        return jsonify({"error": "Mise en page invalide"}), 400
    if len(json.dumps(layout)) > 20000:
        return jsonify({"error": "Mise en page trop grande"}), 400
    with _layouts_lock:
        layouts = _read_layouts()
        layouts.pop(f"{mt}:{tid}", None)
        layouts[f"{mt}:{tid}"] = {**layout, "saved": datetime.now().strftime("%d/%m/%Y %H:%M")}
        while len(layouts) > 200:                        # on garde les 200 dernières
            layouts.pop(next(iter(layouts)))
        LAYOUTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = LAYOUTS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(layouts, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, LAYOUTS_FILE)
    return jsonify({"ok": True})


# ============================================================================
# HTML Template
# ============================================================================

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="fr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>🎬 Personnaliseur de Posters</title>
<link rel="icon" type="image/svg+xml" href="/icons/moufloster.svg"><link rel="icon" type="image/png" sizes="32x32" href="/icons/favicon-32.png"><link rel="apple-touch-icon" href="/icons/apple-touch-icon.png"><link rel="manifest" href="/icons/manifest.webmanifest"><meta name="theme-color" content="#121315">
    <link rel="stylesheet" href="/ui/mou-ui.css?v={{ version|urlencode }}">
    <script src="/ui/mou-ui.js?v={{ version|urlencode }}" defer></script>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Noto+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }

        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            padding: 20px;
        }

        .container {
            max-width: 1400px;
            margin: 0 auto;
            background: white;
            border-radius: 12px;
            box-shadow: 0 10px 40px rgba(0,0,0,0.3);
            overflow: hidden;
        }

        .header {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 30px;
            text-align: center;
        }

        .header h1 {
            font-size: 1.8em;
            margin-bottom: 10px;
        }

        .header p {
            opacity: 0.9;
            font-size: 0.95em;
        }

        @media (min-width: 768px) {
            .header h1 {
                font-size: 2.5em;
            }

            .header p {
                font-size: 1.1em;
            }
        }

        .version {
            position: absolute;
            top: 10px;
            right: 15px;
            background: rgba(255,255,255,0.2);
            color: white;
            padding: 5px 12px;
            border-radius: 20px;
            font-size: 0.75em;
            font-weight: bold;
        }

        .header {
            position: relative;
        }

        .content {
            display: grid;
            grid-template-columns: 1fr;
            gap: 15px;
            padding: 15px;
        }

        @media (min-width: 768px) {
            .content {
                gap: 25px;
                padding: 25px;
            }
        }

        @media (min-width: 1024px) {
            .content {
                grid-template-columns: 1fr 1fr;
                gap: 30px;
                padding: 30px;
            }
        }

        .panel {
            display: flex;
            flex-direction: column;
            gap: 15px;
        }

        @media (min-width: 768px) {
            .panel {
                gap: 20px;
            }
        }

        .section {
            background: #f8f9fa;
            border-radius: 8px;
            padding: 15px;
            border-left: 4px solid #667eea;
        }

        @media (min-width: 768px) {
            .section {
                padding: 20px;
            }
        }

        .section h3 {
            color: #333;
            margin-bottom: 12px;
            font-size: 1.05em;
        }

        @media (min-width: 768px) {
            .section h3 {
                font-size: 1.2em;
                margin-bottom: 15px;
            }
        }

        .search-box {
            display: flex;
            gap: 8px;
            flex-direction: column;
        }

        @media (min-width: 768px) {
            .search-box {
                flex-direction: row;
                gap: 10px;
            }
        }

        input[type="text"],
        input[type="number"],
        select {
            padding: 10px;
            border: 2px solid #e0e0e0;
            border-radius: 6px;
            font-size: 0.9em;
            transition: border-color 0.3s;
        }

        @media (min-width: 768px) {
            input[type="text"],
            input[type="number"],
            select {
                padding: 12px;
                font-size: 1em;
            }
        }

        input[type="text"]:focus,
        input[type="number"]:focus,
        select:focus {
            outline: none;
            border-color: #667eea;
        }

        button {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            border: none;
            padding: 10px 15px;
            border-radius: 6px;
            cursor: pointer;
            font-size: 0.85em;
            font-weight: bold;
            transition: transform 0.2s, box-shadow 0.2s;
            width: 100%;
        }

        @media (min-width: 768px) {
            button {
                padding: 12px 20px;
                font-size: 0.95em;
                width: auto;
            }
        }

        button:hover {
            transform: translateY(-2px);
            box-shadow: 0 5px 15px rgba(102, 126, 234, 0.4);
        }

        button:active {
            transform: translateY(0);
        }

        .results {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(70px, 1fr));
            gap: 6px;
            max-height: 200px;
            overflow-y: auto;
        }

        @media (min-width: 768px) {
            .results {
                grid-template-columns: repeat(auto-fill, minmax(90px, 1fr));
                gap: 8px;
                max-height: 280px;
            }
        }

        @media (min-width: 1024px) {
            .results {
                grid-template-columns: repeat(auto-fill, minmax(100px, 1fr));
                gap: 10px;
                max-height: 320px;
            }
        }

        .results-list {
            display: flex;
            flex-direction: column;
            gap: 6px;
            max-height: 260px;
            overflow-y: auto;
            -webkit-overflow-scrolling: touch;
        }

        .results-list .result-item {
            font-size: 0.95em;
            line-height: 1.3;
            word-break: break-word;
        }

        .poster-tools { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin-bottom: 10px; }
        .poster-tools select { flex: 1 1 160px; min-width: 0; padding: 8px; border-radius: 6px; border: 1px solid #ccc; font-size: 14px; }
        .upload-btn { flex: 0 0 auto; padding: 8px 12px; border-radius: 6px; background: #667eea; color: #fff; font-size: 14px; cursor: pointer; }
        .preview-image.draggable { cursor: grab; touch-action: none; }
        .preview-image.draggable.dragging { cursor: grabbing; }
        .preview-image img { user-select: none; -webkit-user-drag: none; }
        .crop-box { margin: 12px 0; display: flex; flex-direction: column; gap: 6px; font-size: 13px; }
        .crop-box .crop-title { font-weight: 600; }
        .crop-box .crop-title span { font-weight: normal; color: #777; }
        .crop-box input[type="range"] { width: 100%; padding: 0; }
        .crop-box button { align-self: flex-start; }

        .erase-stage { position: relative; width: 100%; max-width: 420px; margin: 12px auto; background: #222; line-height: 0; }
        .erase-stage img { width: 100%; height: auto; display: block; user-select: none; -webkit-user-drag: none; }
        .erase-stage canvas { position: absolute; left: 0; top: 0; width: 100%; height: 100%; touch-action: none; cursor: crosshair; }
        .erase-size { display: flex; flex-direction: column; gap: 4px; font-size: 13px; margin-bottom: 8px; }
        .erase-size input { width: 100%; padding: 0; }

        .poster-thumb {
            width: 100%;
            height: auto;
            object-fit: cover;
            display: block;
            cursor: pointer;
            border-radius: 6px;
            overflow: hidden;
            border: 3px solid transparent;
            transition: transform 0.2s, border-color 0.2s;
            aspect-ratio: 2/3;
            min-height: 100px;
        }

        .poster-thumb:hover {
            transform: scale(1.05);
            border-color: #667eea;
        }

        .poster-thumb.selected {
            border-color: #667eea;
            box-shadow: 0 0 10px rgba(102, 126, 234, 0.6);
        }

        .poster-thumb img {
            width: 100%;
            height: 100%;
            object-fit: cover;
        }

        .checkbox-group {
            display: flex;
            flex-direction: column;
            gap: 10px;
        }

        .checkbox-item {
            display: flex;
            align-items: center;
            gap: 8px;
        }

        .checkbox-item label {
            flex: 1;
            min-width: 0;
        }

        .panel, .section, .text-layer {
            min-width: 0;
            max-width: 100%;
        }

        input[type="checkbox"] {
            width: 18px;
            height: 18px;
            cursor: pointer;
            accent-color: #667eea;
            flex-shrink: 0;
        }

        .checkbox-item label {
            font-size: 0.85em;
            cursor: pointer;
        }

        @media (min-width: 768px) {
            .checkbox-item label {
                font-size: 0.95em;
            }
        }

        .text-layer {
            display: flex;
            flex-direction: column;
            gap: 6px;
            margin-bottom: 8px;
        }

        @media (min-width: 768px) {
            .text-layer {
                gap: 8px;
                margin-bottom: 12px;
            }
        }

        .text-layer label {
            font-weight: bold;
            color: #333;
            font-size: 0.85em;
        }

        @media (min-width: 768px) {
            .text-layer label {
                font-size: 0.95em;
            }
        }

        .text-layer input[type="text"] {
            width: 100%;
            padding: 8px 10px;
            border: 1px solid #ddd;
            border-radius: 4px;
            font-size: 14px;
            font-family: inherit;
            box-sizing: border-box;
            transition: border-color 0.2s;
        }

        .text-layer input[type="text"] {
            text-transform: uppercase;
            font-family: Arial, "Liberation Sans", Helvetica, sans-serif;
        }

        .text-layer input[type="text"]:focus {
            outline: none;
            border-color: #667eea;
            box-shadow: 0 0 0 2px rgba(102, 126, 234, 0.1);
        }

        @media (max-width: 768px) {
            .text-layer input[type="text"] {
                padding: 10px 12px;
                font-size: 16px;
            }
        }

        .preview-box {
            display: flex;
            flex-direction: column;
            gap: 12px;
        }

        @media (min-width: 768px) {
            .preview-box {
                gap: 15px;
            }
        }

        .preview-image {
            border-radius: 8px;
            overflow: hidden;
            background: #2d3139;
            padding: 12px;
            box-sizing: border-box;
            max-height: 400px;
            display: flex;
            align-items: center;
            justify-content: center;
            aspect-ratio: 2/3;
        }

        @media (min-width: 768px) {
            .preview-image {
                max-height: 500px;
            }
        }

        @media (min-width: 1024px) {
            .preview-image {
                max-height: 600px;
            }
        }

        .preview-image img {
            max-width: 100%;
            max-height: 100%;
            object-fit: contain;
            box-shadow: 0 4px 18px rgba(0, 0, 0, 0.6);
        }

        .preview-placeholder {
            color: #999;
            text-align: center;
            padding: 40px 20px;
            font-size: 0.95em;
        }

        @media (min-width: 768px) {
            .preview-placeholder {
                padding: 60px 20px;
                font-size: 1.1em;
            }
        }

        .button-group {
            display: flex;
            gap: 8px;
            flex-direction: column;
        }

        @media (min-width: 768px) {
            .button-group {
                gap: 10px;
                flex-direction: row;
            }
        }

        .button-group button {
            width: 100%;
        }

        @media (min-width: 768px) {
            .button-group button {
                width: auto;
                flex: 1;
            }
        }

        button.secondary {
            background: #6c757d;
        }

        button.success {
            background: linear-gradient(135deg, #11998e 0%, #38ef7d 100%);
        }

        .loading {
            display: none;
            text-align: center;
            color: #667eea;
            font-weight: bold;
        }

        .loading.show {
            display: block;
        }

        .message {
            padding: 12px 15px;
            border-radius: 6px;
            display: none;
            margin-bottom: 10px;
        }

        .message.show {
            display: block;
        }

        .message.error {
            background: #f8d7da;
            color: #721c24;
            border: 1px solid #f5c6cb;
        }

        .message.success {
            background: #d4edda;
            color: #155724;
            border: 1px solid #c3e6cb;
        }

        @media (max-width: 1024px) {
            .content {
                grid-template-columns: 1fr;
            }
        }

        /* ===== Fenêtre "Envoyer vers la médiathèque" ===== */
        .modal-overlay {
            display: none;
            position: fixed;
            inset: 0;
            background: rgba(0, 0, 0, 0.65);
            z-index: 1000;
            padding: 12px;
            overflow-y: auto;
            -webkit-overflow-scrolling: touch;
        }
        .modal-overlay.show { display: flex; }
        .modal {
            background: #fff;
            border-radius: 12px;
            width: 100%;
            max-width: 560px;
            margin: auto;
            padding: 16px;
            box-sizing: border-box;
        }
        .modal-head {
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            gap: 10px;
            margin-bottom: 10px;
        }
        .modal-head h3 { margin: 0; font-size: 1.05em; }
        .modal-sub { color: #666; font-size: 0.85em; margin-top: 2px; word-break: break-word; }
        .modal-close {
            width: auto;
            background: #eee;
            color: #333;
            padding: 4px 10px;
            font-size: 1.1em;
        }
        .lib-status {
            font-size: 0.9em;
            padding: 8px 10px;
            border-radius: 6px;
            margin: 8px 0;
            background: #eef1ff;
            color: #2c3a8c;
        }
        .lib-status.ok { background: #d4edda; color: #155724; }
        .lib-status.warn { background: #fff3cd; color: #7a5b00; }
        .lib-status.err { background: #f8d7da; color: #721c24; }
        .lib-field { margin: 10px 0; }
        .lib-field label { display: block; font-weight: bold; font-size: 0.85em; margin-bottom: 4px; }
        .lib-field select, .lib-field input { width: 100%; box-sizing: border-box; }
        .lib-search { display: flex; gap: 6px; margin-top: 6px; }
        .lib-search input { flex: 1; min-width: 0; }
        .lib-search button { width: auto; padding: 8px 12px; }
        .lib-compare {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 10px;
            margin: 12px 0;
        }
        .lib-compare figure { margin: 0; text-align: center; }
        .lib-compare .frame {
            background: #2d3139;
            border-radius: 8px;
            padding: 8px;
            aspect-ratio: 2 / 3;
            display: flex;
            align-items: center;
            justify-content: center;
            overflow: hidden;
        }
        .lib-compare img { max-width: 100%; max-height: 100%; box-shadow: 0 2px 10px rgba(0,0,0,.6); }
        .lib-compare .empty { color: #aab; font-size: 0.85em; padding: 10px; }
        .upload-astuce { font-size: 0.8em; color: #888; margin: 6px 0 4px; }
        .mfg-retour { display: inline-block; margin-top: 8px; padding: 6px 12px; border-radius: 6px; background: #52b54b; color: #fff; text-decoration: none; font-weight: 600; }
        .mfg-retour[hidden], #mfgBandeau[hidden] { display: none; }
        .lib-compare figcaption { font-size: 0.8em; color: #555; margin-top: 6px; line-height: 1.35; }
        .lib-compare figcaption b { color: #222; }
        .lib-actions { display: flex; flex-direction: column; gap: 8px; margin-top: 12px; }
        .layout-btn { width: 100%; margin: 6px 0 10px; }
        .result-item { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
        .batch-add { width: auto !important; padding: 4px 10px !important; font-size: 0.8em !important; flex: none; margin: 0 !important; }
        .batch-hint { color: var(--muted); font-size: 0.82em; margin-bottom: 10px; }
        .batch-item { display: flex; gap: 10px; align-items: flex-start; padding: 8px 0; border-top: 1px solid var(--line); }
        .batch-thumb { width: 64px; aspect-ratio: 2 / 3; object-fit: cover; border-radius: 4px; cursor: pointer; flex: none; background: var(--field); }
        .batch-info { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 4px; }
        .batch-name { font-weight: 600; font-size: 0.9em; }
        .batch-info input { padding: 6px 8px; font-size: 0.85em; }
        .batch-status { font-size: 0.78em; color: var(--muted); word-break: break-word; }
        .batch-del { width: auto !important; padding: 4px 8px !important; flex: none; }
        .batch-actions { display: flex; flex-direction: column; gap: 8px; margin-top: 10px; }
        .lib-backups { display: grid; grid-template-columns: repeat(auto-fill, minmax(96px, 1fr)); gap: 10px; }
        .lib-backups figure { margin: 0; text-align: center; }
        .lib-backups img { width: 100%; aspect-ratio: 2 / 3; object-fit: cover; border-radius: 4px; display: block; }
        .lib-backups figcaption { font-size: 0.75em; color: var(--muted); margin: 4px 0; }
        .lib-backups button { width: 100%; padding: 6px 4px; font-size: 0.8em; }
        button.danger { background: linear-gradient(135deg, #e0524d 0%, #b8302b 100%); }
        button.danger:disabled, .lib-actions button:disabled { opacity: 0.5; cursor: not-allowed; }
        @media (min-width: 768px) {
            .modal { padding: 22px; }
            .lib-actions { flex-direction: row; }
        }
        /* ===== Thème façon Emby (sombre, accent vert) ===== */
        :root {
            color-scheme: dark;
            --bg: #101010; --surface: #1c1c1c; --surface2: #242424; --field: #2a2a2a; --line: #3a3a3a;
            --text: #ffffff; --text2: #d0d0d0; --muted: #9a9a9a;
            --accent: #52b54b; --accent-hover: #63c75c; --err: #e5534b;
        }
        body {
            font-family: "Noto Sans", "Segoe UI", system-ui, -apple-system, Roboto, Arial, sans-serif;
            background: var(--bg);
            color: var(--text);
        }
        body { padding: 16px; }
        .container { max-width: 1200px; background: transparent; border-radius: 0; box-shadow: none; overflow: visible; }
        .header { background: #141414; color: var(--text); border-bottom: 1px solid var(--line); }
        .header h1 { font-weight: 600; }
        .header p { color: var(--muted); opacity: 1; }
        .version { background: var(--field); color: var(--muted); font-weight: 500; }
        .section { background: var(--surface2); border-left: 3px solid var(--accent); }
        .section h3 { color: var(--text); font-weight: 600; }
        input[type="text"], input[type="number"], select {
            background: var(--field); color: var(--text); border: 1px solid var(--line); border-radius: 4px; font-family: inherit;
        }
        input[type="text"]:focus, input[type="number"]:focus, select:focus {
            border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent);
        }
        input::placeholder { color: #777; }
        button { background: var(--accent); color: #fff; border-radius: 4px; font-family: inherit; font-weight: 600; }
        button:hover { background: var(--accent-hover); transform: none; box-shadow: none; }
        button.secondary { background: #3a3a3a; color: var(--text2); }
        button.secondary:hover { background: #4a4a4a; }
        button.success { background: var(--accent); }
        button.danger { background: #c0392b; }
        button.danger:hover { background: #d44636; }
        button:disabled { opacity: 0.5; cursor: not-allowed; }
        input[type="checkbox"], input[type="range"] { accent-color: var(--accent); }
        .loading { color: var(--accent); font-weight: 600; }
        .result-item {
            padding: 10px; border-radius: 4px; background: var(--field); border: 1px solid var(--line);
            color: var(--text2); cursor: pointer; transition: background 0.15s, border-color 0.15s;
        }
        .result-item:hover { border-color: var(--accent); }
        .result-item.active { background: var(--accent); border-color: var(--accent); color: #fff; }
        .upload-btn { background: var(--accent); color: #fff; border-radius: 4px; font-weight: 600; }
        .upload-btn:hover { background: var(--accent-hover); }
        .poster-thumb:hover { border-color: var(--accent); }
        .poster-thumb.selected { border-color: var(--accent); box-shadow: 0 0 10px rgba(82, 181, 75, 0.55); }
        .text-layer label, .checkbox-item label, .lib-field label, .erase-size { color: var(--text2); }
        .text-layer input[type="text"] { border: 1px solid var(--line); }
        .text-layer input[type="text"]:focus { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent); }
        .crop-box .crop-title span, .modal-sub, .lib-compare figcaption { color: var(--muted); }
        .lib-compare figcaption b { color: var(--text); }
        .preview-image, .lib-compare .frame, .erase-stage { background: #0b0b0b; }
        .preview-placeholder { color: #777; }
        .message.error { background: rgba(229, 83, 75, 0.15); color: #ffb4ae; border: 1px solid var(--err); }
        .message.success { background: rgba(82, 181, 75, 0.15); color: #b6e5b2; border: 1px solid var(--accent); }
        .modal { background: var(--surface); color: var(--text); border: 1px solid var(--line); border-radius: 8px; }
        .modal-overlay { background: rgba(0, 0, 0, 0.8); }
        .modal-close { background: #3a3a3a; color: var(--text2); }
        .lib-status { background: rgba(255, 255, 255, 0.07); color: var(--text2); }
        .lib-status.ok { background: rgba(82, 181, 75, 0.15); color: #b6e5b2; }
        .lib-status.warn { background: rgba(240, 173, 78, 0.15); color: #ffd699; }
        .lib-status.err { background: rgba(229, 83, 75, 0.15); color: #ffb4ae; }
        .lib-compare .empty { color: #888; }
        .results-list { margin-top: 10px; }
        .results-list.collapsed .result-item:not(.active) { display: none; }
        .results-list.collapsed .result-item.active::after { content: "  ▾ changer"; font-size: 0.8em; opacity: 0.85; }
        ::-webkit-scrollbar { width: 10px; height: 10px; }
        ::-webkit-scrollbar-thumb { background: #3a3a3a; border-radius: 5px; }
        ::-webkit-scrollbar-track { background: transparent; }
        @media (max-width: 480px) { body { padding: 8px; } }
    </style>
</head>
<body>
    <div class="container">
        <div id="mou-header" data-app="moufloster" data-prefix="MouFl" data-rest="oster" data-version="{{ version }}" data-settings="1"
             data-sub="Posters personnalisés avec TheMovieDB, cadre, dégradé et textes"
             data-actions='[{"label":"🧠 Relancer l&#39;installation de LaMa","url":"/api/lama/retry"}]'></div>

        <div class="content">
            <!-- GAUCHE: Controls -->
            <div class="panel">
                <!-- Venue depuis MouFlanga -->
                <div class="lib-status ok" id="mfgBandeau" hidden style="margin-bottom:12px">
                    <span id="mfgBandeauTexte"></span><br>
                    <a id="mfgRetour" class="mfg-retour" hidden></a>
                </div>
                <!-- Recherche -->
                <div class="section">
                    <h3>🔍 Recherche</h3>
                    <div class="search-box">
                        <input type="text" id="searchQuery" placeholder="Film, série ou manga…" />
                        <button onclick="search()">Chercher</button>
                    </div>
                    <div class="loading" id="searchLoading">Recherche...</div>
                    <div id="results" class="results-list"></div>
                </div>

                <!-- Lot de posters -->
                <div class="section" id="batchPanel" style="display:none">
                    <h3>📦 Lot de posters <span id="batchCount"></span></h3>
                    <div class="batch-hint">Chaque titre garde son affiche et ses textes ; le cadre et le dégradé choisis à droite valent pour tout le lot. Seuls les titres dont le dossier est trouvé avec certitude sont envoyés (l'ancien poster est sauvegardé) ; les autres restent à faire à la main.</div>
                    <div id="batchList"></div>
                    <div class="batch-actions">
                        <button id="batchSend" onclick="batchSendAll()">📤 Envoyer tout le lot vers la médiathèque</button>
                        <button class="secondary" onclick="batchClear()">Vider le lot</button>
                    </div>
                </div>

                <!-- Posters -->
                <div class="section">
                    <h3>🖼️ Posters</h3>
                    <div class="poster-tools">
                        <select id="langSelect" style="display:none" aria-label="Langue des posters"></select>
                        <label class="upload-btn" for="uploadFile">⬆️ Envoyer mon image</label>
                        <input type="file" id="uploadFile" accept="image/*" style="display:none" />
                        <button type="button" class="secondary" id="eraseOpen">🧽 Effacer un élément</button>
                    </div>
                    <div class="upload-astuce">Pas d'affiche trouvée (manga sans anime…) ? Tape juste le nom dans la recherche, puis « ⬆️ Envoyer mon image ».</div>
                    <div id="layoutBar"></div>
                    <div class="loading" id="postersLoading">Chargement...</div>
                    <div id="posters" class="results"></div>
                </div>

                <!-- Options -->
                <div class="section">
                    <h3>⚙️ Cadre & Dégradé</h3>
                    <div class="checkbox-group">
                        <div class="checkbox-item">
                            <input type="checkbox" id="borderAnime" />
                            <label for="borderAnime">Cadre Anime (large)</label>
                        </div>
                        <div class="checkbox-item">
                            <input type="checkbox" id="showGradient" checked />
                            <label for="showGradient">Gradient noir</label>
                        </div>
                    </div>
                </div>
            </div>

            <!-- DROITE: Textes + Aperçu -->
            <div class="panel">
                <!-- Textes -->
                <div class="section">
                    <h3>📝 Textes</h3>

                    <div class="text-layer">
                        <div class="checkbox-item">
                            <input type="checkbox" id="text1Enabled" />
                            <label for="text1Enabled">Ligne 1 (49px)</label>
                        </div>
                        <input type="text" id="text1" placeholder="Ex: Le" disabled />
                    </div>

                    <div class="text-layer">
                        <div class="checkbox-item">
                            <input type="checkbox" id="text2Enabled" />
                            <label for="text2Enabled">Ligne 2 (89px)</label>
                        </div>
                        <input type="text" id="text2" placeholder="Titre principal" disabled />
                    </div>

                    <div class="text-layer">
                        <div class="checkbox-item">
                            <input type="checkbox" id="text3Enabled" />
                            <label for="text3Enabled">Ligne 3 (89px)</label>
                        </div>
                        <input type="text" id="text3" placeholder="Suite du titre" disabled />
                    </div>

                    <div class="text-layer">
                        <div class="checkbox-item">
                            <input type="checkbox" id="text4Enabled" />
                            <label for="text4Enabled">Ligne 4 (49px) · sous un titre d'1 ligne</label>
                        </div>
                        <input type="text" id="text4" placeholder="Ex: et le Roi" disabled />
                    </div>

                    <div class="text-layer">
                        <div class="checkbox-item">
                            <input type="checkbox" id="text5Enabled" />
                            <label for="text5Enabled">Ligne 5 (49px) · sous un titre de 2 lignes</label>
                        </div>
                        <input type="text" id="text5" placeholder="Ex: Saison 2" disabled />
                    </div>

                    <div class="text-layer">
                        <div class="checkbox-item">
                            <input type="checkbox" id="seasonEnabled" />
                            <label for="seasonEnabled">Grand chiffre gris (saison)</label>
                        </div>
                        <input type="text" id="season" placeholder="1" disabled />
                        <div class="checkbox-item" style="margin-top:6px">
                            <input type="checkbox" id="seasonSpecial" />
                            <label for="seasonSpecial">Saison spéciale (Specials) : pas de chiffre sur le poster</label>
                        </div>
                    </div>
                </div>

                <!-- Aperçu -->
                <div class="section">
                    <h3>👁️ Aperçu</h3>
                    <div class="message" id="message"></div>
                    <div class="loading" id="previewLoading">Génération...</div>
                    <div class="preview-box">
                        <div class="preview-image" id="previewImage">
                            <div class="preview-placeholder">Aperçu</div>
                        </div>
                    <div class="crop-box">
                        <div class="crop-title">Recadrage <span>— glisse directement l'image ci-dessus, ou utilise les curseurs</span></div>
                        <label for="cropX">← gauche / droite →</label>
                        <input type="range" id="cropX" min="0" max="100" value="50" />
                        <label for="cropY">↑ haut / bas ↓</label>
                        <input type="range" id="cropY" min="0" max="100" value="50" />
                        <label for="zoom">🔍 Zoom : <span id="zoomVal">100</span> % <small>(molette sur l'image possible)</small></label>
                        <input type="range" id="zoom" min="50" max="300" value="100" />
                        <button type="button" class="secondary" id="cropReset">Recentrer / zoom 100 %</button>
                    </div>
                        <div class="button-group">
                            <button class="secondary" onclick="generatePreview()">Rafraîchir</button>
                            <button class="success" onclick="savePoster()">💾 Sauvegarder et télécharger</button>
                            <button onclick="sendToLibrary()">📁 Envoyer vers la médiathèque</button>
                            <button onclick="sendToMouflanga()">📚 Couverture dans MouFlanga</button>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <!-- Fenêtre: effacer un logo / texte -->
    <div class="modal-overlay" id="eraseModal">
        <div class="modal">
            <div class="modal-head">
                <div>
                    <h3>🧽 Effacer un élément</h3>
                    <div class="modal-sub">Colorie avec le doigt ou la souris ce que tu veux supprimer (logo, texte...), puis clique sur « Effacer ».</div>
                </div>
                <button class="modal-close" onclick="closeErase()" aria-label="Fermer">✕</button>
            </div>
            <div class="erase-stage" id="eraseStage">
                <img id="eraseImg" alt="Poster à nettoyer" />
                <canvas id="eraseCanvas"></canvas>
            </div>
            <label class="erase-size" for="eraseBrush">Taille du pinceau
                <input type="range" id="eraseBrush" min="2" max="20" value="6" />
            </label>
            <div class="lib-status" id="eraseStatus" style="display:none"></div>
            <div class="lib-actions">
                <button class="danger" id="eraseApply">Effacer la zone coloriée</button>
                <button class="secondary" id="eraseClear">Effacer mon tracé</button>
                <button class="secondary" id="eraseUndo" disabled>↩ Annuler le dernier effacement</button>
                <button class="success" id="eraseUse">Utiliser ce poster</button>
            </div>
        </div>
    </div>

    <!-- Fenêtre: couverture envoyée dans MouFlanga -->
    <div class="modal-overlay" id="mfgFiniModal">
        <div class="modal" style="max-width:420px">
            <div class="modal-head"><div><h3>✅ Couverture envoyée</h3><div class="modal-sub" id="mfgFiniTexte"></div></div></div>
            <div class="lib-actions">
                <a id="mfgFiniRetour" class="mfg-retour" hidden>↩ Retour à MouFlanga</a>
                <button class="secondary" id="mfgFiniFermer" onclick="document.getElementById('mfgFiniModal').classList.remove('show')">OK</button>
            </div>
        </div>
    </div>

    <!-- Fenêtre: couverture d'une série MouFlanga -->
    <div class="modal-overlay" id="mfgModal">
        <div class="modal">
            <div class="modal-head">
                <div>
                    <h3>📚 Couverture dans MouFlanga</h3>
                    <div class="modal-sub">Le poster devient la couverture de la série dans ta bibliothèque de mangas (l'ancienne est sauvegardée).</div>
                </div>
                <button class="modal-close" onclick="closeMouflanga()" aria-label="Fermer">✕</button>
            </div>
            <div class="lib-status" id="mfgStatus"></div>
            <div class="lib-field">
                <label for="mfgSelect">Série dans MouFlanga</label>
                <select id="mfgSelect" onchange="mfgShowOld()"></select>
            </div>
            <div class="lib-compare">
                <figure><div class="frame" id="mfgOldFrame"></div><figcaption><b>Actuelle</b><br>dans MouFlanga</figcaption></figure>
                <figure><div class="frame"><img id="mfgNewImg" alt="Nouvelle couverture" /></div><figcaption><b>Nouvelle</b><br>celle que tu viens de créer</figcaption></figure>
            </div>
            <div class="lib-actions"><button id="mfgApply" onclick="mfgApply()">🖼️ Mettre en couverture</button><button class="secondary" onclick="closeMouflanga()">Annuler</button></div>
        </div>
    </div>

    <!-- Fenêtre: envoi vers la médiathèque -->
    <div class="modal-overlay" id="sagaModal">
        <div class="modal">
            <div class="modal-head">
                <div>
                    <h3>📚 Envoyer l'affiche de la saga dans Emby</h3>
                    <div class="modal-sub" id="sagaTitle"></div>
                </div>
                <button class="modal-close" onclick="closeSaga()" aria-label="Fermer">✕</button>
            </div>
            <div class="lib-status" id="sagaStatus"></div>
            <div class="lib-field">
                <label for="sagaSelect">Collection dans Emby</label>
                <select id="sagaSelect" onchange="sagaShowOld()"></select>
                <div class="lib-search">
                    <input type="text" id="sagaSearch" placeholder="Autre collection : tape un nom..." onkeypress="if (event.key === 'Enter') sagaFind(this.value)" />
                    <button class="secondary" onclick="sagaFind(document.getElementById('sagaSearch').value)">Chercher</button>
                </div>
            </div>
            <div class="modal-sub">Une saga n'a pas de dossier dans la médiathèque : Emby garde son affiche lui-même. L'appli la remplace directement dans Emby (l'ancienne est sauvegardée).</div>
            <div class="lib-compare">
                <figure><div class="frame" id="sagaOldFrame"></div><figcaption><b>Actuelle</b><br>dans Emby</figcaption></figure>
                <figure><div class="frame"><img id="sagaNewImg" alt="Nouvelle affiche" /></div><figcaption><b>Nouvelle</b><br>celle que tu viens de créer</figcaption></figure>
            </div>
            <div class="lib-actions"><button id="sagaApply" onclick="sagaApply()">🖼️ Remplacer l'affiche dans Emby</button><button class="secondary" onclick="closeSaga()">Annuler</button></div>
        </div>
    </div>

    <div class="modal-overlay" id="libModal">
        <div class="modal">
            <div class="modal-head">
                <div>
                    <h3>📁 Envoyer vers la médiathèque</h3>
                    <div class="modal-sub" id="libTitle"></div>
                </div>
                <button class="modal-close" onclick="closeLibrary()" aria-label="Fermer">✕</button>
            </div>

            <div class="lib-status" id="libStatus">Recherche du dossier...</div>

            <div class="lib-field">
                <label for="libFolder">Dossier de destination</label>
                <select id="libFolder" onchange="libInspect()"></select>
                <div class="lib-search">
                    <input type="text" id="libSearch" placeholder="Autre dossier : tape un nom..." />
                    <button class="secondary" onclick="libSearchFolders()">Chercher</button>
                </div>
            </div>

            <div class="lib-field" id="libSeasonRow">
                <label for="libSeason">Numéro de saison <span style="font-weight:normal;color:var(--muted)">(vide = poster principal de la série, 0 = spéciaux)</span></label>
                <input type="number" id="libSeason" min="0" max="99" inputmode="numeric" oninput="libState.touched = true; libInspect()" />
                <label for="libEpisode" style="margin-top:8px">Numéro d'épisode <span style="font-weight:normal;color:var(--muted)">(vide = poster de la saison ; rempli = image de cet épisode, ex: saison 0 + épisode 100)</span></label>
                <input type="number" id="libEpisode" min="0" max="9999" inputmode="numeric" oninput="libInspect()" />
            </div>

            <div class="modal-sub">Fichier créé : <b id="libTarget"></b> <span id="libOthers"></span></div>

            <label class="lib-check" id="libEmbyRow" style="display:none;margin:10px 0;font-size:14px;">
                <input type="checkbox" id="libEmby" checked style="width:auto;margin-right:6px;" />
                Actualiser ce titre dans Emby (sans rescanner la bibliothèque)
            </label>

            <div class="lib-compare">
                <figure>
                    <div class="frame" id="libOldFrame"></div>
                    <figcaption id="libOldCap"></figcaption>
                </figure>
                <figure>
                    <div class="frame"><img id="libNewImg" alt="Nouveau poster" /></div>
                    <figcaption><b>Nouveau</b><br>celui que tu viens de créer</figcaption>
                </figure>
            </div>

            <div class="lib-actions" id="libActions"></div>
            <div id="libBackups"></div>
        </div>
    </div>

    <script>
        // Session expirée: retour à la page de connexion au lieu d'une erreur obscure
        const _fetch = window.fetch;
        window.fetch = async (...args) => {
            const res = await _fetch(...args);
            if (res.status === 401) { window.location.href = '/login'; }
            return res;
        };
        let selectedPoster = null;
        let selectedItem = null;

        // Toggle des champs texte selon checkboxes
        document.getElementById('text1Enabled').addEventListener('change', function() {
            document.getElementById('text1').disabled = !this.checked;
        });
        document.getElementById('text2Enabled').addEventListener('change', function() {
            document.getElementById('text2').disabled = !this.checked;
        });
        document.getElementById('text3Enabled').addEventListener('change', function() {
            document.getElementById('text3').disabled = !this.checked;
        });
        document.getElementById('text4Enabled').addEventListener('change', function() {
            document.getElementById('text4').disabled = !this.checked;
        });
        document.getElementById('text5Enabled').addEventListener('change', function() {
            document.getElementById('text5').disabled = !this.checked;
        });
        // Texte du grand chiffre: vide si "saison spéciale" cochée ou si on a écrit 0
        function seasonForPoster() {
            if (document.getElementById('seasonSpecial').checked) return '';
            if (!document.getElementById('seasonEnabled').checked) return '';
            const v = document.getElementById('season').value;
            return v.trim() === '0' ? '' : v;
        }
        document.getElementById('seasonEnabled').addEventListener('change', function() {
            document.getElementById('season').disabled = !this.checked || document.getElementById('seasonSpecial').checked;
        });
        document.getElementById('seasonSpecial').addEventListener('change', function() {
            const en = document.getElementById('seasonEnabled');
            if (this.checked) en.checked = true;  // le poster sera bien rangé comme "spéciaux"
            document.getElementById('season').disabled = !en.checked || this.checked;
            generatePreview();
        });

        // Auto-preview
        ['text1', 'text2', 'text3', 'text4', 'text5', 'season', 'borderAnime', 'showGradient'].forEach(id => {
            document.getElementById(id).addEventListener('change', generatePreview);
        });
        ['text1Enabled', 'text2Enabled', 'text3Enabled', 'text4Enabled', 'text5Enabled', 'seasonEnabled'].forEach(id => {
            document.getElementById(id).addEventListener('change', generatePreview);
        });
        // Glisser le poster directement sur l'aperçu pour le déplacer dans le cadre
        (function setupPreviewDrag() {
            const box = document.getElementById('previewImage');
            let drag = null;
            box.addEventListener('pointerdown', (ev) => {
                const img = box.querySelector('img');
                if (!img || ev.target !== img || !box.classList.contains('draggable')) return;
                ev.preventDefault();
                box.setPointerCapture(ev.pointerId);
                drag = {
                    x: ev.clientX, y: ev.clientY,
                    cx: document.getElementById('cropX').value / 100,
                    cy: document.getElementById('cropY').value / 100,
                    scale: 1000 / img.getBoundingClientRect().width
                };
                box.classList.add('dragging');
            });
            box.addEventListener('pointermove', (ev) => {
                if (!drag) return;
                const clamp = (v) => Math.min(1, Math.max(0, v));
                // pousser le poster vers la droite = montrer plus de sa partie gauche
                if (previewOverflow[0] > 1) {
                    const v = clamp(drag.cx - (ev.clientX - drag.x) * drag.scale / previewOverflow[0]);
                    document.getElementById('cropX').value = Math.round(v * 100);
                }
                if (previewOverflow[1] > 1) {
                    const v = clamp(drag.cy - (ev.clientY - drag.y) * drag.scale / previewOverflow[1]);
                    document.getElementById('cropY').value = Math.round(v * 100);
                }
                generatePreview(true);
            });
            const stop = () => { drag = null; box.classList.remove('dragging'); };
            box.addEventListener('pointerup', stop);
            box.addEventListener('pointercancel', stop);
        })();
        // Curseurs: aperçu en direct pendant qu'on les déplace
        ['cropX', 'cropY'].forEach(id => {
            document.getElementById(id).addEventListener('input', () => generatePreview(true));
        });
        document.getElementById('cropReset').addEventListener('click', () => {
            document.getElementById('cropX').value = 50;
            document.getElementById('cropY').value = 50;
            document.getElementById('zoom').value = 100;
            document.getElementById('zoomVal').textContent = '100';
            generatePreview();
        });
        document.getElementById('zoom').addEventListener('input', () => {
            document.getElementById('zoomVal').textContent = document.getElementById('zoom').value;
            generatePreview(true);
        });
        // Molette sur l'aperçu = zoom
        document.getElementById('previewImage').addEventListener('wheel', (ev) => {
            if (!ev.target.closest('img')) return;
            ev.preventDefault();
            const z = document.getElementById('zoom');
            z.value = Math.min(300, Math.max(50, Number(z.value) + (ev.deltaY < 0 ? 5 : -5)));
            document.getElementById('zoomVal').textContent = z.value;
            generatePreview(true);
        }, {passive: false});

        async function search() {
            const query = document.getElementById('searchQuery').value.trim();
            if (!query) return;

            document.getElementById('searchLoading').classList.add('show');
            document.getElementById('results').innerHTML = '';

            try {
                const res = await fetch('/api/search', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({query})
                });
                const data = await res.json();

                if (data.error) {
                    showMessage(data.error, 'error');
                    return;
                }

                const resultsDiv = document.getElementById('results');
                resultsDiv.classList.remove('collapsed');
                data.results.forEach(item => {
                    const div = document.createElement('div');
                    div.className = 'result-item';
                    div.textContent = (item.media_type === 'collection' ? '📚 ' : '') + item.title + (item.year !== '?' ? ` (${item.year})` : '') + (item.media_type === 'collection' ? ' · saga' : '');
                    if (item.media_type !== 'collection') {          // les sagas vont directement dans Emby : pas dans le lot
                        const add = document.createElement('button');
                        add.type = 'button'; add.className = 'batch-add'; add.textContent = '➕ Lot'; add.title = 'Ajouter au lot de posters';
                        add.addEventListener('click', ev => { ev.stopPropagation(); batchAdd(item); });
                        div.appendChild(add);
                    }
                    div.addEventListener('click', () => {
                        // clic sur le titre déjà choisi (liste refermée): on rouvre la liste pour en changer
                        if (div.classList.contains('active') && resultsDiv.classList.contains('collapsed')) {
                            resultsDiv.classList.remove('collapsed');
                            return;
                        }
                        selectedItem = item;
                        preremplirTitre(item.title);
                        loadPosters(item);
                        document.querySelectorAll('.result-item').forEach(el => el.classList.remove('active'));
                        div.classList.add('active');
                        resultsDiv.classList.add('collapsed');  // la liste se referme: seul le titre choisi reste affiché
                    });

                    resultsDiv.appendChild(div);
                });
            } finally {
                document.getElementById('searchLoading').classList.remove('show');
            }
        }

        let allPosters = [];

        function langLabel(code) {
            if (code === '') return 'Sans texte';
            try {
                const n = new Intl.DisplayNames(['fr'], {type: 'language'}).of(code);
                return n ? n.charAt(0).toUpperCase() + n.slice(1) : code.toUpperCase();
            } catch (e) {
                return code.toUpperCase();
            }
        }

        function showPosters(code) {
            const postersDiv = document.getElementById('posters');
            postersDiv.innerHTML = '';
            const list = code === 'all' ? allPosters : allPosters.filter(p => p.lang === code);
            list.forEach(poster => {
                const img = document.createElement('img');
                img.src = poster.url;
                img.loading = 'lazy';
                img.title = poster.lang ? langLabel(poster.lang) : 'Sans texte';
                img.className = 'poster-thumb';
                if (selectedPoster && selectedPoster.url === poster.url) img.classList.add('selected');
                img.onclick = () => {
                    selectPoster(poster);
                    img.classList.add('selected');
                };
                postersDiv.appendChild(img);
            });
        }

        function selectPoster(poster) {
            selectedPoster = poster;
            document.querySelectorAll('.poster-thumb').forEach(el => el.classList.remove('selected'));
            generatePreview();
        }

        // ===== Lot de posters : plusieurs titres d'un coup, même cadre et même dégradé =====
        const batch = [];
        function batchSplitTitle(t) {
            t = (t || '').trim();
            const m = t.match(/^(.+?)\\s*(?::| - | – )\\s*(.+)$/);
            if (m) return [m[1], m[2]];
            if (t.length <= 16) return [t, ''];
            const words = t.split(' '); let best = 1, bestDiff = 1e9;
            for (let i = 1; i < words.length; i++) { const d = Math.abs(words.slice(0, i).join(' ').length - words.slice(i).join(' ').length); if (d < bestDiff) { bestDiff = d; best = i; } }
            return [words.slice(0, best).join(' '), words.slice(best).join(' ')];
        }
        async function batchAdd(item) {
            if (batch.some(b => b.item.id === item.id && b.item.media_type === item.media_type)) return showMessage('Déjà dans le lot', 'error');
            const [l2, l3] = batchSplitTitle(item.title);
            const entry = {item, posters: [], poster: null, l2, l3, status: '⏳ chargement des affiches…'};
            batch.push(entry); batchRender();
            try {
                const data = await (await fetch('/api/posters', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({id: item.id, media_type: item.media_type})})).json();
                const all = data.posters || [];
                const pref = all.filter(p => p.lang === '').concat(all.filter(p => p.lang === 'fr'), all.filter(p => p.lang !== '' && p.lang !== 'fr'));
                entry.posters = pref.slice(0, 12); entry.poster = entry.posters[0] || null;
                entry.status = entry.poster ? '' : '❌ aucune affiche sur TheMovieDB';
            } catch (e) { entry.status = '❌ affiches indisponibles'; }
            batchRender();
        }
        function batchClear() { batch.length = 0; batchRender(); }
        function batchRender() {
            document.getElementById('batchPanel').style.display = batch.length ? 'block' : 'none';
            document.getElementById('batchCount').textContent = batch.length ? '(' + batch.length + ')' : '';
            const list = document.getElementById('batchList'); list.innerHTML = '';
            batch.forEach((b, i) => {
                const row = document.createElement('div'); row.className = 'batch-item';
                const thumb = document.createElement('img'); thumb.className = 'batch-thumb'; thumb.alt = '';
                if (b.poster) thumb.src = b.poster.url;
                thumb.title = 'Clique pour changer d’affiche';
                thumb.onclick = () => { if (b.posters.length > 1) { const k = b.posters.indexOf(b.poster); b.poster = b.posters[(k + 1) % b.posters.length]; batchRender(); } };
                const info = document.createElement('div'); info.className = 'batch-info';
                const name = document.createElement('div'); name.className = 'batch-name'; name.textContent = b.item.title + (b.item.year && b.item.year !== '?' ? ' (' + b.item.year + ')' : '') + (b.item.media_type === 'tv' ? ' · série' : ' · film');
                const in2 = document.createElement('input'); in2.value = b.l2; in2.placeholder = 'Ligne 2 (titre)'; in2.oninput = () => { b.l2 = in2.value; };
                const in3 = document.createElement('input'); in3.value = b.l3; in3.placeholder = 'Ligne 3 (suite, facultatif)'; in3.oninput = () => { b.l3 = in3.value; };
                const st = document.createElement('div'); st.className = 'batch-status'; st.textContent = b.status || (b.posters.length > 1 ? '↻ clique sur l’affiche pour en changer (' + b.posters.length + ')' : '');
                const del = document.createElement('button'); del.type = 'button'; del.className = 'secondary batch-del'; del.textContent = '✕'; del.title = 'Retirer du lot';
                del.onclick = () => { batch.splice(i, 1); batchRender(); };
                info.append(name, in2, in3, st);
                row.append(thumb, info, del); list.appendChild(row);
            });
        }
        async function batchPost(url, body) {
            const res = await fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
            return res.json();
        }
        async function batchSendAll() {
            const todo = batch.filter(b => b.poster && !b.done);
            if (!todo.length) return showMessage('Rien à envoyer : ajoute des titres avec une affiche', 'error');
            if (!confirm('Envoyer ' + todo.length + ' poster(s) vers la médiathèque ? Les anciens posters seront sauvegardés.')) return;
            const btn = document.getElementById('batchSend'); btn.disabled = true;
            let sent = 0, manual = 0, errs = 0;
            for (const b of todo) {
                try {
                    b.status = '🎨 création du poster…'; batchRender();
                    const saved = await batchPost('/api/save', {
                        poster_url: b.poster.url, title: b.item.title,
                        border_style: document.getElementById('borderAnime').checked ? 'anime' : 'standard',
                        show_gradient: document.getElementById('showGradient').checked,
                        text_1: '', text_2: b.l2, text_3: b.l3, text_4: '', text_5: '', season: '', crop_x: 0.5, crop_y: 0.5, zoom: 1});
                    if (saved.error) throw new Error(saved.error);
                    b.saved = saved.filename;
                    b.status = '🔎 recherche du dossier…'; batchRender();
                    const m = await batchPost('/api/library/match', {media_type: b.item.media_type, tmdb_id: b.item.id, title: b.item.title,
                        original_title: b.item.original_title || '', year: b.item.year, season_text: ''});
                    if (m.error) throw new Error(m.error);
                    if (!m.selected || !['sur', 'memorise'].includes(m.confidence)) {
                        b.status = '✋ dossier incertain : poster enregistré (' + saved.filename + '), à envoyer à la main'; manual++; batchRender(); continue;
                    }
                    const cand = (m.candidates || []).find(c => c.rel === m.selected) || {};
                    const r = await batchPost('/api/library/apply', {saved_filename: saved.filename, rel: m.selected, action: 'replace',
                        media_type: b.item.media_type, target_type: cand.media_type || b.item.media_type, tmdb_id: b.item.id,
                        title: b.item.title, original_title: b.item.original_title || '', refresh_emby: true, season_text: '', episode_text: ''});
                    if (r.error) throw new Error(r.error);
                    b.done = true; sent++;
                    b.status = '✅ ' + r.written + (r.backup ? ' (ancien sauvegardé)' : '') + (r.emby ? (r.emby.ok ? ' · 🔄 Emby' : ' · ⚠️ ' + r.emby.message) : '');
                } catch (err) { b.status = '❌ ' + err.message; errs++; }
                batchRender();
            }
            btn.disabled = false;
            fetch('/api/notif/lot', {method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({envoyes: sent, manuels: manual, erreurs: errs})}).catch(() => {});
            showMessage('Lot terminé : ' + sent + ' envoyé(s)' + (manual ? ', ' + manual + ' à faire à la main' : ''), sent ? 'success' : 'error');
        }

        // ===== Mise en page mémorisée : reprendre un poster plus tard =====
        const LAYOUT_CHECKS = ['borderAnime', 'showGradient', 'text1Enabled', 'text2Enabled', 'text3Enabled', 'text4Enabled', 'text5Enabled', 'seasonEnabled', 'seasonSpecial'];
        const LAYOUT_VALUES = ['text1', 'text2', 'text3', 'text4', 'text5', 'season', 'cropX', 'cropY', 'zoom'];
        function captureLayout() {
            const l = {poster: selectedPoster, checks: {}, values: {}};
            LAYOUT_CHECKS.forEach(id => { l.checks[id] = document.getElementById(id).checked; });
            LAYOUT_VALUES.forEach(id => { l.values[id] = document.getElementById(id).value; });
            return l;
        }
        function applyLayout(l) {
            Object.entries(l.checks || {}).forEach(([id, v]) => { const el = document.getElementById(id); if (el) el.checked = !!v; });
            Object.entries(l.values || {}).forEach(([id, v]) => { const el = document.getElementById(id); if (el) { el.value = v; el.dispatchEvent(new Event('input')); } });
            const en = document.getElementById('seasonEnabled');
            document.getElementById('season').disabled = !en.checked || document.getElementById('seasonSpecial').checked;
            if (l.poster) { selectedPoster = l.poster; document.querySelectorAll('.poster-thumb').forEach(el => el.classList.toggle('selected', el.src === l.poster.url)); }
            generatePreview();
        }
        async function rememberLayout() {
            if (!selectedItem || !selectedPoster || selectedItem.libre) return;
            try {
                await fetch('/api/layout', {method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({tmdb_id: selectedItem.id, media_type: selectedItem.media_type, layout: captureLayout()})});
            } catch (e) {}
        }
        async function offerLayout(item) {
            const bar = document.getElementById('layoutBar');
            bar.innerHTML = '';
            let data = {};
            try { data = await (await fetch('/api/layout?tmdb_id=' + encodeURIComponent(item.id) + '&media_type=' + encodeURIComponent(item.media_type))).json(); } catch (e) { return; }
            if (!data.layout || selectedItem !== item) return;
            const b = document.createElement('button');
            b.type = 'button'; b.className = 'secondary layout-btn';
            b.textContent = '♻️ Reprendre la dernière mise en page' + (data.layout.saved ? ' (' + data.layout.saved + ')' : '');
            b.onclick = () => { applyLayout(data.layout); bar.innerHTML = ''; showMessage('Mise en page reprise : vérifie l’aperçu', 'success'); };
            bar.appendChild(b);
        }

        async function loadPosters(item) {
            offerLayout(item);
            selectedPoster = null;              // nouveau titre : l'affiche de l'ancien titre n'est plus choisie
            document.getElementById('previewImage').innerHTML = '<div class="preview-placeholder">Aperçu</div>';
            document.getElementById('postersLoading').classList.add('show');
            document.getElementById('posters').innerHTML = '';
            const sel = document.getElementById('langSelect');
            sel.style.display = 'none';
            sel.innerHTML = '';
            allPosters = [];

            try {
                const res = await fetch('/api/posters', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        id: item.id,
                        media_type: item.media_type
                    })
                });
                const data = await res.json();

                if (data.error) {
                    showMessage(data.error, 'error');
                    return;
                }

                allPosters = data.posters;
                if (!allPosters.length) {
                    showMessage('Aucun poster trouvé pour ce titre. Tu peux envoyer ta propre image.', 'error');
                    return;
                }

                const addOpt = (value, text) => {
                    const o = document.createElement('option');
                    o.value = value;
                    o.textContent = text;
                    sel.appendChild(o);
                };
                data.languages.forEach(l => addOpt(l.code, langLabel(l.code) + ' (' + l.count + ')'));
                addOpt('all', 'Toutes les langues (' + allPosters.length + ')');
                sel.value = data.default;
                sel.style.display = 'block';
                showPosters(data.default);

                if (data.default !== '') {
                    showMessage('Aucun poster sans texte : choisis une autre langue dans la liste si besoin.', 'error');
                }
            } finally {
                document.getElementById('postersLoading').classList.remove('show');
            }
        }

        document.getElementById('langSelect').addEventListener('change', (e) => showPosters(e.target.value));

        // ===== Effacer un élément (logo, texte) =====
        const eraseState = {url: null, history: [], mask: null, drawing: false, last: null, dirty: false};

        function eraseDisplayUrl(url) {
            return url.replace('/w500/', '/w780/');
        }

        function eraseStatus(msg, kind) {
            const el = document.getElementById('eraseStatus');
            el.style.display = msg ? 'block' : 'none';
            el.textContent = msg || '';
            el.className = 'lib-status' + (kind ? ' ' + kind : '');
        }

        function eraseSetupCanvas() {
            const img = document.getElementById('eraseImg');
            const canvas = document.getElementById('eraseCanvas');
            const scale = Math.min(1, 900 / img.naturalWidth);
            canvas.width = Math.round(img.naturalWidth * scale);
            canvas.height = Math.round(img.naturalHeight * scale);
            eraseState.mask = document.createElement('canvas');
            eraseState.mask.width = canvas.width;
            eraseState.mask.height = canvas.height;
            const mctx = eraseState.mask.getContext('2d');
            mctx.fillStyle = '#000';
            mctx.fillRect(0, 0, canvas.width, canvas.height);
            eraseState.dirty = false;
        }

        function eraseClearMask() {
            const canvas = document.getElementById('eraseCanvas');
            canvas.getContext('2d').clearRect(0, 0, canvas.width, canvas.height);
            if (eraseState.mask) eraseSetupCanvas();
        }

        function erasePoint(ev) {
            const canvas = document.getElementById('eraseCanvas');
            const r = canvas.getBoundingClientRect();
            return {x: (ev.clientX - r.left) * canvas.width / r.width, y: (ev.clientY - r.top) * canvas.height / r.height};
        }

        function eraseStroke(from, to) {
            const canvas = document.getElementById('eraseCanvas');
            const size = parseInt(document.getElementById('eraseBrush').value, 10) * canvas.width / 200;
            const draw = (ctx, color) => {
                ctx.strokeStyle = color;
                ctx.fillStyle = color;
                ctx.lineWidth = size * 2;
                ctx.lineCap = 'round';
                ctx.lineJoin = 'round';
                ctx.beginPath();
                ctx.moveTo(from.x, from.y);
                ctx.lineTo(to.x, to.y);
                ctx.stroke();
                ctx.beginPath();
                ctx.arc(to.x, to.y, size, 0, Math.PI * 2);
                ctx.fill();
            };
            draw(canvas.getContext('2d'), 'rgba(255, 40, 40, 0.55)');
            draw(eraseState.mask.getContext('2d'), '#fff');
            eraseState.dirty = true;
        }

        function openErase() {
            if (!selectedPoster) {
                showMessage("Choisis d'abord un poster (ou envoie ton image).", 'error');
                return;
            }
            eraseState.history = [];
            eraseState.url = selectedPoster.url;
            eraseState.original = selectedPoster;
            eraseStatus('');
            document.getElementById('eraseUndo').disabled = true;
            document.getElementById('eraseModal').classList.add('show');
            document.body.style.overflow = 'hidden';
            eraseLoadImage();
        }

        function eraseLoadImage() {
            const img = document.getElementById('eraseImg');
            img.onload = () => { eraseSetupCanvas(); document.getElementById('eraseCanvas').getContext('2d').clearRect(0, 0, 9999, 9999); };
            img.src = eraseDisplayUrl(eraseState.url);
        }

        function closeErase() {
            document.getElementById('eraseModal').classList.remove('show');
            document.body.style.overflow = '';
        }

        async function eraseApply() {
            if (!eraseState.dirty) { eraseStatus("Colorie d'abord la zone à effacer.", 'warn'); return; }
            const btns = document.querySelectorAll('#eraseModal .lib-actions button');
            btns.forEach(b => b.disabled = true);
            eraseStatus('Effacement en cours...');
            try {
                const res = await fetch('/api/erase', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({poster_url: eraseState.url, mask: eraseState.mask.toDataURL('image/png')})
                });
                const data = await res.json();
                if (data.error) { eraseStatus('❌ ' + data.error, 'err'); return; }
                eraseState.history.push(eraseState.url);
                eraseState.url = data.url;
                eraseStatus('✅ Effacé (' + (data.engine || '') + '). Tu peux en effacer un autre, ou cliquer sur « Utiliser ce poster ».' + (data.note ? ' ⚠️ ' + data.note + ' (détails : bouton « Journal » en haut de la page)' : ''), data.note ? 'warn' : 'ok');
                eraseLoadImage();
            } catch (err) {
                eraseStatus('❌ ' + err.message, 'err');
            } finally {
                btns.forEach(b => b.disabled = false);
                document.getElementById('eraseUndo').disabled = eraseState.history.length === 0;
            }
        }

        function eraseUndo() {
            if (!eraseState.history.length) return;
            eraseState.url = eraseState.history.pop();
            eraseStatus('');
            document.getElementById('eraseUndo').disabled = eraseState.history.length === 0;
            eraseLoadImage();
        }

        function eraseUse() {
            if (eraseState.url === eraseState.original.url) { closeErase(); return; }
            const poster = {url: eraseState.url, lang: '', upload: true};
            const postersDiv = document.getElementById('posters');
            const img = document.createElement('img');
            img.src = eraseState.url;
            img.title = 'Poster nettoyé';
            img.className = 'poster-thumb';
            img.onclick = () => { selectPoster(poster); img.classList.add('selected'); };
            postersDiv.insertBefore(img, postersDiv.firstChild);
            selectPoster(poster);
            img.classList.add('selected');
            closeErase();
        }

        (function setupEraseEvents() {
            const canvas = document.getElementById('eraseCanvas');
            canvas.addEventListener('pointerdown', (ev) => {
                if (!eraseState.mask) return;
                ev.preventDefault();
                canvas.setPointerCapture(ev.pointerId);
                eraseState.drawing = true;
                eraseState.last = erasePoint(ev);
                eraseStroke(eraseState.last, eraseState.last);
            });
            canvas.addEventListener('pointermove', (ev) => {
                if (!eraseState.drawing) return;
                ev.preventDefault();
                const p = erasePoint(ev);
                eraseStroke(eraseState.last, p);
                eraseState.last = p;
            });
            const stop = () => { eraseState.drawing = false; };
            canvas.addEventListener('pointerup', stop);
            canvas.addEventListener('pointercancel', stop);
            document.getElementById('eraseOpen').addEventListener('click', openErase);
            document.getElementById('eraseApply').addEventListener('click', eraseApply);
            document.getElementById('eraseClear').addEventListener('click', () => { eraseClearMask(); eraseStatus(''); });
            document.getElementById('eraseUndo').addEventListener('click', eraseUndo);
            document.getElementById('eraseUse').addEventListener('click', eraseUse);
        })();

        // Ligne 2 (titre principal) : remplie avec le nom choisi si elle est vide ou si elle contient encore
        // le nom mis automatiquement la fois d'avant (enchaînement de posters) ; jamais si tu l'as modifiée à la main
        function preremplirTitre(nom) {
            const champ = document.getElementById('text2'), case2 = document.getElementById('text2Enabled');
            const auto = champ.value.trim() === '' || champ.value === (champ.dataset.auto || '');
            if (!nom || !auto) return;
            champ.value = nom;
            champ.dataset.auto = nom;
            if (!case2.checked) { case2.checked = true; case2.dispatchEvent(new Event('change')); }
        }

        // Titre « libre » : pas de fiche TheMovieDB, juste un nom
        function elementLibre() {
            let nom = (mfgVenue.serie || document.getElementById('searchQuery').value || '').trim();
            if (!nom) nom = (prompt('Nom du manga, du film ou de la série (pour nommer le poster) :') || '').trim();
            if (!nom) return null;
            const item = {id: null, title: nom, original_title: '', media_type: 'tv', year: '?', libre: true};
            preremplirTitre(nom);
            const res = document.getElementById('results');
            res.innerHTML = '';
            const div = document.createElement('div');
            div.className = 'result-item active';
            div.textContent = '✏️ ' + nom + ' · sans recherche (ton image)';
            res.appendChild(div);
            return item;
        }

        // Envoi manuel d'une image
        document.getElementById('uploadFile').addEventListener('change', async (e) => {
            const file = e.target.files[0];
            e.target.value = '';
            if (!file) return;
            if (!selectedItem) {
                // Sans recherche (manga sans anime, image perso) : le nom vient de MouFlanga, de la recherche tapée, ou on le demande
                selectedItem = elementLibre();
                if (!selectedItem) { showMessage("Indique le nom (dans la recherche) pour nommer le poster.", 'error'); return; }
            }
            document.getElementById('postersLoading').classList.add('show');
            try {
                const form = new FormData();
                form.append('file', file);
                const res = await fetch('/api/upload', {method: 'POST', body: form});
                const data = await res.json();
                if (data.error) { showMessage(data.error, 'error'); return; }

                const poster = {url: data.url, lang: '', upload: true};
                const postersDiv = document.getElementById('posters');
                const img = document.createElement('img');
                img.src = data.url;
                img.title = 'Mon image';
                img.className = 'poster-thumb';
                img.onclick = () => { selectPoster(poster); img.classList.add('selected'); };
                postersDiv.insertBefore(img, postersDiv.firstChild);
                selectPoster(poster);
                img.classList.add('selected');
            } catch (err) {
                showMessage('Erreur: ' + err.message, 'error');
            } finally {
                document.getElementById('postersLoading').classList.remove('show');
            }
        });

        let previewInFlight = false, previewQueued = false, previewOverflow = [0, 0];

        async function generatePreview(quiet) {
            if (!selectedPoster || !selectedItem) return;
            if (previewInFlight) { previewQueued = true; return; }
            previewInFlight = true;

            if (quiet !== true) document.getElementById('previewLoading').classList.add('show');

            try {
                const res = await fetch('/api/preview', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        poster_url: selectedPoster.url,
                        title: selectedItem.title,
                        border_style: document.getElementById('borderAnime').checked ? 'anime' : 'standard',
                        show_gradient: document.getElementById('showGradient').checked,
                        text_1: document.getElementById('text1Enabled').checked ? document.getElementById('text1').value : '',
                        text_2: document.getElementById('text2Enabled').checked ? document.getElementById('text2').value : '',
                        text_3: document.getElementById('text3Enabled').checked ? document.getElementById('text3').value : '',
                        text_4: document.getElementById('text4Enabled').checked ? document.getElementById('text4').value : '',
                        text_5: document.getElementById('text5Enabled').checked ? document.getElementById('text5').value : '',
                        season: seasonForPoster(),
                        crop_x: document.getElementById('cropX').value / 100,
                        crop_y: document.getElementById('cropY').value / 100,
                        zoom: document.getElementById('zoom').value / 100
                    })
                });
                const data = await res.json();

                if (data.error) {
                    showMessage(data.error, 'error');
                    return;
                }

                const previewDiv = document.getElementById('previewImage');
                previewDiv.innerHTML = `<img src="${data.preview}" draggable="false" />`;
                previewOverflow = data.overflow || [0, 0];
                previewDiv.classList.toggle('draggable', previewOverflow[0] > 1 || previewOverflow[1] > 1);
                if (quiet !== true) showMessage('Aperçu généré', 'success');
            } catch (err) {
                showMessage('Erreur: ' + err.message, 'error');
            } finally {
                document.getElementById('previewLoading').classList.remove('show');
                previewInFlight = false;
                if (previewQueued) { previewQueued = false; generatePreview(true); }
            }
        }

        // Enregistre le poster sur le serveur (dossier de sortie). Retourne la réponse ou null.
        async function saveToServer() {
            if (!selectedPoster || !selectedItem) {
                showMessage('Sélectionnez un poster', 'error');
                return null;
            }

            try {
                const res = await fetch('/api/save', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        poster_url: selectedPoster.url,
                        title: selectedItem.title,
                        border_style: document.getElementById('borderAnime').checked ? 'anime' : 'standard',
                        show_gradient: document.getElementById('showGradient').checked,
                        text_1: document.getElementById('text1Enabled').checked ? document.getElementById('text1').value : '',
                        text_2: document.getElementById('text2Enabled').checked ? document.getElementById('text2').value : '',
                        text_3: document.getElementById('text3Enabled').checked ? document.getElementById('text3').value : '',
                        text_4: document.getElementById('text4Enabled').checked ? document.getElementById('text4').value : '',
                        text_5: document.getElementById('text5Enabled').checked ? document.getElementById('text5').value : '',
                        season: seasonForPoster(),
                        crop_x: document.getElementById('cropX').value / 100,
                        crop_y: document.getElementById('cropY').value / 100,
                        zoom: document.getElementById('zoom').value / 100
                    })
                });
                const data = await res.json();

                if (data.error) {
                    showMessage('Erreur: ' + data.error, 'error');
                    return null;
                }
                rememberLayout();          // pour pouvoir reprendre ce poster plus tard
                return data;
            } catch (err) {
                showMessage('Erreur: ' + err.message, 'error');
                return null;
            }
        }

        async function savePoster() {
            const data = await saveToServer();
            if (!data) return;

            showMessage('✅ Sauvegardé: ' + data.filename, 'success');

            // Télécharge aussi le fichier sur l'appareil (téléphone/PC)
            const a = document.createElement('a');
            a.href = data.download_url;
            a.download = data.filename;
            document.body.appendChild(a);
            a.click();
            a.remove();
        }

        // ===== Envoi vers la médiathèque =====
        let libState = {saved: null};

        async function sendToLibrary() {
            const data = await saveToServer();
            if (!data) return;
            if (selectedItem && selectedItem.media_type === 'collection') openSaga(data.filename);   // saga : directement dans Emby
            else openLibrary(data.filename);
        }

        // ===== MouFlanga : le poster devient la couverture d'une série de mangas =====
        let mfgState = {saved: null, series: []};
        // Ouverture depuis MouFlanga (« Créer avec MouFloster ») : ?mouflanga=<série>&q=<recherche>&retour=<adresse>
        const mfgVenue = (() => {
            const p = new URLSearchParams(location.search), retour = p.get('retour') || '';
            return {serie: p.get('mouflanga') || '', q: p.get('q') || '', retour: /^https?:\/\//i.test(retour) ? retour : ''};
        })();
        function mfgBandeau(fini) {
            const b = document.getElementById('mfgBandeau');
            if (!mfgVenue.serie) { b.hidden = true; return; }
            b.hidden = false;
            document.getElementById('mfgBandeauTexte').textContent = fini
                ? '✅ Couverture de « ' + mfgVenue.serie + ' » envoyée dans MouFlanga.'
                : '📚 Couverture pour « ' + mfgVenue.serie + ' » (MouFlanga) : choisis une affiche, personnalise-la, puis « 📚 Couverture dans MouFlanga ».';
            const a = document.getElementById('mfgRetour');
            a.hidden = !mfgVenue.retour; a.href = mfgVenue.retour;
            a.textContent = fini ? '↩ Retour à MouFlanga' : '↩ Annuler et revenir';
        }
        document.addEventListener('DOMContentLoaded', () => {
            if (!mfgVenue.serie) return;
            mfgBandeau(false);
            if (mfgVenue.q) { document.getElementById('searchQuery').value = mfgVenue.q; search(); }
        });
        function mfgStatus(msg, kind) { const el = document.getElementById('mfgStatus'); el.textContent = msg; el.className = 'lib-status' + (kind ? ' ' + kind : ''); }
        async function sendToMouflanga() {
            const data = await saveToServer();
            if (!data) return;
            mfgState = {saved: data.filename, series: []};
            document.getElementById('mfgNewImg').src = '/api/download/' + encodeURIComponent(data.filename) + '?inline=1&t=' + Date.now();
            document.getElementById('mfgSelect').innerHTML = '';
            document.getElementById('mfgOldFrame').innerHTML = '';
            document.getElementById('mfgApply').disabled = true;
            document.getElementById('mfgModal').classList.add('show');
            document.body.style.overflow = 'hidden';
            mfgStatus('Lecture de la bibliothèque MouFlanga...');
            try {
                const res = await fetch('/api/mouflanga/series', {method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({title: selectedItem.title, original_title: selectedItem.original_title || '', cible: mfgVenue.serie})});
                const r = await res.json();
                if (!res.ok || r.error) throw new Error(r.error || 'Erreur ' + res.status);
                if (!r.series.length) { mfgStatus('Aucune série dans MouFlanga pour le moment.', 'err'); return; }
                mfgState.series = r.series;
                const sel = document.getElementById('mfgSelect');
                r.series.forEach(x => { const o = document.createElement('option'); o.value = x.name; o.textContent = x.name + (x.name === r.selected ? '  ✓' : ''); sel.appendChild(o); });
                if (r.selected) sel.value = r.selected;
                mfgStatus(r.selected ? 'Série trouvée : vérifie puis valide.' : 'Choisis la série dans la liste.', r.selected ? 'ok' : '');
                document.getElementById('mfgApply').disabled = false;
                mfgShowOld();
            } catch (err) { mfgStatus('❌ ' + err.message, 'err'); }
        }
        function mfgShowOld() {
            const nom = document.getElementById('mfgSelect').value, frame = document.getElementById('mfgOldFrame');
            const s = mfgState.series.find(x => x.name === nom);
            frame.innerHTML = '';
            if (s && s.cover) { const img = document.createElement('img'); img.alt = 'Couverture actuelle'; img.src = '/api/mouflanga/cover?name=' + encodeURIComponent(nom) + '&t=' + Date.now(); frame.appendChild(img); }
            else frame.innerHTML = '<div class="empty">Couverture automatique<br>(1re page du 1er chapitre)</div>';
        }
        async function mfgApply() {
            const nom = document.getElementById('mfgSelect').value, btn = document.getElementById('mfgApply');
            if (!nom) return;
            btn.disabled = true; mfgStatus('Envoi...');
            try {
                const res = await fetch('/api/mouflanga/apply', {method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({saved_filename: mfgState.saved, name: nom})});
                const r = await res.json();
                if (!res.ok || r.error) throw new Error(r.error || 'Erreur ' + res.status);
                closeMouflanga();
                showMessage('📚 Couverture de « ' + nom + ' » changée dans MouFlanga' + (r.backup ? ' (ancienne sauvegardée)' : ''), 'success');
                if (mfgVenue.retour) mfgBandeau(true);
                mfgFini(nom, r.backup);
            } catch (err) { mfgStatus('❌ ' + err.message, 'err'); btn.disabled = false; }
        }
        // Fenêtre de fin : bien visible, avec le bouton pour revenir sur la série dans MouFlanga
        function mfgFini(nom, sauvegarde) {
            document.getElementById('mfgFiniTexte').textContent = 'La couverture de « ' + nom + ' » est dans MouFlanga' + (sauvegarde ? ' (l’ancienne est sauvegardée).' : '.');
            const a = document.getElementById('mfgFiniRetour');
            a.hidden = !mfgVenue.retour; if (mfgVenue.retour) a.href = mfgVenue.retour;
            document.getElementById('mfgFiniFermer').textContent = mfgVenue.retour ? 'Rester dans MouFloster' : 'OK';
            document.getElementById('mfgFiniModal').classList.add('show');
        }
        function closeMouflanga() {
            document.getElementById('mfgModal').classList.remove('show');
            document.body.style.overflow = '';
        }

        // ===== Sagas : l'affiche est envoyée directement à Emby (collection) =====
        let sagaState = {saved: null, cands: []};
        function sagaStatus(msg, kind) { const el = document.getElementById('sagaStatus'); el.textContent = msg; el.className = 'lib-status' + (kind ? ' ' + kind : ''); }
        function sagaFill(cands, selected) {
            sagaState.cands = cands;
            const sel = document.getElementById('sagaSelect'); sel.innerHTML = '';
            cands.forEach(c => { const o = document.createElement('option'); o.value = c.id; o.textContent = c.name + (c.sure ? '  ✓' : (c.score < 1 ? '  (' + Math.round(c.score * 100) + '%)' : '')); sel.appendChild(o); });
            if (selected) sel.value = selected;
            sagaShowOld();
        }
        function sagaShowOld() {
            const id = document.getElementById('sagaSelect').value, frame = document.getElementById('sagaOldFrame');
            document.getElementById('sagaApply').disabled = !id;
            if (!id) { frame.innerHTML = '<div class="empty">Aucune collection choisie</div>'; return; }
            const img = document.createElement('img'); img.alt = 'Affiche actuelle';
            img.onerror = () => { frame.innerHTML = '<div class="empty">Pas encore d’affiche dans Emby</div>'; };
            img.src = '/api/saga/image/' + encodeURIComponent(id) + '?t=' + Date.now();
            frame.innerHTML = ''; frame.appendChild(img);
        }
        async function sagaFind(q) {
            sagaStatus('Recherche de la collection dans Emby...');
            try {
                const res = await fetch('/api/saga/match', {method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({tmdb_id: selectedItem.id, title: selectedItem.title, original_title: selectedItem.original_title || '', q: q || ''})});
                const data = await res.json();
                if (data.error) { sagaFill([], null); return sagaStatus('❌ ' + data.error, 'err'); }
                sagaFill(data.candidates || [], data.selected);
                if (!(data.candidates || []).length) sagaStatus('⚠️ Aucune collection trouvée dans Emby. Vérifie qu’elle existe (Emby → collections), ou cherche-la par son nom ci-dessous.', 'warn');
                else if (data.sure) sagaStatus('✅ Collection trouvée. Vérifie puis confirme.', 'ok');
                else sagaStatus('⚠️ Vérifie que c’est la bonne collection, ou cherche-en une autre ci-dessous.', 'warn');
            } catch (err) { sagaStatus('❌ ' + err.message, 'err'); }
        }
        function openSaga(savedFilename) {
            sagaState = {saved: savedFilename, cands: []};
            document.getElementById('sagaTitle').textContent = selectedItem.title + ' · saga';
            document.getElementById('sagaNewImg').src = '/api/download/' + encodeURIComponent(savedFilename) + '?inline=1&t=' + Date.now();
            document.getElementById('sagaSearch').value = '';
            document.getElementById('sagaModal').classList.add('show');
            document.body.style.overflow = 'hidden';
            sagaFind('');
        }
        function closeSaga() { document.getElementById('sagaModal').classList.remove('show'); document.body.style.overflow = ''; }
        async function sagaApply() {
            const id = document.getElementById('sagaSelect').value; if (!id) return;
            const c = sagaState.cands.find(x => x.id === id) || {name: selectedItem.title};
            document.getElementById('sagaApply').disabled = true;
            sagaStatus('Envoi de l’affiche à Emby...');
            try {
                const res = await fetch('/api/saga/apply', {method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({saved_filename: sagaState.saved, item_id: id, name: c.name})});
                const data = await res.json();
                if (data.error) { document.getElementById('sagaApply').disabled = false; return sagaStatus('❌ ' + data.error, 'err'); }
                closeSaga();
                showMessage('✅ ' + data.message + ' · ton poster est aussi conservé : ' + sagaState.saved, 'success');
            } catch (err) { document.getElementById('sagaApply').disabled = false; sagaStatus('❌ ' + err.message, 'err'); }
        }

        function libEpisodeText() {
            return libType() === 'tv' ? document.getElementById('libEpisode').value : '';
        }

        function libSeasonText() {
            return document.getElementById('libSeason').value;
        }

        function libSetStatus(msg, kind) {
            const el = document.getElementById('libStatus');
            el.textContent = msg;
            el.className = 'lib-status' + (kind ? ' ' + kind : '');
        }

        function libFill(candidates, selected) {
            const sel = document.getElementById('libFolder');
            sel.innerHTML = '';
            candidates.forEach(c => {
                const o = document.createElement('option');
                o.value = c.rel;
                o.dataset.mt = c.media_type || '';
                const pct = c.score >= 1 ? '' : '  (' + Math.round(c.score * 100) + '%)';
                o.textContent = c.family + ' / ' + c.name + pct;
                sel.appendChild(o);
            });
            if (selected) sel.value = selected;
            return candidates.length > 0;
        }

        async function openLibrary(savedFilename) {
            libState = {saved: savedFilename, touched: false};
            const item = selectedItem;
            const isTv = item.media_type === 'tv';

            document.getElementById('libTitle').textContent =
                item.title + (item.year && item.year !== '?' ? ' (' + item.year + ')' : '') + (isTv ? ' · série' : ' · film');
            document.getElementById('libSeasonRow').style.display = isTv ? 'block' : 'none';

            const seasonTxt = document.getElementById('seasonSpecial').checked ? '0' : (document.getElementById('seasonEnabled').checked ? document.getElementById('season').value : '');
            const m = seasonTxt.match(/[0-9]+/);
            document.getElementById('libSeason').value = m ? m[0] : '';
            document.getElementById('libSearch').value = '';
            document.getElementById('libEpisode').value = '';
            document.getElementById('libNewImg').src = '/api/download/' + encodeURIComponent(savedFilename) + '?inline=1&t=' + Date.now();
            document.getElementById('libActions').innerHTML = '';
            document.getElementById('libOldFrame').innerHTML = '';
            document.getElementById('libOldCap').textContent = '';
            document.getElementById('libTarget').textContent = '';
            document.getElementById('libOthers').textContent = '';
            document.getElementById('libFolder').innerHTML = '';
            document.getElementById('libModal').classList.add('show');
            document.body.style.overflow = 'hidden';
            libSetStatus('Recherche du dossier dans la médiathèque...');

            try {
                const res = await fetch('/api/library/match', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        media_type: item.media_type,
                        tmdb_id: item.id,
                        title: item.title,
                        original_title: item.original_title || '',
                        year: item.year,
                        season_text: isTv ? libSeasonText() : ''
                    })
                });
                const data = await res.json();
                if (data.error) { libSetStatus('❌ ' + data.error, 'err'); return; }

                document.getElementById('libEmbyRow').style.display = data.emby_configured ? 'block' : 'none';
                const has = libFill(data.candidates, data.selected);
                libSyncType();
                if (data.confidence === 'memorise') {
                    libSetStatus('✅ Dossier mémorisé pour ce titre. Vérifie puis confirme.', 'ok');
                } else if (data.confidence === 'sur') {
                    libSetStatus('✅ Dossier trouvé. Vérifie puis confirme.', 'ok');
                } else if (data.confidence === 'incertain') {
                    libSetStatus('⚠️ Je ne suis pas sûr du dossier : vérifie-le, ou cherche-en un autre ci-dessous.', 'warn');
                } else {
                    libSetStatus('⚠️ Aucun dossier trouvé : cherche-le avec le champ ci-dessous.', 'warn');
                }
                if (has) libRender(data.target_name, data.inspect); else libRender('', null);
            } catch (err) {
                libSetStatus('❌ ' + err.message, 'err');
            }
        }

        async function libSearchFolders() {
            const q = document.getElementById('libSearch').value.trim();
            if (q.length < 2) return;
            libSetStatus('Recherche...');
            let data = {};
            try {
                const res = await fetch('/api/library/search', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({q, media_type: selectedItem.media_type})
                });
                data = await res.json();
            } catch (e) {
                return libSetStatus('Recherche impossible (serveur injoignable ?)', 'err');
            }
            const cands = data.candidates || [];
            if (data.error) return libSetStatus(data.error, 'err');
            if (libFill(cands, cands[0] && cands[0].rel)) {
                libSetStatus('Choisis le bon dossier dans la liste.', 'warn');
                libInspect();
            } else {
                libSetStatus('Aucun dossier trouvé pour « ' + q + ' ».', 'err');
                libRender('', null);
            }
        }

        // Type du dossier choisi (un film peut être rangé dans une série: spéciaux)
        function libType() {
            const sel = document.getElementById('libFolder');
            const opt = sel.options[sel.selectedIndex];
            return (opt && opt.dataset.mt) || selectedItem.media_type;
        }
        function libSyncType() {
            const tv = libType() === 'tv';
            document.getElementById('libSeasonRow').style.display = tv ? 'block' : 'none';
            const f = document.getElementById('libSeason');
            if (tv && selectedItem.media_type !== 'tv' && f.value === '' && !libState.touched) f.value = '0';
        }

        async function libInspect() {
            libSyncType();
            const rel = document.getElementById('libFolder').value;
            if (!rel) { libRender('', null); return; }
            const res = await fetch('/api/library/inspect', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    rel,
                    media_type: libType(),
                    season_text: libType() === 'tv' ? libSeasonText() : '',
                    episode_text: libEpisodeText()
                })
            });
            const data = await res.json();
            if (data.error) { libSetStatus('❌ ' + data.error, 'err'); libRender('', null); return; }
            libRender(data.target_name, data.inspect);
            if (data.video) libSetStatus('🎞️ Image de la vidéo : ' + data.video, 'ok');
        }

        function libSize(bytes) {
            return bytes > 1048576 ? (bytes / 1048576).toFixed(1) + ' Mo' : Math.round(bytes / 1024) + ' Ko';
        }

        function libRender(targetName, info) {
            const rel = document.getElementById('libFolder').value;
            const frame = document.getElementById('libOldFrame');
            const cap = document.getElementById('libOldCap');
            const actions = document.getElementById('libActions');
            frame.innerHTML = '';
            actions.innerHTML = '';
            document.getElementById('libTarget').textContent = targetName || '';
            document.getElementById('libOthers').textContent =
                info && info.others && info.others.length ? '⚠️ existe aussi : ' + info.others.join(', ') : '';

            if (!rel || !info) {
                cap.innerHTML = '<b>Actuel</b>';
                frame.innerHTML = '<div class="empty">Choisis un dossier</div>';
                return;
            }

            const add = (label, cls, fn) => {
                const b = document.createElement('button');
                b.textContent = label;
                if (cls) b.className = cls;
                b.onclick = fn;
                actions.appendChild(b);
                return b;
            };

            if (info.exists) {
                const img = document.createElement('img');
                img.alt = 'Poster actuel';
                img.src = '/api/library/file?rel=' + encodeURIComponent(rel) + '&name=' + encodeURIComponent(targetName) + '&sub=' + encodeURIComponent(info.sub || '') + (info.episode ? '&ep=1' : '') + '&v=' + info.mtime_raw;
                frame.appendChild(img);
                cap.innerHTML = '<b>Actuel</b><br>' + info.mtime + '<br>' + (info.dimensions ? info.dimensions + ' · ' : '') + libSize(info.size);
                libSetStatus('⚠️ Un poster existe déjà ici. Compare les deux avant de décider.', 'warn');
                add('Remplacer (ancien poster sauvegardé)', 'danger', () => libApply('replace'));
                add('Garder les deux', 'secondary', () => libApply('both'));
            } else {
                frame.innerHTML = '<div class="empty">Aucun poster existant</div>';
                cap.innerHTML = '<b>Actuel</b><br>(rien à cet emplacement)';
                add('Copier ici', 'success', () => libApply('copy'));
            }
            add('Annuler', 'secondary', closeLibrary);
            libLoadBackups(rel);
        }

        // Anciens posters de cet emplacement (sauvegardés à chaque remplacement) : on peut en remettre un
        async function libLoadBackups(rel) {
            const box = document.getElementById('libBackups');
            box.innerHTML = '';
            const body = {rel, media_type: libType(), season_text: libType() === 'tv' ? libSeasonText() : '', episode_text: libEpisodeText()};
            let data = {};
            try { data = await (await fetch('/api/library/backups', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)})).json(); } catch (e) { return; }
            if (document.getElementById('libFolder').value !== rel || !(data.backups || []).length) return;
            box.innerHTML = '<div class="lib-field"><label>🕘 Anciens posters de cet emplacement (' + data.backups.length + ')</label><div class="lib-backups"></div></div>';
            const list = box.querySelector('.lib-backups');
            data.backups.forEach(b => {
                const fig = document.createElement('figure');
                const img = document.createElement('img'); img.loading = 'lazy'; img.alt = 'Ancien poster'; img.src = '/api/library/backup-file?id=' + encodeURIComponent(b.id);
                const cap = document.createElement('figcaption'); cap.textContent = b.date;
                const btn = document.createElement('button'); btn.className = 'secondary'; btn.textContent = '↩️ Restaurer';
                btn.onclick = () => libRestore(rel, b.id, btn);
                fig.append(img, cap, btn); list.appendChild(fig);
            });
        }
        async function libRestore(rel, id, btn) {
            if (!confirm('Remettre cet ancien poster ? Le poster actuel sera sauvegardé avant (rien n’est supprimé).')) return;
            btn.disabled = true; libSetStatus('Restauration en cours...');
            try {
                const res = await fetch('/api/library/restore', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({
                    rel, id, target_type: libType(), media_type: selectedItem.media_type, tmdb_id: selectedItem.id, title: selectedItem.title,
                    original_title: selectedItem.original_title || '', refresh_emby: document.getElementById('libEmby').checked,
                    season_text: libType() === 'tv' ? libSeasonText() : '', episode_text: libEpisodeText()})});
                const data = await res.json();
                if (data.error) { btn.disabled = false; return libSetStatus('❌ ' + data.error, 'err'); }
                let msg = '✅ Ancien poster remis : ' + data.written + (data.backup ? ' (le poster remplacé est sauvegardé)' : '');
                if (data.emby) msg += (data.emby.ok ? ' — 🔄 ' : ' — ⚠️ ') + data.emby.message;
                await libInspect();
                libSetStatus(msg, 'ok');
            } catch (err) { btn.disabled = false; libSetStatus('❌ ' + err.message, 'err'); }
        }

        async function libApply(action) {
            const rel = document.getElementById('libFolder').value;
            if (!rel) return;
            document.querySelectorAll('#libActions button').forEach(b => b.disabled = true);
            libSetStatus('Copie en cours...');
            try {
                const res = await fetch('/api/library/apply', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        saved_filename: libState.saved,
                        rel,
                        action,
                        media_type: selectedItem.media_type,
                        target_type: libType(),
                        tmdb_id: selectedItem.id,
                        title: selectedItem.title,
                        original_title: selectedItem.original_title || '',
                        refresh_emby: document.getElementById('libEmby').checked,
                        season_text: libType() === 'tv' ? libSeasonText() : '',
                        episode_text: libEpisodeText()
                    })
                });
                const data = await res.json();
                if (data.error) {
                    libSetStatus('❌ ' + data.error, 'err');
                    document.querySelectorAll('#libActions button').forEach(b => b.disabled = false);
                    return;
                }
                closeLibrary();
                let msg = '✅ Copié : ' + data.written + (data.backup ? ' (ancien poster sauvegardé)' : '') + ' · ton poster est aussi conservé : ' + libState.saved;
                let kind = 'success';
                if (data.emby) {
                    msg += (data.emby.ok ? ' — 🔄 ' : ' — ⚠️ ') + data.emby.message;
                    if (!data.emby.ok) kind = 'error';
                }
                showMessage(msg, kind);
            } catch (err) {
                libSetStatus('❌ ' + err.message, 'err');
                document.querySelectorAll('#libActions button').forEach(b => b.disabled = false);
            }
        }

        function closeLibrary() {
            document.getElementById('libModal').classList.remove('show');
            document.body.style.overflow = '';
        }

        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') { closeLibrary(); closeErase(); closeSaga(); closeMouflanga(); }
        });
        document.addEventListener('DOMContentLoaded', () => {
            document.getElementById('libSearch').addEventListener('keypress', (e) => {
                if (e.key === 'Enter') libSearchFolders();
            });
        });

        function showMessage(msg, type) {
            const msgEl = document.getElementById('message');
            msgEl.textContent = msg;
            msgEl.className = 'message show ' + type;
            setTimeout(() => msgEl.classList.remove('show'), 9000);
        }

        // Enter to search
        document.getElementById('searchQuery').addEventListener('keypress', (e) => {
            if (e.key === 'Enter') search();
        });
    </script>
</body>
</html>
"""


if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("🎬 Démarrage Personnaliseur de Posters")
    logger.info("=" * 60)

    if TMDB_API_KEY == "TON_CLE_TMDB_ICI":
        logger.error("❌ TMDB_API_KEY non configurée!")
        logger.error("   Définis: export TMDB_API_KEY='ta-clé'")
        exit(1)

    logger.info(f"📁 Output: {OUTPUT_BASE}")
    logger.info("🌐 Ouvre: http://localhost:8000")
    notif.au_demarrage(BASE_DIR, APP_VERSION, Path(BASE_DIR) / "data" / "moufloster.log")   # seulement le vrai service

    app.run(debug=False, host="0.0.0.0", port=8000)
