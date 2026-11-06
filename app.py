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
import coherence   # 🎯 poster d'une saison : ressemblance avec les posters déjà dans la médiathèque
import comblage    # 🩹 zone effacée comblée avec les morceaux d'une 2e image (mêmes personnages, autre montage)
import mouflanga_link
import emby
import nouveautes   # 🆕 films, séries et saisons ajoutés dans Emby sans poster fait
import titre        # découpage du titre en lignes, petits mots en petit (taille des lettres gardée)
from functools import lru_cache

app = Flask(__name__)

# Version: base manuelle + numéro de déploiement (nombre de commits) + hash court
BASE_VERSION = "1.0"
DEPART_VERSION = 30          # nombre de commits au passage en 1.0 : la version repart de 1.0.0


def get_version():
    try:
        import subprocess
        cwd = str(Path(__file__).parent)
        count = subprocess.check_output(["git", "rev-list", "--count", "HEAD"], cwd=cwd, text=True, stderr=subprocess.DEVNULL).strip()
        short = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=cwd, text=True, stderr=subprocess.DEVNULL).strip()
        return f"v{BASE_VERSION}.{max(0, int(count) - DEPART_VERSION)} ({short})"
    except Exception:
        return f"v{BASE_VERSION}"


APP_VERSION = get_version()
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB max

import auth
auth.init_app(app, APP_VERSION)
import compression   # réponses texte compressées (gzip) : beaucoup moins de données au téléphone
compression.init_app(app)  # page de connexion + protection de toutes les routes
import apparence
apparence.init_app(app)     # skins et couleurs au choix, par compte
import comptes   # 👥 comptes « copain » : créateur de posters, envoi vers LEUR Emby (jamais la médiathèque de l'admin)
comptes.init_app(app, auth, Path(__file__).parent / "data")
auth.COPAIN_OK = {("GET", "/"), ("POST", "/api/search"), ("POST", "/api/posters"), ("POST", "/api/upload"), ("POST", "/api/erase"), ("POST", "/api/etendre"), ("POST", "/api/coherence/aligner_etendre"),
                  ("POST", "/api/preview"), ("POST", "/api/decoupe"), ("POST", "/api/save"), ("GET", "/api/layout"), ("POST", "/api/clientlog"), ("GET", "/api/moi"),
                  ("POST", "/api/copain/profil"), ("POST", "/api/copain/envoyer"), ("POST", "/api/copain/existant")}
auth.COPAIN_PREFIXES = (("GET", "/api/upload/"), ("GET", "/api/download/"), ("GET", "/api/copain/existant/"))
auth.COPAIN_OK |= {("GET", "/skin.css"), ("GET", "/api/apparence"), ("POST", "/api/apparence"), ("GET", "/apparence/reset")}

# ============================================================================
# CONFIG
# ============================================================================

TMDB_API_KEY = os.getenv("TMDB_API_KEY", "TON_CLE_TMDB_ICI")
MEDIATHEQUE_DIR = os.getenv("MEDIATHEQUE_DIR", "") or "/mnt/mediatheque"
OUTPUT_BASE = os.getenv("OUTPUT_DIR", "") or str(BASE_DIR / "data" / "posters")
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


def _bordure(border_style) -> int:
    """Épaisseur du cadre en px : « anime » 45, « standard » 22, « 0 » sans cadre, ou un nombre de 0 à 150 (curseur)."""
    if border_style == "anime":
        return BORDER_ANIME
    if border_style in (None, "", "standard"):
        return BORDER_STANDARD
    try:
        return min(max(int(float(border_style)), 0), 150)
    except (TypeError, ValueError):
        return BORDER_STANDARD

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
import redemarrer       # bouton « 🔄 Redémarrer l'appli » (fichier identique dans les quatre applis)
redemarrer.init_app(app)

import notif     # alertes Telegram (redémarrage inattendu, erreurs, mise à jour)
notif.init_app(app, BASE_DIR, APP_VERSION, Path(BASE_DIR) / "data" / "moufloster.log", emby_settings._write_secret)


def _apres_reprise(noms):
    """Réglages repris d'une appli voisine : la clé TheMovieDB sert tout de suite (Emby et Telegram sont relus à chaque appel)."""
    if "TMDB_API_KEY" in noms:
        _set_tmdb_key(os.environ["TMDB_API_KEY"])


import voisins   # 🔗 MouFlopening et les autres applis MouFl : appels directs protégés par une clé, reprise des réglages
voisins.init_app(app, BASE_DIR, emby_settings._write_secret, _apres_reprise)

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


def _uploads_gardes():
    """Images envoyées dont une mise en page mémorisée a besoin (pour pouvoir modifier ce poster plus tard)."""
    noms = set()
    try:
        for l in json.loads((BASE_DIR / "data" / "layouts.json").read_text(encoding="utf-8")).values():
            url = ((l or {}).get("poster") or {}).get("url") or ""
            if url.startswith(UPLOAD_URL_PREFIX):
                noms.add(os.path.basename(url))
    except (OSError, ValueError, AttributeError):
        pass
    return noms


def _clean_uploads():
    """Les images envoyées et les étapes de « Effacer un élément » ne servent que le temps de la retouche : nettoyées après 7 jours
    (sauf celles d'un poster qu'on peut reprendre : sa mise en page est mémorisée)."""
    import time as _t
    n = 0
    gardes = _uploads_gardes()
    try:
        for f in os.scandir(UPLOAD_DIR):
            if f.is_file() and _UPLOAD_RE.match(f.name) and f.name not in gardes and _t.time() - f.stat().st_mtime > UPLOAD_KEEP_DAYS * 86400:
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


@lru_cache(maxsize=64)
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
    border = _bordure(border_style)
    iw, ih = IMG_WIDTH - 2 * border, IMG_HEIGHT - 2 * border
    sw, sh = size
    scale = max(iw / sw, ih / sh) * _clamp_zoom(zoom)
    return [round(max(0.0, sw * scale - iw), 1), round(max(0.0, sh * scale - ih), 1)]


def _clamp01(value, default=0.5):
    try:
        return min(max(float(value), 0.0), 1.0)
    except (TypeError, ValueError):
        return default


@lru_cache(maxsize=8)
def _degrade(border_width: int) -> Image.Image:
    """Dégradé noir de l'intérieur du cadre (courbe du PSD), calculé une fois par épaisseur de cadre."""
    inner_w, inner_h = IMG_WIDTH - 2 * border_width, IMG_HEIGHT - 2 * border_width
    hauteurs = np.arange(inner_h) + border_width
    alpha = np.round(255 * np.interp(hauteurs, [p[0] for p in GRADIENT_CURVE], [p[1] for p in GRADIENT_CURVE])).astype(np.uint8)
    rgba = np.zeros((inner_h, inner_w, 4), dtype=np.uint8)
    rgba[:, :, 3] = alpha[:, None]
    return Image.fromarray(rgba, "RGBA")


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
    zoom: float = 1.0,         # 1 = l'image remplit le cadre; >1 = zoom avant; <1 = dézoom (bords flous)
    rapport: list = None       # si fourni : taille réelle de chaque ligne (alerte « titre trop long » de la page)
) -> Image.Image:
    """
    Compose l'image finale (1000x1500) :
    1. Poster agrandi SANS déformation (ratio conservé), puis recadré pour remplir l'intérieur du cadre
    2. Dégradé noir (transparent -> noir complet), limité au poster
    3. Cadre blanc (anime 45 px / standard 22 px)
    4. Grand chiffre gris (saison) centré derrière le titre
    5. Textes centrés à hauteur fixe
    """
    border_width = _bordure(border_style)
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
        poster = Image.alpha_composite(poster.convert("RGBA"), _degrade(border_width)).convert("RGB")

    final = Image.new("RGB", (IMG_WIDTH, IMG_HEIGHT), "white")
    final.paste(poster, (border_width, border_width))

    center_x = IMG_WIDTH // 2
    max_text_w = titre.largeur_utile(border_width)

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

    # Chaque ligne a sa position EXACTE du PSD (aucun décalage automatique) ; la taille est gardée :
    # petits mots en petit si besoin, rétrécissement seulement en dernier recours (signalé à la page)
    for num, (text, base_size, baseline) in enumerate((
        (t1, 49, BASELINE_L1),
        (t2, 89, BASELINE_L2),
        (t3, 89, BASELINE_L3),
        (t4, 49, BASELINE_L4),
        (t5, 49, BASELINE_L5),
    ), start=1):
        if text:
            plan = titre.ecrire_ligne(draw, text, base_size, center_x, baseline, max_text_w, load_font)
            if rapport is not None:
                rapport.append({"ligne": num, "base": base_size, "taille": plan["taille"], "mode": plan["mode"]})

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


def _telecharger_tmdb(url):
    """Octets d'une image TMDB (seulement image.tmdb.org), None si indisponible."""
    if not url.startswith("https://image.tmdb.org/t/p/"):
        return None
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        return r.content
    except Exception:
        return None


def _dossier_de_la_serie(media_type, tmdb_id, titles, year):
    """Dossier « Famille/Titre » de la médiathèque seulement s'il est sûr (mémorisé, connu d'Emby, ou nom identique) ; sinon None."""
    memo = library.remembered(media_type, tmdb_id)
    if memo:
        return memo
    if tmdb_id and emby.configured():
        try:
            item = emby.find_item(media_type, tmdb_id, "", titles)
            rel = library.rel_depuis_emby((item or {}).get("Path"), media_type) if item else None
            if rel and library.resolve_folder(rel) is not None:
                return rel
        except Exception as e:
            logger.info("Dossier d'après Emby impossible : %s", e)
    candidats = library.find_candidates(media_type, titles, year=year, tmdb_id=tmdb_id)
    if candidats:
        best = candidats[0]
        gap = best["score"] - (candidats[1]["score"] if len(candidats) > 1 else 0)
        if best["score"] >= 0.9 and (gap >= 0.05 or best["score"] >= 2):
            return best["rel"]
    return None


_CACHE_REFS = {}                     # (type, id, cible) -> (instant, refs, message) : la recherche des modèles ne se refait pas à chaque lot


def _parts_de_la_saga(media_type, tmdb_id):
    """Films de la saga d'un film (ou d'une saga TMDB) : [{id, title, original_title, year}] du plus ancien au plus récent."""
    cid = tmdb_id
    if media_type == "movie":
        r = tmdb_get(f"https://api.themoviedb.org/3/movie/{tmdb_id}")
        r.raise_for_status()
        cid = ((r.json() or {}).get("belongs_to_collection") or {}).get("id")
        if not cid:
            return []
    r = tmdb_get(f"https://api.themoviedb.org/3/collection/{cid}", {"language": "fr-FR"})
    r.raise_for_status()
    parts = sorted((r.json() or {}).get("parts") or [], key=lambda p: p.get("release_date") or "9999")
    return [{"id": p.get("id"), "title": p.get("title") or "", "original_title": p.get("original_title") or "",
             "year": _to_int((p.get("release_date") or "")[:4])} for p in parts if p.get("id")]


def _references_de(media_type, tmdb_id, titles, year, season_text):
    """Posters modèles à suivre pour ce titre : (refs, message, cible).
    Série : les posters du dossier de la série (sauf celui qu'on refait). Film d'une saga, ou saga : les posters des AUTRES films
    de la saga qui sont dans la médiathèque. Gardé 10 minutes."""
    cible = library.target_name("tv", season_text) if media_type == "tv" else "poster.jpg"
    cle = (media_type, tmdb_id, cible)
    memo = _CACHE_REFS.get(cle)
    if memo and time.monotonic() - memo[0] < 600:
        return memo[1], memo[2], cible
    refs, message = [], ""
    try:
        if media_type == "tv":
            rel = _dossier_de_la_serie("tv", tmdb_id, titles, year)
            if not rel:
                message = "Dossier de la série introuvable dans la médiathèque : pas de comparaison."
            else:
                refs = coherence.posters_existants(rel, cible)
                if not refs:
                    message = "Aucun poster déjà présent dans le dossier de cette série : rien à comparer."
        else:
            parts = _parts_de_la_saga(media_type, tmdb_id)
            if not parts:
                message = "Ce film n'appartient à aucune saga : rien à comparer."
            for part in parts[:14]:
                if media_type == "movie" and part["id"] == tmdb_id:
                    continue                                         # le film dont on refait le poster ne sert pas de modèle
                rel = _dossier_de_la_serie("movie", part["id"], [t for t in (part["title"], part["original_title"]) if t], part["year"])
                dossier = library.resolve_folder(rel) if rel else None
                if dossier is not None and (dossier / "poster.jpg").is_file():
                    refs.append({"rel": rel, "name": "poster.jpg", "label": part["title"] + (f" ({part['year']})" if part["year"] else "")})
            if parts and not refs:
                message = "Aucun autre film de la saga n'a de poster dans ta médiathèque : rien à comparer."
    except Exception as e:
        logger.info("Modèles de poster : %s", diag.redact(str(e)))
        refs, message = [], "Saga ou dossier illisible pour le moment (TMDB ou médiathèque) : pas de comparaison."
    if len(_CACHE_REFS) > 200:
        _CACHE_REFS.clear()
    _CACHE_REFS[cle] = (time.monotonic(), refs, message)
    return refs, message, cible


def _lire_titre_de(data):
    titles = [t for t in (data.get("title"), data.get("original_title")) if t]
    return data.get("media_type"), _to_int(data.get("tmdb_id")), titles, _to_int(data.get("year")), data.get("season_text")


@app.route("/api/coherence", methods=["POST"])
def api_coherence():
    """🎯 Posters proposés par TMDB classés selon leur ressemblance avec les posters déjà présents dans la médiathèque :
    posters du dossier de la série, ou des autres films de la saga. L'interface l'appelle par lots de 10 posters (barre de progression).
    POST {media_type, tmdb_id, title, original_title, year, season_text, posters: [{path, lang}]}.
    Réponse : {refs: [{rel, name, label}], scores: {chemin TMDB: {score, raison, texte, rang}}, message} ; rien n'est modifié."""
    data = request.get_json(silent=True) or {}
    media_type, tmdb_id, titles, year, saison = _lire_titre_de(data)
    if media_type not in ("tv", "movie", "collection") or not tmdb_id:
        return jsonify({"refs": [], "scores": {}, "message": ""})
    if not library.LIBRARY_ROOT.is_dir():
        return jsonify({"refs": [], "scores": {}, "message": "Médiathèque introuvable."})
    refs, message, cible = _references_de(media_type, tmdb_id, titles, year, saison)
    posters = [p for p in (data.get("posters") or []) if isinstance(p, dict)]
    resultat = coherence.classer(refs, posters, _telecharger_tmdb)
    return jsonify({**resultat, "message": message if not resultat["refs"] else "", "cible": cible})


@app.route("/api/coherence/aligner", methods=["POST"])
def api_coherence_aligner():
    """🎯 Cadrages proposés (zoom + position) pour que le personnage ou le groupe du poster choisi soit au même endroit et à la même
    taille que sur les posters modèles. POST {…comme /api/coherence…, poster_url, border_style}.
    Réponse : {propositions: [{mode, zoom, crop_x, crop_y, ecart_haut, ecart_centre, bords_flous, sur, conseille}], modeles, message}.
    Rien n'est appliqué : l'interface montre l'aperçu."""
    data = request.get_json(silent=True) or {}
    media_type, tmdb_id, titles, year, saison = _lire_titre_de(data)
    if media_type not in ("tv", "movie", "collection") or not tmdb_id or not data.get("poster_url"):
        return jsonify({"propositions": [], "modeles": 0, "message": ""})
    refs, message, _ = _references_de(media_type, tmdb_id, titles, year, saison)
    mesures = coherence.modeles_mesures(refs) if refs else []
    cible = coherence.resume_modeles(mesures)
    if cible is None:
        return jsonify({"propositions": [], "modeles": 0, "message": message or "Je n'ai pas réussi à repérer le personnage sur tes posters modèles."})
    img = load_source_image(str(data.get("poster_url")), "w500")
    if not img:
        return jsonify({"propositions": [], "modeles": cible["n"], "message": "Impossible de charger ce poster."})
    propositions = coherence.proposer(img, cible, _bordure(data.get("border_style")), conseille=coherence.conseiller(cible))
    if not propositions:
        return jsonify({"propositions": [], "modeles": cible["n"], "message": "Je n'ai pas réussi à repérer le personnage sur ce poster : cadre-le à la main."})
    return jsonify({"propositions": propositions, "modeles": cible["n"], "message": "",
                    "regulier": bool(cible["ecarts"]["haut"] <= 0.12)})


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


def _masque_du_trace(mask_img: Image.Image, size, grossir=True):
    """Tracé de l'utilisatrice (blanc = à effacer) → masque 0/255 à la taille de l'image, un peu élargi (halo, anti-crénelage).
    Lève ValueError si rien n'a été peint."""
    import numpy as np
    import cv2
    w, h = size
    mask = np.array(mask_img.convert("L").resize((w, h), Image.Resampling.BILINEAR))
    mask = (mask > 40).astype("uint8") * 255
    if not mask.any():
        raise ValueError("Rien n'a été peint : colorie d'abord la zone à effacer")
    if grossir:
        grow = max(3, int(min(w, h) * 0.005))
        mask = cv2.dilate(mask, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * grow + 1, 2 * grow + 1)))
    return mask


def inpaint_region(img: Image.Image, mask_img: Image.Image, grossir=True):
    """
    Efface la zone peinte et la remplit avec son environnement (comme un remplissage d'après le contenu).
    Retourne (image, moteur, note): LaMa si installé, sinon OpenCV (et la note dit pourquoi).
    grossir=False : le masque est déjà définitif (zone restante après un comblage avec une 2e image).
    """
    import numpy as np
    import cv2

    img = img.convert("RGB")
    if max(img.size) > 4500:
        img.thumbnail((4500, 4500))
    w, h = img.size

    # On élargit un peu la zone: les contours (halo, anti-crénelage) d'un logo dépassent toujours du tracé
    mask = _masque_du_trace(mask_img, (w, h), grossir)

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


def _agrandir_avec_decor(img: Image.Image, pads):
    """Ajoute à l'image des bandes (gauche, haut, droite, bas, en pixels) et les fait remplir par LaMa ; l'image d'origine reste intacte.
    Renvoie l'image agrandie. Lève RuntimeError (message en français) si LaMa n'est pas disponible."""
    import numpy as np
    import lama
    ok, raison = lama.check()
    if not ok:
        raise RuntimeError("L'outil qui invente le décor (LaMa) n'est pas disponible : " + str(raison))
    g, h, d, b = pads
    arr = np.asarray(img.convert("RGB"))
    sh, sw = arr.shape[:2]
    large = np.pad(arr, ((h, b), (g, d), (0, 0)), mode="edge")       # le bord répété sert de point de départ neutre
    masque = np.full(large.shape[:2], 255, np.uint8)
    masque[h:h + sh, g:g + sw] = 0
    t0 = time.time()
    rempli = lama.inpaint(Image.fromarray(large), masque)
    logger.info(f"✨ Décor inventé par LaMa en {time.time() - t0:.1f} s ({sw}x{sh} → {large.shape[1]}x{large.shape[0]})")
    sortie = np.asarray(rempli.convert("RGB")).copy()
    sortie[h:h + sh, g:g + sw] = arr
    return Image.fromarray(sortie)


@app.route("/api/coherence/aligner_etendre", methods=["POST"])
def api_coherence_aligner_etendre():
    """🧑 Aligne le VISAGE du poster choisi sur celui de tes posters (même taille, même place) en inventant, si besoin, le décor qui manque
    d'un côté précis (par exemple au-dessus de la tête). POST {…comme /api/coherence/aligner…}.
    Réponse : {url (nouvelle image, absente si rien ne manque), zoom, crop_x, crop_y, part, message}."""
    data = request.get_json(silent=True) or {}
    media_type, tmdb_id, titles, year, saison = _lire_titre_de(data)
    if media_type not in ("tv", "movie", "collection") or not tmdb_id or not data.get("poster_url"):
        return jsonify({"error": "Titre ou poster manquant."}), 400
    refs, message, _ = _references_de(media_type, tmdb_id, titles, year, saison)
    cible = coherence.resume_modeles(coherence.modeles_mesures(refs) if refs else [])
    if cible is None or not cible.get("visage"):
        return jsonify({"error": "Aucun visage repéré sur tes posters de cette série."}), 422
    img = load_source_image(str(data.get("poster_url")), "original")
    if not img:
        return jsonify({"error": "Impossible de charger ce poster."}), 400
    img = img.convert("RGB")
    if max(img.size) > 2400:
        img.thumbnail((2400, 2400), Image.Resampling.LANCZOS)
    vis = coherence.visage(img)
    if not vis:
        return jsonify({"error": "Je ne vois pas de visage sur ce poster."}), 422
    pl = coherence.plan_visage(img.width, img.height, vis, cible["visage"], _bordure(data.get("border_style")))
    if pl["part"] > 0.5 or not 0.5 <= pl["zoom"] <= 3.0:
        return jsonify({"error": "Il manquerait trop de décor (%d %%) pour mettre ce visage à sa place : choisis un autre poster." % round(100 * pl["part"])}), 422
    sortie = {"zoom": round(pl["zoom"], 4), "crop_x": round(pl["crop_x"], 4), "crop_y": round(pl["crop_y"], 4), "part": round(100 * pl["part"])}
    if not any(pl["pads"]):
        return jsonify(dict(sortie, rien=True, message="Aucun décor à ajouter : le visage rejoint sa place."))
    try:
        agrandie = _agrandir_avec_decor(img, pl["pads"])
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503
    except Exception as e:
        logger.exception("Erreur pendant l'ajout de décor")
        return jsonify({"error": f"Impossible d'inventer le décor : {e}"}), 500
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    name = uuid.uuid4().hex + ".jpg"
    agrandie.save(os.path.join(UPLOAD_DIR, name), quality=95)
    return jsonify(dict(sortie, url=UPLOAD_URL_PREFIX + name, w=agrandie.width, h=agrandie.height,
                        message="Visage aligné ; décor ajouté : " + ", ".join(
                            f"{n} {v} px" for n, v in zip(("à gauche", "au-dessus", "à droite", "en dessous"), pl["pads"]) if v) + "."))


@app.route("/api/etendre", methods=["POST"])
def api_etendre():
    """✨ Invente le décor qui manque quand l'image est plus petite que le cadre au zoom choisi (bandes floues du dézoom).
    POST {poster_url, crop_x, crop_y, zoom, border_style}. Les bandes manquantes sont ajoutées à l'image puis remplies par LaMa
    (l'image d'origine n'est jamais modifiée) ; la réponse donne la nouvelle image et le zoom / recadrage qui gardent le même cadrage."""
    import numpy as np
    data = request.get_json(silent=True) or {}
    img = load_source_image(str(data.get("poster_url") or ""), "original")
    if not img:
        return jsonify({"error": "Impossible de charger l'image"}), 400
    img = img.convert("RGB")
    cx, cy, zoom = _clamp01(data.get("crop_x", 0.5)), _clamp01(data.get("crop_y", 0.5)), _clamp_zoom(data.get("zoom", 1.0))
    iw = IMG_WIDTH - 2 * _bordure(data.get("border_style"))
    ih = IMG_HEIGHT - 2 * _bordure(data.get("border_style"))
    if max(img.size) > 2400:                      # inutile d'inventer en très haute définition : le poster fini fait 1000 px
        img.thumbnail((2400, 2400), Image.Resampling.LANCZOS)
    sw, sh = img.size
    echelle = max(iw / sw, ih / sh) * zoom
    manque_x = max(0.0, iw / echelle - sw)        # largeur / hauteur manquantes, en pixels de l'image
    manque_y = max(0.0, ih / echelle - sh)
    if manque_x < 2 and manque_y < 2:
        return jsonify({"rien": True, "message": "Rien ne manque : l'image remplit déjà le cadre à ce zoom."})
    a, b = int(round(manque_x / 2)), int(round(manque_y / 2))        # moitié de chaque côté, comme l'aperçu centre l'image
    nw, nh = sw + 2 * a, sh + 2 * b
    part = max(manque_x / max(sw, 1), manque_y / max(sh, 1))
    if part > 0.8:
        return jsonify({"error": "Il manquerait plus de 80 % de l'image : choisis une image plus grande ou un zoom plus grand."}), 422
    try:
        resultat = _agrandir_avec_decor(img, (a, b, a, b))
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503
    except Exception as e:
        logger.exception("Erreur pendant l'ajout de décor")
        return jsonify({"error": f"Impossible d'inventer le décor : {e}"}), 500
    # même cadrage qu'avant : l'échelle d'affichage reste « echelle » ; sur un axe qui était trop petit, l'image remplit maintenant juste le cadre
    base = max(iw / nw, ih / nh)
    nouveau_zoom = _clamp_zoom(echelle / base)
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    name = uuid.uuid4().hex + ".jpg"
    resultat.save(os.path.join(UPLOAD_DIR, name), quality=95)
    return jsonify({"url": UPLOAD_URL_PREFIX + name, "w": nw, "h": nh, "zoom": round(nouveau_zoom, 4),
                    "crop_x": 0.5 if manque_x >= 2 else cx, "crop_y": 0.5 if manque_y >= 2 else cy,
                    "part": round(100 * part), "message": f"Décor ajouté ({round(100 * part)} % de l'image)."})


@app.route("/api/erase/combler", methods=["POST"])
def api_erase_combler():
    """🩹 Comble la zone peinte (texte, logo) avec les morceaux d'une 2e image des mêmes personnages, replacés un par un ;
    ce que la 2e image ne couvre pas est comblé par l'effaceur habituel.
    POST {poster_url, mask (PNG en data URL), source_url}. Réponse comme /api/erase + rapport {couvert, reste, morceaux}."""
    import numpy as np
    data = request.get_json(silent=True) or {}
    mask_data = data.get("mask") or ""
    if not mask_data.startswith("data:image/png;base64,") or len(mask_data) > 20_000_000:
        return jsonify({"error": "Tracé invalide"}), 400
    poster = load_source_image(data.get("poster_url") or "", "original")
    source = load_source_image(data.get("source_url") or "", "original")
    if not poster or not source:
        return jsonify({"error": "Impossible de charger l'une des deux images"}), 400
    try:
        poster = poster.convert("RGB")
        if max(poster.size) > 4500:
            poster.thumbnail((4500, 4500))
        source = source.convert("RGB")
        source.thumbnail((3200, 3200))
        mask_img = Image.open(BytesIO(base64.b64decode(mask_data.split(",", 1)[1])))
        masque = _masque_du_trace(mask_img, poster.size)
        res = comblage.combler(np.array(poster), masque, np.array(source))
        rapport = res["rapport"]
        if rapport["couvert"] < 5.0:
            return jsonify({"error": "Cette image ne montre pas, à cet endroit, quelque chose qui correspond à ton poster : je ne peux pas la superposer. "
                                     "Essaie une autre image, ou efface la zone avec « Effacer la zone coloriée ».", "rapport": rapport}), 422
        result, engine, note = Image.fromarray(res["image"]), "2ᵉ image", ""
        if (res["reste"] > 0).sum() > 40:                                  # le reste : l'effaceur, avec les morceaux collés comme contexte
            result, engine2, note = inpaint_region(result, Image.fromarray(res["reste"]), grossir=False)
            engine = "2ᵉ image + " + engine2
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.exception("Erreur comblage avec une 2e image")
        return jsonify({"error": f"Comblage impossible: {e}"}), 500
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    name = uuid.uuid4().hex + ".jpg"
    result.save(os.path.join(UPLOAD_DIR, name), quality=95)
    logger.info(f"🩹 Comblage ({engine}): {rapport['couvert']} % par la 2e image, {rapport['reste']} % par l'effaceur → {name}")
    return jsonify({"url": UPLOAD_URL_PREFIX + name, "w": result.width, "h": result.height, "engine": engine, "note": note, "rapport": rapport})


@app.route("/api/backdrops", methods=["POST"])
def api_backdrops():
    """Fonds d'écran TheMovieDB d'un titre (images 16:9 des personnages, souvent sans texte) : sources possibles pour combler un poster.
    POST {id, media_type}. Réponse : {backdrops: [{url, vignette, w, h, lang}]}, ceux sans texte d'abord."""
    data = request.get_json(silent=True) or {}
    tmdb_id, media_type = str(data.get("id") or ""), data.get("media_type")
    if media_type not in ("movie", "tv", "collection") or not tmdb_id.isdigit():
        return jsonify({"backdrops": []})
    try:
        r = tmdb_get(f"https://api.themoviedb.org/3/{media_type}/{tmdb_id}/images")
        r.raise_for_status()
        liste = r.json().get("backdrops") or []
    except Exception as e:
        logger.info("Fonds d'écran TMDB : %s", diag.redact(str(e)))
        return jsonify({"backdrops": [], "error": "Fonds d'écran indisponibles sur TMDB"})
    liste = [b for b in liste if b.get("file_path")]
    liste.sort(key=lambda b: (bool(b.get("iso_639_1")), -(b.get("vote_average") or 0)))
    return jsonify({"backdrops": [{"url": "https://image.tmdb.org/t/p/w1280" + b["file_path"], "vignette": "https://image.tmdb.org/t/p/w300" + b["file_path"],
                                   "w": b.get("width"), "h": b.get("height"), "lang": b.get("iso_639_1") or ""} for b in liste[:30]]})


@app.route("/api/decoupe", methods=["POST"])
def api_decoupe():
    """Lignes proposées pour le titre choisi (titre, sous-titre, petits mots, grand chiffre gris de la suite)."""
    data = request.get_json(silent=True) or {}
    nom = str(data.get("title") or "")[:300]
    largeur = titre.largeur_utile(_bordure(data.get("border_style", "standard")))
    return jsonify(titre.decouper(nom, largeur, load_font, film=data.get("media_type") == "movie"))


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
    lignes = []
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
            zoom=data.get("zoom", 1.0),
            rapport=lignes
        )

        # Envoyé tel quel en JPEG (qualité 85, sans différence visible à l'écran) : environ deux fois moins de données
        # qu'avant (JPEG 95 en base64 dans du JSON) ; le débord et la taille des lignes passent dans les en-têtes
        buffer = BytesIO()
        composed.save(buffer, format="JPEG", quality=85)
        resp = app.response_class(buffer.getvalue(), mimetype="image/jpeg")
        resp.headers["X-Apercu-Debord"] = json.dumps(cover_overflow(img.size, border_style, data.get("zoom", 1.0)))
        resp.headers["X-Apercu-Lignes"] = json.dumps(lignes)          # ASCII seulement (ensure_ascii)
        resp.headers["Cache-Control"] = "no-store"
        return resp
    except Exception as e:
        logger.error(f"Erreur composition: {e}")
        return jsonify({"error": str(e)}), 500


def _sortie():
    """Dossier des posters : celui de l'admin, ou « _copains/<id> » pour un copain (vidé au bout de 30 jours)."""
    if auth.role() == "copain":
        d = os.path.join(OUTPUT_BASE, "_copains", re.sub(r"[^A-Za-z0-9._@+-]", "_", auth.utilisateur() or "x"))
        os.makedirs(d, exist_ok=True)
        return d
    return OUTPUT_BASE


def _menage_copains():
    """Chaque jour : les créations des copains de plus de 30 jours sont effacées."""
    while True:
        racine = os.path.join(OUTPUT_BASE, "_copains")
        limite = time.time() - 30 * 86400
        for dp, _, fs in os.walk(racine):
            for f in fs:
                try:
                    if os.path.getmtime(os.path.join(dp, f)) < limite:
                        os.remove(os.path.join(dp, f))
                except OSError:
                    pass
        time.sleep(86400)


@app.route("/api/copain/envoyer", methods=["POST"])
def api_copain_envoyer():
    """Poster du copain (ou poster de l'admin, « existant ») → l'élément correspondant de SON Emby (par identifiant TMDB)."""
    p = comptes.profil(auth.utilisateur())
    if not (p.get("emby_url") and p.get("emby_cle")):
        return jsonify({"ok": False, "error": "Relie d'abord ton Emby (bouton « 🔌 Mon Emby » en haut)."}), 400
    ok, msg = comptes.adresse_permise(p["emby_url"])
    if not ok:
        return jsonify({"ok": False, "error": msg}), 400
    data = request.get_json(silent=True) or {}
    media_type = "tv" if data.get("media_type") == "tv" else "movie"
    tmdb_id = _to_int(data.get("tmdb_id"))
    if not tmdb_id:
        return jsonify({"ok": False, "error": "Choisis d'abord un film ou une série."}), 400
    if data.get("existant"):
        image = _poster_admin(media_type, tmdb_id, data.get("title") or "")
    else:
        f = os.path.join(_sortie(), os.path.basename(str(data.get("filename", ""))))
        image = open(f, "rb").read() if os.path.isfile(f) else None
    if not image:
        return jsonify({"ok": False, "error": "Poster introuvable."}), 404
    try:
        with emby.serveur(p["emby_url"], p["emby_cle"]):
            item = emby.find_item(media_type, tmdb_id, "", [data.get("title") or ""])
            if not item:
                return jsonify({"ok": False, "error": "Ce titre n'est pas trouvé dans ton Emby (identifiant TMDB)."}), 404
            emby.set_primary_image(item["Id"], image)
    except emby.EmbyError as e:
        return jsonify({"ok": False, "error": str(e).replace("EMBY_API_KEY", "ta clé")}), 502
    logger.info("Copain %s : poster envoyé dans son Emby (%s %s)", auth.utilisateur(), media_type, tmdb_id)
    return jsonify({"ok": True, "message": f"✅ Poster envoyé dans ton Emby : {item.get('Name', '')}."})


def _poster_admin(media_type, tmdb_id, titre):
    """Poster actuel de ce titre dans l'Emby de l'admin (lecture seule), ou None."""
    if not emby.configured():
        return None
    try:
        item = emby.find_item(media_type, tmdb_id, "", [titre])
        return emby.get_primary_image(item["Id"]) if item else None
    except emby.EmbyError:
        return None


@app.route("/api/copain/existant", methods=["POST"])
def api_copain_existant():
    data = request.get_json(silent=True) or {}
    media_type = "tv" if data.get("media_type") == "tv" else "movie"
    tmdb_id = _to_int(data.get("tmdb_id"))
    return jsonify({"existe": bool(tmdb_id and _poster_admin(media_type, tmdb_id, data.get("title") or ""))})


@app.route("/api/copain/existant/<media_type>/<int:tmdb_id>")
def api_copain_existant_image(media_type, tmdb_id):
    img = _poster_admin("tv" if media_type == "tv" else "movie", tmdb_id, "")
    if not img:
        return jsonify({"error": "Pas de poster"}), 404
    resp = send_file(BytesIO(img), mimetype="image/jpeg", as_attachment=request.args.get("dl") == "1", download_name=f"poster-{tmdb_id}.jpg")
    resp.headers["Cache-Control"] = "no-store"
    return resp


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
        while os.path.exists(os.path.join(_sortie(), filename)):      # jamais d'écrasement (deux enregistrements dans la même seconde)
            filename = f"{safe_title}_{timestamp}_{n}.jpg"
            n += 1
        filepath = os.path.join(_sortie(), filename)

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
    filepath = os.path.join(_sortie(), safe_name)
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
        return jsonify({"error": f"Médiathèque introuvable : {library.LIBRARY_ROOT}. Choisis son dossier dans ⚙️ Réglages → Dossiers."}), 500

    titles = [t for t in (data.get("title"), data.get("original_title")) if t]
    year = _to_int(data.get("year"))
    tmdb_id = _to_int(data.get("tmdb_id"))
    library.clear_cache()

    candidates = library.find_candidates(media_type, titles, year=year, tmdb_id=tmdb_id)
    if media_type == "movie" and not any(c["score"] >= 0.9 for c in candidates):
        # film absent des dossiers de films : souvent un spécial d'une série (Blue Exorcist, le film) ; jamais « sûr »
        for c in library.find_candidates("tv", titles, min_score=0.85, limit=3):
            if not any(x["rel"] == c["rel"] for x in candidates):
                candidates.append({**c, "score": min(c["score"], 0.89)})
        candidates.sort(key=lambda c: -c["score"])
    memo = library.remembered(media_type, tmdb_id)
    if not memo and tmdb_id and emby.configured():
        # Emby connaît le dossier exact par l'identifiant TMDB (dossiers nommés par Sonarr avec des points, etc.)
        try:
            item = emby.find_item(media_type, tmdb_id, "", titles)
            rel = library.rel_depuis_emby((item or {}).get("Path"), media_type) if item else None
        except Exception as e:
            logger.info("Dossier d'après Emby impossible : %s", e)
            rel = None
        if rel:
            candidates = [c for c in candidates if c["rel"] != rel]
            family, _, name = rel.partition("/")
            candidates.insert(0, {"rel": rel, "family": family, "name": name, "score": 2.5,
                                  "media_type": "tv" if family in library.FAMILIES["tv"] else "movie"})

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
    if not tgt["episode"]:                                        # 🆕 ce titre (ou cette saison) n'est plus à faire
        nouveautes.marquer(rel, library.parse_season(data.get("season_text")) if target_type == "tv" else None)
    logger.info(f"📁 Médiathèque: {result['written']} (action={action}, sauvegarde={result['backup']})")
    dossier_titre = library.resolve_dir(rel, "")                 # dossier du film / de la série (pas la saison)
    result["generique"] = _a_un_generique(dossier_titre) if dossier_titre else None
    result["dossier"] = str(dossier_titre) if dossier_titre else ""

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


@app.route("/api/library/episodes", methods=["POST"])
def api_library_episodes():
    """Épisodes d'une saison du dossier (spéciaux surtout : films rangés dans la saison 0), pour les choisir dans une liste"""
    data = request.get_json(silent=True) or {}
    season = library.parse_season(data.get("season_text"))
    if season is None or library.resolve_folder(data.get("rel")) is None:
        return jsonify({"episodes": []})
    return jsonify({"episodes": library.list_episodes(data.get("rel"), season)})


# ---------- 🆕 Nouveautés : ajoutés dans Emby, pas encore de poster fait ----------

@app.route("/api/nouveautes")
def api_nouveautes():
    if not emby.configured():
        return jsonify({"error": "Emby n'est pas réglé (⚙️ Réglages → Connexions)", "elements": []})
    try:
        r = nouveautes.lister(request.args.get("choix"))
        for el in r["elements"]:                     # 🎵 a-t-il déjà son générique ? (bouton pour l'ouvrir dans MouFlopening)
            d = library.resolve_folder(el["rel"])
            el["dossier"] = str(d) if d else ""
            el["generique"] = _a_un_generique(d) if d else None
        return jsonify(r)
    except emby.EmbyError as e:
        return jsonify({"error": str(e), "elements": []})
    except Exception as e:
        logger.error(f"Nouveautés : {e}")
        return jsonify({"error": "Lecture d'Emby impossible", "elements": []})


@app.route("/api/nouveautes/deja-bon", methods=["POST"])
def api_nouveautes_deja_bon():
    data = request.get_json(silent=True) or {}
    rel = data.get("rel")
    if library.resolve_folder(rel) is None:
        return jsonify({"error": "Dossier introuvable"}), 400
    saisons = data.get("saisons")
    if isinstance(saisons, list):
        for n in saisons:
            if _to_int(n) is not None:
                nouveautes.marquer(rel, _to_int(n))
    if data.get("poster"):
        nouveautes.marquer(rel)
    return jsonify({"ok": True})


@app.route("/api/settings/nouveautes", methods=["GET", "POST"])
def api_settings_nouveautes():
    if request.method == "POST":
        try:
            nouveautes.regler_defaut((request.get_json(silent=True) or {}).get("defaut"))
        except (ValueError, OSError) as e:
            return jsonify({"ok": False, "error": str(e)}), 400
        return jsonify({"ok": True, "message": "Choix enregistré."})
    return jsonify(nouveautes.reglages())


# ---------- MouFlopening : le titre a-t-il un générique ? (même règle que MouFlopening et Emby) ----------
_GENERIQUE_EXTS = (".mp3", ".flac", ".ogg", ".m4a", ".aac", ".wav", ".opus", ".wma")


def _a_un_generique(dossier):
    """theme.<audio> d'au moins 8 Ko dans le dossier, ou un fichier audio dans « theme-music »."""
    try:
        d = Path(dossier)
        if any((d / f"theme{e}").is_file() and (d / f"theme{e}").stat().st_size >= 8192 for e in _GENERIQUE_EXTS):
            return True
        tm = d / "theme-music"
        return tm.is_dir() and any(f.suffix.lower() in _GENERIQUE_EXTS and f.stat().st_size >= 8192 for f in tm.iterdir())
    except OSError:
        return None


# ---------- 🔗 MouFlopening, de serveur à serveur (voisins.py) ----------

@app.route("/api/generique/auto", methods=["POST"])
def api_generique_auto():
    """Après l'envoi d'un poster : MouFlopening cherche lui-même le générique validé (ThemerrDB, AnimeThemes) du titre,
    sans quitter MouFloster. -> {ok, job} à suivre avec /api/generique/job/<job>, ou {ok: False, error} (on propose alors
    d'ouvrir MouFlopening)."""
    dossier = str((request.get_json(silent=True) or {}).get("dossier") or "")
    if not dossier:
        return jsonify({"ok": False, "error": "Dossier inconnu"}), 400
    try:
        r = voisins.appeler("mouflopening", "POST", "/api/voisin/generique/auto", json={"dossier": dossier})
    except voisins.ErreurVoisin as e:
        return jsonify({"ok": False, "error": str(e)})
    logger.info("🎵 Générique demandé à MouFlopening : %s", dossier)
    return jsonify(r)


@app.route("/api/generique/job/<jid>")
def api_generique_job(jid):
    if not re.fullmatch(r"[0-9a-f]{6,32}", jid):
        return jsonify({"error": "Opération inconnue"}), 404
    try:
        return jsonify(voisins.appeler("mouflopening", "GET", f"/api/voisin/job/{jid}"))
    except voisins.ErreurVoisin as e:
        return jsonify({"error": str(e)}), 502


@voisins.route(app, "/api/voisin/nouveautes")
def voisin_nouveautes():
    """Pour le compte rendu de la nuit de MouFlopening : tout ce qui est arrivé dans Emby depuis « depuis »,
    avec ce qui reste à faire en posters."""
    depuis = str(request.args.get("depuis") or "")
    if not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", depuis):
        return jsonify({"ok": False, "error": "Date « depuis » invalide"}), 400
    if not emby.configured():
        return jsonify({"ok": False, "error": "Emby n'est pas réglé dans MouFloster"}), 503
    try:
        return jsonify({"ok": True, "elements": nouveautes.arrivees(depuis)})
    except emby.EmbyError as e:
        return jsonify({"ok": False, "error": str(e)}), 502


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


@app.route("/api/mouflanga/recherche")
def api_mouflanga_recherche():
    """Séries de MouFlanga (avec poster choisi) dont le nom contient la recherche : proposées avec les résultats de TheMovieDB."""
    if not mouflanga_link.mode():
        return jsonify({"series": []})
    try:
        liste = mouflanga_link.series()
    except mouflanga_link.ErreurMouflanga:
        return jsonify({"series": []})
    return jsonify({"series": mouflanga_link.chercher(request.args.get("q", "").strip(), liste)})


@app.route("/api/mouflanga/reprendre", methods=["POST"])
def api_mouflanga_reprendre():
    """Le poster actuel d'une série de MouFlanga devient une image de travail (comme une image envoyée), pour le refaire ou le garder."""
    nom = str((request.get_json(silent=True) or {}).get("name", ""))
    try:
        octets = mouflanga_link.couverture(nom)
    except mouflanga_link.ErreurMouflanga as e:
        return jsonify({"error": str(e)}), 502
    if not octets:
        return jsonify({"error": "Cette série n'a pas de poster choisi dans MouFlanga"}), 404
    try:
        img = Image.open(BytesIO(octets))
        img.load()
        img = ImageOps.exif_transpose(img).convert("RGB")
    except Exception:
        return jsonify({"error": "Le poster de MouFlanga n'est pas une image lisible"}), 502
    if max(img.size) > UPLOAD_MAX_SIDE:
        img.thumbnail((UPLOAD_MAX_SIDE, UPLOAD_MAX_SIDE), Image.LANCZOS)
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    fichier = uuid.uuid4().hex + ".jpg"
    img.save(os.path.join(UPLOAD_DIR, fichier), quality=95)
    logger.info(f"📚 Poster repris de MouFlanga : {nom} ({img.width}x{img.height})")
    return jsonify({"url": UPLOAD_URL_PREFIX + fichier, "w": img.width, "h": img.height})


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


def _cle_layout(media_type, tmdb_id):
    """Clé d'une mise en page : « tv:1234 », ou « libre:<nom> » pour un titre sans fiche TheMovieDB (image perso)."""
    mt, tid = str(media_type or ""), str(tmdb_id or "")
    if mt in ("movie", "tv", "collection") and tid.isdigit():
        return f"{mt}:{tid}"
    if mt == "libre":
        nom = " ".join(tid.lower().split())[:200]
        return f"libre:{nom}" if nom else None
    return None


@app.route("/api/layout")
def api_layout_get():
    key = _cle_layout(request.args.get("media_type"), request.args.get("tmdb_id"))
    return jsonify({"layout": _read_layouts().get(key) if key else None})


@app.route("/api/layout", methods=["POST"])
def api_layout_save():
    data = request.get_json(silent=True) or {}
    key, layout = _cle_layout(data.get("media_type"), data.get("tmdb_id")), data.get("layout")
    if not key or not isinstance(layout, dict):
        return jsonify({"error": "Mise en page invalide"}), 400
    if len(json.dumps(layout)) > 20000:
        return jsonify({"error": "Mise en page trop grande"}), 400
    with _layouts_lock:
        layouts = _read_layouts()
        layouts.pop(key, None)
        layouts[key] = {**layout, "saved": datetime.now().strftime("%d/%m/%Y %H:%M")}
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
<link rel="stylesheet" href="/ui/apparence.css?v={{ version|urlencode }}">
<script src="/ui/apparence.js?v={{ version|urlencode }}" defer></script>
<script src="/ui/gestes.js?v={{ version|urlencode }}" defer></script>
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
        .preview-image.draggable { cursor: grab; }
        .preview-image.recadrage { touch-action: none; outline: 2px dashed var(--accent, #52b54b); outline-offset: -2px; }
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
            flex: 0 1 auto;          /* seul le texte est sensible au doigt, pas toute la ligne */
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
        .cadre-reglage { margin: 6px 0 10px; }
        .cadre-reglage input[type=range] { width: 100%; }
        .cadre-presets { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 4px; }
        .cadre-presets button { width: auto; padding: 6px 10px; font-size: 13px; }
        #depotVoile { position: fixed; inset: 0; z-index: 9999; background: color-mix(in srgb, var(--accent) 25%, transparent); border: 4px dashed var(--accent); display: flex; align-items: center; justify-content: center; font-size: 1.6em; font-weight: 700; color: #fff; text-shadow: 0 2px 6px #000; pointer-events: none; }
        #depotVoile[hidden] { display: none; }
        .mfg-retour { display: inline-block; margin-top: 8px; padding: 6px 12px; border-radius: 6px; background: var(--accent); color: #fff; text-decoration: none; font-weight: 600; }
        .mfg-retour[hidden], #mfgBandeau[hidden] { display: none; }
        .lib-compare figcaption { font-size: 0.8em; color: #555; margin-top: 6px; line-height: 1.35; }
        .lib-compare figcaption b { color: #222; }
        .lib-actions { display: flex; flex-direction: column; gap: 8px; margin-top: 12px; }
        .layout-btn { width: 100%; margin: 6px 0 10px; }
        /* 🆕 Nouveautés sans poster */
        .nouv-barre { display: flex; align-items: center; gap: 8px; white-space: nowrap; margin-bottom: 8px; font-size: 0.85em; color: var(--muted); }
        .nouv-barre select { flex: 1; width: auto; min-width: 0; }
        .nouv-item { padding: 8px 0; border-top: 1px solid var(--line); }
        .nouv-nom { font-weight: 600; font-size: 0.9em; margin-bottom: 6px; }
        .nouv-nom small { font-weight: 400; color: var(--muted); }
        .nouv-act { display: flex; flex-wrap: wrap; gap: 6px; }
        .nouv-act button { width: auto !important; padding: 6px 10px !important; font-size: 0.82em !important; margin: 0 !important; }
        .nouv-vide { font-size: 0.85em; color: var(--muted); padding: 6px 0; }
        .nouv-gen { align-self: center; font-size: 0.8em; color: var(--muted); }
        .nouv-gen-lien { display: inline-block; padding: 6px 10px; border-radius: 6px; border: 1px solid var(--line); color: var(--text); text-decoration: none; }
        body.copain #nouvPanel { display: none !important; }
        .taille-info { font-size: 0.82em; margin-top: 4px; line-height: 1.35; }
        .taille-info:empty { display: none; }
        .taille-info.rouge { color: #ff6b6b; font-weight: 600; }
        .taille-info.doux { color: var(--muted); }
        .text-layer input.trop-long { border-color: #ff6b6b !important; box-shadow: 0 0 0 1px #ff6b6b; }
        .result-item { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
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
        /* 🎯 cohérence : posters déjà dans la médiathèque + ressemblance des posters proposés */
        .coh-bande { padding: 4px 0 8px; }
        .coh-refs { display: flex; gap: 8px; overflow-x: auto; padding: 4px 0; -webkit-overflow-scrolling: touch; }
        .coh-ref { flex: 0 0 auto; width: 64px; text-align: center; font-size: 11px; color: var(--text2); }
        .coh-ref img { width: 64px; aspect-ratio: 2/3; object-fit: cover; border-radius: 5px; border: 1px solid var(--line); display: block; margin-bottom: 2px; }
        .coh-titre { font-size: 12px; color: var(--text2); }
        .coh-info { font-size: 13px; color: var(--text2); margin: 2px 0 8px; line-height: 1.35; }
        .coh-progres { margin: 6px 0 8px; font-size: 13px; color: var(--text2); }
        .coh-progres .barre { height: 8px; border-radius: 4px; background: var(--field); overflow: hidden; margin-top: 4px; }
        .coh-progres .barre i { display: block; height: 100%; width: 3%; background: var(--accent); transition: width 0.4s; }
        .coh-progres.indet .barre i { width: 35%; animation: coh-va-et-vient 1.1s ease-in-out infinite alternate; }
        @keyframes coh-va-et-vient { from { margin-left: 0; } to { margin-left: 65%; } }
        .coh-align { margin: 4px 0 8px; }
        .coh-propo { display: flex; flex-wrap: wrap; gap: 6px; margin: 6px 0; }
        .coh-propo button { flex: 1 1 140px; font-size: 14px; padding: 10px 8px; }
        .coh-propo button.conseille { border-color: var(--accent); }
        .coh-propo button.applique { background: var(--accent); color: #fff; }
        .combler-zone { margin: 8px 0; }
        .combler-choix { display: flex; gap: 8px; overflow-x: auto; padding: 6px 0; -webkit-overflow-scrolling: touch; }
        .combler-choix img { height: 96px; width: auto; flex: 0 0 auto; border-radius: 6px; border: 2px solid transparent; cursor: pointer; }
        .combler-choix img:hover { border-color: var(--accent); }
        .poster-cell { position: relative; }
        .poster-cell .coh-badge { position: absolute; left: 5px; right: 5px; bottom: 8px; padding: 1px 3px; border-radius: 4px; background: rgba(0, 0, 0, 0.75); color: #fff; font-size: 10px; line-height: 1.25; text-align: center; pointer-events: none; }
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
<style>
/* 👥 Compte copain : créateur de posters seulement, envoi vers SON Emby */
body.copain #mouSettingsOpen, body.copain #mouLogOpen, body.copain button[onclick="sendToMouflanga()"] { display:none !important; }
.copain-barre { display:flex; flex-wrap:wrap; align-items:center; gap:8px; margin:0 0 12px; padding:10px 12px; border-radius:8px; background:rgba(82,181,75,.1); border:1px solid rgba(82,181,75,.4); font-size:14px; }
.copain-barre b { margin-right:auto; }
.copain-barre button { width:auto; padding:6px 10px; font-size:13px; }
.copain-form { flex-basis:100%; display:none; gap:6px; flex-direction:column; }
.copain-form.ouvert { display:flex; }
.copain-form small { color:#aaa; }
.copain-existant { margin:10px 0; padding:10px; border-radius:8px; background:#1f1f1f; display:flex; gap:10px; align-items:center; font-size:14px; }
.copain-existant img { width:70px; border-radius:4px; }
.copain-existant .act { display:flex; flex-direction:column; gap:6px; }
.copain-existant button, .copain-existant a { width:auto; padding:6px 10px; font-size:13px; }
/* Gestes au téléphone (ui/gestes.js) : curseurs à glisser de côté ou − / +, aperçu à recadrer seulement en mode ✋ */
.curseur { display:flex; align-items:center; gap:8px; touch-action:pan-y; }
.curseur input[type=range] { flex:1; min-width:0; margin:0; }
.curseur .pas { flex:none; width:42px !important; height:36px; padding:0 !important; margin:0 !important; font-size:18px; line-height:1; }
@media (pointer: coarse) { .curseur input[type=range] { pointer-events:none; } .souris-seule { display:none; } }
@media (pointer: fine) { .doigt-seul { display:none !important; } }
#recadrerBtn { margin-bottom:4px; }
#recadrerBtn.actif { background:var(--accent, #52b54b) !important; color:#fff !important; }
#miniApercu { position:fixed; right:12px; bottom:12px; z-index:60; width:78px !important; padding:0 !important; margin:0 !important; border:2px solid var(--accent, #52b54b) !important; border-radius:6px; overflow:hidden; background:#000 !important; box-shadow:0 4px 16px rgba(0,0,0,.7); line-height:0; }
#miniApercu img { display:block; width:100%; height:auto; }
#retourEdition { position:fixed; right:12px; bottom:12px; z-index:60; width:auto !important; margin:0 !important; padding:10px 14px !important; box-shadow:0 4px 16px rgba(0,0,0,.7); }
</style>
<link rel="stylesheet" href="{{ skin_url(request.args.get('skin') == 'off') }}">
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
                <!-- Venue depuis MouFlopening (bouton « 🖼 Poster dans MouFloster ») -->
                <div class="lib-status ok" id="venueBandeau" hidden style="margin-bottom:12px">
                    <span id="venueTexte"></span><br>
                    <a id="venueRetour" class="mfg-retour" hidden></a>
                </div>
                <!-- 🆕 Nouveautés : ajoutés dans Emby, pas encore de poster fait -->
                <div class="section" id="nouvPanel" hidden>
                    <h3>🆕 Nouveautés sans poster <span id="nouvCount"></span></h3>
                    <div class="nouv-barre"><label for="nouvChoix">Remonter à</label><select id="nouvChoix" aria-label="Remonter à"></select></div>
                    <div id="nouvListe"></div>
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

                <!-- Posters -->
                <div class="section">
                    <h3>🖼️ Posters</h3>
                    <div class="poster-tools">
                        <select id="langSelect" style="display:none" aria-label="Langue des posters"></select>
                        <label class="upload-btn" for="uploadFile">⬆️ Envoyer mon image</label>
                        <input type="file" id="uploadFile" accept="image/*" style="display:none" />
                        <button type="button" class="secondary" id="eraseOpen">🧽 Effacer un élément</button>
                        <button type="button" class="secondary" id="cohTri" hidden>🎯 Trié par ressemblance</button>
                    </div>
                    <div class="upload-astuce">Pas d'affiche trouvée (manga sans anime…) ? Tape juste le nom dans la recherche, puis « ⬆️ Envoyer mon image » ou glisse l’image sur la page.</div>
                    <div id="layoutBar"></div>
                    <div id="cohProgres" class="coh-progres" hidden><span></span><div class="barre"><i></i></div></div>
                    <div id="cohBande" class="coh-bande" hidden></div>
                    <div id="cohInfo" class="coh-info" hidden></div>
                    <div id="cohAlign" class="coh-align" hidden>
                        <div class="checkbox-item"><input type="checkbox" id="cohAuto" checked /><label for="cohAuto">🎯 Aligner automatiquement le personnage quand je choisis un poster</label></div>
                        <div id="cohPropo" class="coh-propo"></div>
                        <div id="cohAlignEtat" class="coh-info" hidden></div>
                    </div>
                    <div class="loading" id="postersLoading">Chargement...</div>
                    <div id="posters" class="results"></div>
                </div>

                <!-- Options -->
                <div class="section">
                    <h3>⚙️ Cadre & Dégradé</h3>
                    <div class="checkbox-group">
                        <div class="checkbox-item">
                            <input type="checkbox" id="cadreOn" checked />
                            <label for="cadreOn">Cadre blanc</label>
                        </div>
                        <div class="cadre-reglage" id="cadreReglage">
                            <label for="cadrePx">Épaisseur : <b id="cadrePxVal">22</b> px</label>
                            <input type="range" id="cadrePx" min="2" max="120" step="1" value="22" />
                            <div class="cadre-presets"><button type="button" class="secondary" data-cadre="22">Standard 22</button><button type="button" class="secondary" data-cadre="45">Anime 45</button><button type="button" class="secondary" data-cadre="5">Fin 5</button></div>
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
                    <div class="taille-info doux" style="margin:-4px 0 10px">Astuce : tape « et/la » (avec une barre) pour empiler deux petits mots en tout petit, comme « ANT-MAN et/la GUÊPE ».<br>Pour réduire un seul mot, colle une étoile devant : « NOËL *DE CHIEN » écrit « de » en petit.</div>

                    <div class="text-layer">
                        <div class="checkbox-item">
                            <input type="checkbox" id="text1Enabled" />
                            <label for="text1Enabled">Ligne 1 (49px)</label>
                        </div>
                        <input type="text" id="text1" placeholder="Ex: Le" disabled />
                        <div class="taille-info" id="text1Info"></div>
                    </div>

                    <div class="text-layer">
                        <div class="checkbox-item">
                            <input type="checkbox" id="text2Enabled" />
                            <label for="text2Enabled">Ligne 2 (89px)</label>
                        </div>
                        <input type="text" id="text2" placeholder="Titre principal" disabled />
                        <div class="taille-info" id="text2Info"></div>
                    </div>

                    <div class="text-layer">
                        <div class="checkbox-item">
                            <input type="checkbox" id="text3Enabled" />
                            <label for="text3Enabled">Ligne 3 (89px)</label>
                        </div>
                        <input type="text" id="text3" placeholder="Suite du titre" disabled />
                        <div class="taille-info" id="text3Info"></div>
                    </div>

                    <div class="text-layer">
                        <div class="checkbox-item">
                            <input type="checkbox" id="text4Enabled" />
                            <label for="text4Enabled">Ligne 4 (49px) · sous un titre d'1 ligne</label>
                        </div>
                        <input type="text" id="text4" placeholder="Ex: et le Roi" disabled />
                        <div class="taille-info" id="text4Info"></div>
                    </div>

                    <div class="text-layer">
                        <div class="checkbox-item">
                            <input type="checkbox" id="text5Enabled" />
                            <label for="text5Enabled">Ligne 5 (49px) · sous un titre de 2 lignes</label>
                        </div>
                        <input type="text" id="text5" placeholder="Ex: Saison 2" disabled />
                        <div class="taille-info" id="text5Info"></div>
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
                    <div class="lib-status" id="genAuto" hidden></div>
                    <div class="loading" id="previewLoading">Génération...</div>
                    <div class="preview-box">
                        <div class="preview-image" id="previewImage">
                            <div class="preview-placeholder">Aperçu</div>
                        </div>
                    <div class="crop-box">
                        <div class="crop-title">Recadrage <span class="doigt-seul">— « ✋ Recadrer au doigt » (ou appui long sur l'image), puis glisse l'image ; deux doigts pour zoomer. Sinon, les curseurs : glisse-les de côté, ou − / +.</span><span class="souris-seule">— glisse directement l'image ci-dessus, ou utilise les curseurs</span></div>
                        <button type="button" class="secondary doigt-seul" id="recadrerBtn">✋ Recadrer au doigt</button>
                        <label for="cropX">← gauche / droite →</label>
                        <input type="range" id="cropX" min="0" max="100" value="50" />
                        <label for="cropY">↑ haut / bas ↓</label>
                        <input type="range" id="cropY" min="0" max="100" value="50" />
                        <label for="zoom">🔍 Zoom : <span id="zoomVal">100</span> % <small>(molette sur l'image possible)</small></label>
                        <input type="range" id="zoom" min="50" max="300" value="100" />
                        <button type="button" class="secondary" id="cropReset">Recentrer / zoom 100 %</button>
                        <button type="button" class="secondary" id="etendreBtn" title="Quand l'image est plus petite que le cadre (bandes floues), invente le décor qui manque">✨ Inventer le décor qui manque</button>
                        <button type="button" class="secondary" id="etendreAnnuler" hidden>↩️ Revenir à l'image d'avant</button>
                        <div class="taille-info doux" id="etendreInfo" hidden></div>
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

    <!-- Au téléphone : petit aperçu collé en bas tant que le grand est hors de l'écran, et retour à l'endroit quitté -->
    <button type="button" id="miniApercu" hidden aria-label="Voir l'aperçu"><img alt="" /></button>
    <button type="button" id="retourEdition" hidden>↩ Retour aux réglages</button>

    <!-- Fenêtre: effacer un logo / texte -->
    <div id="depotVoile" hidden>⬆️ Lâche l’image ici</div>
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
            <div class="combler-zone">
                <button type="button" class="secondary" id="combleOuvrir">🩹 Combler avec une 2ᵉ image</button>
                <div id="comblePanel" hidden>
                    <div class="modal-sub">Colorie d'abord le texte à enlever, puis choisis une image où les mêmes personnages sont visibles (même si le montage est différent) : MouFloster la remet en place, personnage par personnage, et recopie les morceaux qui manquent. Ce qu'elle ne montre pas est comblé comme d'habitude.</div>
                    <label class="upload-btn" for="combleFichier">⬆️ Envoyer mon image</label>
                    <input type="file" id="combleFichier" accept="image/*" style="display:none" />
                    <div class="modal-sub" id="combleInfo"></div>
                    <div id="combleChoix" class="combler-choix"></div>
                </div>
            </div>
            <div class="lib-status" id="eraseStatus" style="display:none"></div>
            <div class="lib-actions">
                <button class="danger" id="eraseApply">Effacer la zone coloriée</button>
                <button class="secondary" id="eraseClear">Effacer mon tracé</button>
                <button class="secondary" id="eraseUndo" disabled>↩ Annuler le dernier effacement</button>
                <button class="success" id="eraseUse">Utiliser ce poster</button>
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
                <label for="libEpisode" style="margin-top:8px">Numéro d'épisode <span style="font-weight:normal;color:var(--muted)">(vide = poster de la saison ; rempli = image de cet épisode : choisis-le dans la liste ou tape son numéro)</span></label>
                <select id="libEpisodeChoix" hidden style="margin-bottom:6px" aria-label="Choisir l’épisode" onchange="libChoixEpisode()"></select>
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
        ['text1', 'text2', 'text3', 'text4', 'text5', 'season', 'cadreOn', 'showGradient'].forEach(id => {
            document.getElementById(id).addEventListener('change', generatePreview);
        });
        // Cadre : activé ou non, épaisseur au curseur (préréglages 22 / 45 / 5) ; la dernière épaisseur est retenue sur l'appareil
        function styleCadre() {
            return document.getElementById('cadreOn').checked ? String(document.getElementById('cadrePx').value) : '0';
        }
        function majCadre() {
            document.getElementById('cadrePxVal').textContent = document.getElementById('cadrePx').value;
            document.getElementById('cadreReglage').style.opacity = document.getElementById('cadreOn').checked ? '1' : '.4';
            try { localStorage.setItem('mfCadrePx', document.getElementById('cadrePx').value); localStorage.setItem('mfCadreOn', document.getElementById('cadreOn').checked ? '1' : '0'); } catch (e) {}
        }
        try {
            const px = localStorage.getItem('mfCadrePx'), on = localStorage.getItem('mfCadreOn');
            if (px) document.getElementById('cadrePx').value = px;
            if (on !== null) document.getElementById('cadreOn').checked = on === '1';
        } catch (e) {}
        majCadre();
        document.getElementById('cadreOn').addEventListener('change', majCadre);
        document.getElementById('cadrePx').addEventListener('input', () => { majCadre(); generatePreview(true); });
        document.querySelectorAll('[data-cadre]').forEach(b => b.addEventListener('click', () => {
            document.getElementById('cadreOn').checked = true;
            document.getElementById('cadrePx').value = b.dataset.cadre;
            majCadre(); generatePreview();
        }));
        ['text1Enabled', 'text2Enabled', 'text3Enabled', 'text4Enabled', 'text5Enabled', 'seasonEnabled'].forEach(id => {
            document.getElementById(id).addEventListener('change', generatePreview);
        });
        // Glisser le poster directement sur l'aperçu pour le déplacer dans le cadre.
        // Au doigt, seulement en mode « ✋ Recadrer » (bouton ou appui long sur l'image) : sinon un doigt posé sur
        // l'aperçu fait défiler la page sans rien changer. En mode recadrage, deux doigts écartés ou rapprochés = zoom.
        // À la souris, on glisse directement comme avant.
        function modeRecadrage(on) {
            const box = document.getElementById('previewImage'), b = document.getElementById('recadrerBtn');
            box.classList.toggle('recadrage', !!on);
            if (b) { b.textContent = on ? '✓ Recadrage fini' : '✋ Recadrer au doigt'; b.classList.toggle('actif', !!on); }
        }
        (function setupPreviewDrag() {
            const box = document.getElementById('previewImage');
            const doigts = new Map();
            let drag = null, pince = null, appuiLong = null;
            const zoomEl = document.getElementById('zoom');
            const debut = (ev, img) => ({
                x: ev.clientX, y: ev.clientY,
                cx: document.getElementById('cropX').value / 100,
                cy: document.getElementById('cropY').value / 100,
                scale: 1000 / img.getBoundingClientRect().width
            });
            const ecart = () => { const [a, b] = [...doigts.values()]; return Math.hypot(a.x - b.x, a.y - b.y) || 1; };
            box.addEventListener('pointerdown', (ev) => {
                const img = box.querySelector('img');
                if (!img || ev.target !== img) return;
                const doigt = ev.pointerType === 'touch';
                if (doigt && !box.classList.contains('recadrage')) {
                    // appui long (½ s sans bouger) : passe en mode recadrage ; si le doigt bouge avant, c'est un défilement
                    clearTimeout(appuiLong && appuiLong.t);
                    const id = ev.pointerId;
                    appuiLong = {x: ev.clientX, y: ev.clientY, x0: ev.clientX, y0: ev.clientY, t: setTimeout(() => {
                        const p = appuiLong; appuiLong = null; modeRecadrage(true);
                        if (navigator.vibrate) navigator.vibrate(15);
                        try { box.setPointerCapture(id); } catch (e) {}
                        doigts.set(id, {x: p.x, y: p.y});
                        if (box.classList.contains('draggable')) { drag = debut({clientX: p.x, clientY: p.y}, img); box.classList.add('dragging'); }
                    }, 500)};
                    return;
                }
                if (!doigt && !box.classList.contains('draggable')) return;
                ev.preventDefault();
                try { box.setPointerCapture(ev.pointerId); } catch (e) {}
                doigts.set(ev.pointerId, {x: ev.clientX, y: ev.clientY});
                if (doigts.size === 2) { drag = null; pince = {d: ecart(), z: Number(zoomEl.value)}; return; }
                if (box.classList.contains('draggable')) { drag = debut(ev, img); box.classList.add('dragging'); }
            });
            box.addEventListener('pointermove', (ev) => {
                if (appuiLong && Math.hypot(ev.clientX - appuiLong.x0, ev.clientY - appuiLong.y0) > 10) { clearTimeout(appuiLong.t); appuiLong = null; }
                else if (appuiLong) { appuiLong.x = ev.clientX; appuiLong.y = ev.clientY; }
                if (!doigts.has(ev.pointerId)) return;
                doigts.set(ev.pointerId, {x: ev.clientX, y: ev.clientY});
                if (pince && doigts.size === 2) {
                    const z = Math.round(Math.min(300, Math.max(50, pince.z * ecart() / pince.d)));
                    if (z !== Number(zoomEl.value)) { zoomEl.value = z; zoomEl.dispatchEvent(new Event('input')); }
                    return;
                }
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
            const stop = (ev) => {
                if (appuiLong) { clearTimeout(appuiLong.t); appuiLong = null; }
                doigts.delete(ev.pointerId);
                if (doigts.size < 2) pince = null;
                if (!doigts.size) { drag = null; box.classList.remove('dragging'); }
            };
            box.addEventListener('pointerup', stop);
            box.addEventListener('pointercancel', stop);
            box.addEventListener('contextmenu', (ev) => { if (ev.target.closest('img')) ev.preventDefault(); });   // pas de menu « enregistrer l'image » sur l'appui long
            // appui long réussi : le doigt resté posé glisse tout de suite l'image, sans faire défiler la page
            box.addEventListener('touchmove', (ev) => { if (box.classList.contains('recadrage')) ev.preventDefault(); }, {passive: false});
            document.getElementById('recadrerBtn').addEventListener('click', () => modeRecadrage(!box.classList.contains('recadrage')));
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

        // opts.choisir = {id, media_type} : ce titre est choisi tout seul s'il est dans les résultats (nouveautés, reprise)
        async function search(opts) {
            const query = document.getElementById('searchQuery').value.trim();
            if (!query) return null;
            const choisir = opts && opts.choisir;
            let choisi = null;

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
                    div.addEventListener('click', () => {
                        // clic sur le titre déjà choisi (liste refermée): on rouvre la liste pour en changer
                        if (div.classList.contains('active') && resultsDiv.classList.contains('collapsed')) {
                            resultsDiv.classList.remove('collapsed');
                            return;
                        }
                        const autre = !selectedItem || selectedItem.id !== item.id || selectedItem.media_type !== item.media_type;
                        selectedItem = item;
                        const titrePret = preremplirTitre(item.title, autre);
                        div._chargement = Promise.all([loadPosters(item), titrePret]).then(r => r[0]);
                        document.querySelectorAll('.result-item').forEach(el => el.classList.remove('active'));
                        div.classList.add('active');
                        resultsDiv.classList.add('collapsed');  // la liste se referme: seul le titre choisi reste affiché
                    });

                    resultsDiv.appendChild(div);
                    if (choisir && !choisi && String(item.id) === String(choisir.id) && item.media_type === choisir.media_type) choisi = div;
                });
                proposerPosterLibre([query, (() => { try { return mfgVenue.serie || ''; } catch (e) { return ''; } })()], resultsDiv);
                proposerSeriesMouflanga(query, resultsDiv);
                if (choisi) { choisi.click(); await choisi._chargement; return selectedItem; }
            } finally {
                document.getElementById('searchLoading').classList.remove('show');
            }
            return null;
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
            let list = code === 'all' ? allPosters : allPosters.filter(p => p.lang === code);
            const notes = (coherence && coherence.scores) || {};
            if (coherence && coherence.refs.length && cohTriActif) {          // les plus ressemblants d'abord, les non comparés à la fin
                const rang = p => (notes[p.path] && notes[p.path].rang !== undefined) ? notes[p.path].rang : -1;
                list = list.map((p, i) => [p, i]).sort((a, b) => (rang(b[0]) - rang(a[0])) || (a[1] - b[1])).map(x => x[0]);
            }
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
                    cohAligner(poster);                                  // 🎯 propose (et applique si coché) le cadrage qui colle à la série
                };
                const note = notes[poster.path];
                if (note && coherence.refs.length) {
                    const cell = document.createElement('div');
                    cell.className = 'poster-cell';
                    const badge = document.createElement('span');
                    badge.className = 'coh-badge';
                    badge.textContent = (note.score !== undefined ? note.score + ' %' : '') + ' ' + cohTexteCourt(note.texte);
                    img.title = (img.title ? img.title + ' · ' : '') + cohPhrase(note);
                    cell.appendChild(img);
                    cell.appendChild(badge);
                    postersDiv.appendChild(cell);
                } else {
                    postersDiv.appendChild(img);
                }
            });
        }

        // ===== 🎯 Cohérence : posters déjà dans la médiathèque (série ou saga), classement par ressemblance, alignement du personnage =====
        let coherence = null;                 // {refs: [{rel, name, label}], scores: {chemin TMDB: {score, rang, raison, texte, proche_de}}, message}
        let cohTriActif = true;
        let cohJeton = 0, cohJetonAlign = 0, cohMinuteur = null;
        let cohAuto = true;
        try { cohAuto = localStorage.getItem('mfCohAuto') !== '0'; } catch (e) {}
        const cohLibelles = {largeur: '↔️ Même largeur', hauteur: '↕️ Même hauteur', tete: '🙂 Même place de la tête', visage: '🧑 Même visage (taille et place)'};
        const cohTypes = ['tv', 'movie', 'collection'];
        function cohTexteCourt(t) {
            return {sans: '✅ sans texte', facile: '🧽 texte facile à effacer', moyen: '🧽 texte moyen', difficile: '⚠️ texte difficile'}[t] || '';
        }
        function cohPhrase(n) {
            const morceaux = [];
            if (n.score !== undefined) morceaux.push('Ressemble à ' + (n.proche_de || 'tes posters') + ' à ' + n.score + ' % (' + n.raison + ')');
            const t = cohTexteCourt(n.texte);
            if (t) morceaux.push(t.replace('🧽 ', '').replace('✅ ', '').replace('⚠️ ', '') + (n.texte === 'sans' ? '' : ' (estimation)'));
            return morceaux.join(' · ');
        }
        function cohSaison() {
            if (document.getElementById('seasonSpecial').checked) return '0';
            if (!document.getElementById('seasonEnabled').checked) return '';
            return document.getElementById('season').value;
        }
        function cohBase(item) {
            return {media_type: item.media_type, tmdb_id: item.id, title: item.title, original_title: item.original_title || '', year: item.year, season_text: cohSaison()};
        }
        // Barre de progression : pct = null → travail en cours sans pourcentage (barre qui va et vient) ; texte vide → cachée
        function cohProgres(texte, pct) {
            const z = document.getElementById('cohProgres');
            if (!texte) { z.hidden = true; return; }
            z.hidden = false;
            z.querySelector('span').textContent = texte;
            z.classList.toggle('indet', pct === null || pct === undefined);
            z.querySelector('i').style.width = (pct === null || pct === undefined) ? '' : Math.max(3, Math.min(100, pct)) + '%';
        }
        function cohDessiner() {
            const bande = document.getElementById('cohBande'), info = document.getElementById('cohInfo'), bouton = document.getElementById('cohTri');
            const align = document.getElementById('cohAlign');
            bande.innerHTML = ''; bande.hidden = true; bouton.hidden = true; align.hidden = true;
            if (!coherence) { info.hidden = true; return; }
            if (!coherence.refs.length) {
                info.textContent = coherence.message || '';
                info.hidden = !info.textContent;
                return;
            }
            const titre = document.createElement('div');
            titre.className = 'coh-titre';
            titre.textContent = '📚 Déjà dans ta médiathèque (le style à suivre)';
            bande.appendChild(titre);
            const rangee = document.createElement('div');
            rangee.className = 'coh-refs';
            bande.appendChild(rangee);
            coherence.refs.forEach(r => {
                const d = document.createElement('div');
                d.className = 'coh-ref';
                const im = document.createElement('img');
                im.alt = r.label;
                im.src = '/api/library/file?rel=' + encodeURIComponent(r.rel) + '&name=' + encodeURIComponent(r.name);
                d.appendChild(im);
                d.appendChild(document.createTextNode(r.label));
                rangee.appendChild(d);
            });
            bande.hidden = false;
            bouton.hidden = false;
            bouton.textContent = cohTriActif ? '🎯 Trié par ressemblance' : '↕️ Ordre TMDB';
            align.hidden = false;
            document.getElementById('cohAuto').checked = cohAuto;
            info.textContent = 'Touche un poster pour voir pourquoi il ressemble (ou pas). Le texte « facile à effacer » est une estimation.';
            info.hidden = false;
        }
        function cohAfficherPourquoi(poster) {
            if (!coherence || !coherence.refs.length) return;
            const n = (coherence.scores || {})[poster.path];
            const info = document.getElementById('cohInfo');
            if (n) { info.textContent = '🎯 ' + cohPhrase(n); info.hidden = false; }
        }
        async function cohAppel(url, corps) {
            try {
                const res = await fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(corps)});
                return res.ok ? await res.json() : null;
            } catch (e) { return null; }
        }
        async function chargerCoherence(item) {
            const jeton = ++cohJeton;
            if (!item || item.libre || !cohTypes.includes(item.media_type) || !allPosters.length) { coherence = null; cohProgres(''); cohDessiner(); return; }
            const code = document.getElementById('langSelect').value || '';
            const memes = allPosters.filter(p => code === 'all' || p.lang === code);
            const autres = allPosters.filter(p => !memes.includes(p));
            const envoyes = memes.concat(autres).slice(0, 60).map(p => ({path: p.path, lang: p.lang || ''}));
            cohProgres('🎯 Recherche des posters déjà dans ta médiathèque…', null);
            const base = cohBase(item);
            let data = await cohAppel('/api/coherence', Object.assign({}, base, {posters: []}));
            if (jeton !== cohJeton || selectedItem !== item) return;       // un autre titre a été choisi entre-temps
            if (!data || !data.refs) { coherence = null; cohProgres(''); cohDessiner(); return; }
            coherence = {refs: data.refs, scores: {}, message: data.message || ''};
            cohDessiner();                                                 // la bande des modèles s'affiche tout de suite
            if (!coherence.refs.length) { cohProgres(''); return; }
            const total = envoyes.length;
            for (let i = 0; i < total; i += 10) {                          // par lots de 10 : la barre avance vraiment
                cohProgres('🎯 Comparaison avec tes posters… ' + i + ' / ' + total + ' (' + Math.round(100 * i / total) + ' %)', 100 * i / total);
                const d = await cohAppel('/api/coherence', Object.assign({}, base, {posters: envoyes.slice(i, i + 10)}));
                if (jeton !== cohJeton || selectedItem !== item) return;
                if (d && d.scores) Object.assign(coherence.scores, d.scores);
            }
            cohProgres('');
            showPosters(document.getElementById('langSelect').value || 'all');
        }
        function cohPlanifier() {
            clearTimeout(cohMinuteur);
            cohMinuteur = setTimeout(() => { if (selectedItem) chargerCoherence(selectedItem); }, 700);
        }
        function cohEtat(texte) {
            const e = document.getElementById('cohAlignEtat');
            e.textContent = texte || '';
            e.hidden = !texte;
        }
        function cohAppliquer(p) {
            const cx = document.getElementById('cropX'), cy = document.getElementById('cropY'), z = document.getElementById('zoom');
            cx.value = Math.round(p.crop_x * 100);
            cy.value = Math.round(p.crop_y * 100);
            z.value = Math.round(p.zoom * 100);
            document.getElementById('zoomVal').textContent = z.value;
            document.querySelectorAll('#cohPropo button').forEach(b => b.classList.toggle('applique', b.dataset.mode === p.mode));
            const avert = [];
            if (p.bords_flous) avert.push('⚠️ le poster est plus petit que le cadre : des bords flous sont ajoutés');
            if (Math.abs(p.ecart_haut) > 60) avert.push('⚠️ la tête ne peut pas rejoindre sa place (écart ' + Math.abs(p.ecart_haut) + ' px sur 1500) : pas assez d’image ' + (p.ecart_haut < 0 ? 'au-dessus' : 'en dessous'));
            if (!p.sur) avert.push('⚠️ je ne suis pas sûre d’avoir bien repéré le personnage : vérifie');
            cohEtat('✅ Cadrage appliqué : ' + cohLibelles[p.mode] + ', zoom ' + z.value + ' %.' + (avert.length ? ' ' + avert.join(' · ') : ' Ajuste avec les curseurs si besoin.'));
            generatePreview();
        }
        async function cohAligner(poster) {
            if (!coherence || !coherence.refs.length || !selectedItem) return;
            const jeton = ++cohJetonAlign, item = selectedItem;
            const propo = document.getElementById('cohPropo');
            propo.innerHTML = '';
            cohEtat('⏳ Je repère le personnage sur ce poster et je le compare à tes posters…');
            const d = await cohAppel('/api/coherence/aligner', Object.assign({}, cohBase(item), {poster_url: poster.url, border_style: styleCadre()}));
            if (jeton !== cohJetonAlign || selectedItem !== item) return;
            if (!d || !d.propositions || !d.propositions.length) { cohEtat((d && d.message) || 'Alignement indisponible pour ce poster : cadre-le à la main.'); return; }
            d.propositions.forEach(p => {
                const b = document.createElement('button');
                b.type = 'button';
                b.className = 'secondary' + (p.conseille ? ' conseille' : '');
                b.dataset.mode = p.mode;
                b.textContent = cohLibelles[p.mode] + (p.conseille ? ' ⭐' : '') + (p.acceptable ? '' : ' ⚠️');
                b.title = 'Zoom ' + Math.round(p.zoom * 100) + ' %' + (p.etendre ? ' · ' + p.part + ' % de décor à inventer' : '');
                if (p.etendre) b.textContent += ' ✨';
                b.onclick = () => p.etendre ? cohVisageEtendre(p) : cohAppliquer(p);
                propo.appendChild(b);
            });
            const meilleur = d.propositions.find(p => p.conseille) || d.propositions[0];
            if (cohAuto && meilleur.acceptable && meilleur.etendre) cohEtat('🧑 Pour mettre le visage à la même place que sur tes posters, il faut inventer ' + meilleur.part + ' % de décor : touche « ' + cohLibelles.visage + ' ✨ » (10 à 30 secondes).');
            else if (cohAuto && meilleur.acceptable) cohAppliquer(meilleur);
            else if (cohAuto) cohEtat('Aucun cadrage automatique assez sûr pour ce poster (personnage trop différent de tes posters, ou pas assez d’image) : essaie une proposition ci-dessus, ou règle à la main.');
            else cohEtat('Choisis un cadrage proposé (⭐ = conseillé d’après tes ' + d.modeles + ' poster(s)), ou règle à la main.');
        }
        document.getElementById('cohAuto').addEventListener('change', (e) => {
            cohAuto = e.target.checked;
            try { localStorage.setItem('mfCohAuto', cohAuto ? '1' : '0'); } catch (err) {}
            if (cohAuto && selectedPoster) cohAligner(selectedPoster);
        });
        document.getElementById('cohTri').addEventListener('click', () => {
            cohTriActif = !cohTriActif;
            cohDessiner();
            showPosters(document.getElementById('langSelect').value || 'all');
        });
        ['season', 'seasonEnabled', 'seasonSpecial'].forEach(id => document.getElementById(id).addEventListener('change', cohPlanifier));

        // ===== ✨ Inventer le décor qui manque (bandes floues d'une image plus petite que le cadre) =====
        let etendrePrecedent = null;
        function etendreAppliquer(d) {
            etendrePrecedent = {poster: selectedPoster, cropX: document.getElementById('cropX').value, cropY: document.getElementById('cropY').value,
                                zoom: document.getElementById('zoom').value};
            selectedPoster = {url: d.url, lang: '', upload: true};
            document.getElementById('cropX').value = Math.round(d.crop_x * 100);
            document.getElementById('cropY').value = Math.round(d.crop_y * 100);
            document.getElementById('zoom').value = Math.round(d.zoom * 100);
            document.getElementById('zoomVal').textContent = Math.round(d.zoom * 100);
            document.getElementById('etendreAnnuler').hidden = false;
            generatePreview();
        }
        async function cohVisageEtendre(p) {
            if (!selectedPoster || !selectedItem) return;
            const item = selectedItem;
            document.querySelectorAll('#cohPropo button').forEach(b => b.disabled = true);
            cohEtat('⏳ J’aligne le visage et j’invente le décor qui manque… (10 à 30 secondes)');
            try {
                const d = await cohAppel('/api/coherence/aligner_etendre', Object.assign({}, cohBase(item), {poster_url: selectedPoster.url, border_style: styleCadre()}));
                if (!d || d.error) { cohEtat('❌ ' + ((d && d.error) || 'Alignement impossible.')); return; }
                if (d.url) etendreAppliquer(d);
                else cohAppliquer(Object.assign({}, p, {zoom: d.zoom, crop_x: d.crop_x, crop_y: d.crop_y, etendre: false}));
                document.querySelectorAll('#cohPropo button').forEach(b => b.classList.toggle('applique', b.dataset.mode === 'visage'));
                cohEtat('✅ ' + d.message + ' « ↩️ Revenir à l’image d’avant » annule.');
            } finally {
                document.querySelectorAll('#cohPropo button').forEach(b => b.disabled = false);
            }
        }

        function etendreMessage(t) { const e = document.getElementById('etendreInfo'); e.textContent = t || ''; e.hidden = !t; }
        document.getElementById('etendreBtn').addEventListener('click', async () => {
            if (!selectedPoster) { etendreMessage('Choisis d’abord un poster.'); return; }
            const b = document.getElementById('etendreBtn');
            b.disabled = true;
            etendreMessage('⏳ J’invente le décor qui manque… (10 à 30 secondes)');
            try {
                const res = await fetch('/api/etendre', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({
                    poster_url: selectedPoster.url, border_style: styleCadre(),
                    crop_x: document.getElementById('cropX').value / 100, crop_y: document.getElementById('cropY').value / 100,
                    zoom: document.getElementById('zoom').value / 100})});
                const d = await res.json();
                if (d.error) { etendreMessage('❌ ' + d.error); return; }
                if (d.rien) { etendreMessage(d.message); return; }
                etendreAppliquer(d);
                etendreMessage('✅ ' + d.message + ' Le cadrage est resté le même ; tu peux maintenant déplacer ou zoomer sans bords flous.');
            } catch (err) {
                etendreMessage('❌ ' + err.message);
            } finally {
                b.disabled = false;
            }
        });
        document.getElementById('etendreAnnuler').addEventListener('click', () => {
            if (!etendrePrecedent) return;
            selectedPoster = etendrePrecedent.poster;
            document.getElementById('cropX').value = etendrePrecedent.cropX;
            document.getElementById('cropY').value = etendrePrecedent.cropY;
            document.getElementById('zoom').value = etendrePrecedent.zoom;
            document.getElementById('zoomVal').textContent = etendrePrecedent.zoom;
            etendrePrecedent = null;
            document.getElementById('etendreAnnuler').hidden = true;
            etendreMessage('');
            generatePreview();
        });

        function selectPoster(poster) {
            selectedPoster = poster;
            document.querySelectorAll('.poster-thumb').forEach(el => el.classList.remove('selected'));
            cohAfficherPourquoi(poster);
            generatePreview();
        }

        // ===== Mise en page mémorisée : reprendre un poster plus tard =====
        const LAYOUT_CHECKS = ['cadreOn', 'showGradient', 'text1Enabled', 'text2Enabled', 'text3Enabled', 'text4Enabled', 'text5Enabled', 'seasonEnabled', 'seasonSpecial'];
        const LAYOUT_VALUES = ['text1', 'text2', 'text3', 'text4', 'text5', 'season', 'cropX', 'cropY', 'zoom', 'cadrePx'];
        function captureLayout() {
            const l = {poster: selectedPoster, checks: {}, values: {}};
            LAYOUT_CHECKS.forEach(id => { l.checks[id] = document.getElementById(id).checked; });
            LAYOUT_VALUES.forEach(id => { l.values[id] = document.getElementById(id).value; });
            return l;
        }
        function layoutCle(item) {
            return item.libre ? {media_type: 'libre', tmdb_id: item.title} : {media_type: item.media_type, tmdb_id: item.id};
        }
        function applyLayout(l) {
            if (l.checks && l.checks.borderAnime !== undefined && !(l.values || {}).cadrePx) {      // poster fait avant le curseur
                l.values = Object.assign({}, l.values, {cadrePx: l.checks.borderAnime ? '45' : '22'});
                l.checks = Object.assign({}, l.checks, {cadreOn: true});
            }
            Object.entries(l.checks || {}).forEach(([id, v]) => { const el = document.getElementById(id); if (el) el.checked = !!v; });
            Object.entries(l.values || {}).forEach(([id, v]) => { const el = document.getElementById(id); if (el) { el.value = v; el.dispatchEvent(new Event('input')); } });
            const en = document.getElementById('seasonEnabled');
            document.getElementById('season').disabled = !en.checked || document.getElementById('seasonSpecial').checked;
            titreJeton++;                                       // un découpage encore en route n'écrase pas cette mise en page
            titreAuto = Object.assign(lignesActuelles(), {saison: ''});   // une nouvelle recherche pourra encore la remplacer
            if (l.poster) {
                selectedPoster = l.poster;
                const pd = document.getElementById('posters');
                if (l.poster.upload && ![...pd.querySelectorAll('img')].some(el => el.getAttribute('src') === l.poster.url)) {
                    const img = document.createElement('img');            // l'image perso de ce poster revient dans la liste
                    img.src = l.poster.url; img.title = 'Mon image'; img.className = 'poster-thumb';
                    img.onclick = () => { selectPoster(l.poster); img.classList.add('selected'); };
                    pd.insertBefore(img, pd.firstChild);
                }
                document.querySelectorAll('.poster-thumb').forEach(el => el.classList.toggle('selected', el.getAttribute('src') === l.poster.url));
            }
            majCadre();
            generatePreview();
        }
        async function rememberLayout() {
            if (!selectedItem || !selectedPoster) return;
            try {
                await fetch('/api/layout', {method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({...layoutCle(selectedItem), layout: captureLayout()})});
            } catch (e) {}
        }
        async function offerLayout(item) {
            const bar = document.getElementById('layoutBar');
            bar.innerHTML = '';
            let data = {};
            const cle = layoutCle(item);
            try { data = await (await fetch('/api/layout?tmdb_id=' + encodeURIComponent(cle.tmdb_id) + '&media_type=' + encodeURIComponent(cle.media_type))).json(); } catch (e) { return; }
            if (!data.layout || selectedItem !== item) return;
            const b = document.createElement('button');
            b.type = 'button'; b.className = 'secondary layout-btn';
            b.textContent = '✏️ Modifier le poster déjà fait' + (data.layout.saved ? ' (' + data.layout.saved + ')' : '');
            b.onclick = () => { applyLayout(data.layout); bar.innerHTML = ''; showMessage('Poster repris tel que tu l’avais fait : modifie-le puis renvoie-le', 'success'); };
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
            coherence = null; cohDessiner();                     // nouveau titre : l'ancienne comparaison ne vaut plus

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
                chargerCoherence(item);                           // en arrière-plan : les posters s'affichent déjà

                if (data.default !== '') {
                    showMessage('Aucun poster sans texte : choisis une autre langue dans la liste si besoin.', 'error');
                }
            } finally {
                document.getElementById('postersLoading').classList.remove('show');
            }
        }

        document.getElementById('langSelect').addEventListener('change', (e) => { showPosters(e.target.value); cohPlanifier(); });

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

        // ===== 🩹 Combler la zone peinte avec une 2e image (mêmes personnages, autre montage) =====
        async function combleOuvrir() {
            const panel = document.getElementById('comblePanel');
            panel.hidden = !panel.hidden;
            if (panel.hidden) return;
            const choix = document.getElementById('combleChoix'), info = document.getElementById('combleInfo');
            choix.innerHTML = '';
            info.textContent = 'Recherche des images du même titre sur TheMovieDB…';
            const sources = [];
            if (selectedItem && selectedItem.id && cohTypes.includes(selectedItem.media_type)) {
                const d = await cohAppel('/api/backdrops', {id: selectedItem.id, media_type: selectedItem.media_type});
                (d && d.backdrops || []).forEach(b => sources.push({url: b.url, vignette: b.vignette, titre: 'Fond d’écran' + (b.lang ? ' (' + langLabel(b.lang) + ')' : ' sans texte')}));
            }
            allPosters.filter(p => p.lang === '').slice(0, 12).forEach(p => sources.push({url: p.url, vignette: p.url, titre: 'Poster sans texte'}));
            info.textContent = sources.length ? 'Touche une image pour l’utiliser (les fonds d’écran sans texte d’abord), ou envoie la tienne.' : 'Aucune image trouvée pour ce titre : envoie la tienne.';
            sources.forEach(src => {
                const im = document.createElement('img');
                im.src = src.vignette; im.title = src.titre; im.alt = src.titre;
                im.onclick = () => eraseCombler(src.url);
                choix.appendChild(im);
            });
        }
        async function eraseCombler(sourceUrl) {
            if (!eraseState.dirty) { eraseStatus('Colorie d’abord le texte à enlever (avec le doigt), puis choisis l’image.', 'warn'); return; }
            const btns = document.querySelectorAll('#eraseModal .lib-actions button');
            btns.forEach(b => b.disabled = true);
            eraseStatus('⏳ Je cherche où cette image correspond à ton poster, puis je comble… (environ 15 secondes)');
            try {
                const res = await fetch('/api/erase/combler', {method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({poster_url: eraseState.url, mask: eraseState.mask.toDataURL('image/png'), source_url: sourceUrl})});
                const data = await res.json();
                if (data.error) { eraseStatus('❌ ' + data.error, 'err'); return; }
                eraseState.history.push(eraseState.url);
                eraseState.url = data.url;
                const r = data.rapport || {};
                eraseStatus('✅ Comblé : ' + Math.round(r.couvert || 0) + ' % de la zone avec ta 2ᵉ image, ' + Math.round(r.reste || 0) + ' % par l’effaceur. Vérifie le résultat : « ↩ Annuler » le défait, « Utiliser ce poster » le garde.' + (data.note ? ' ⚠️ ' + data.note : ''), data.note ? 'warn' : 'ok');
                eraseLoadImage();
            } catch (err) {
                eraseStatus('❌ ' + err.message, 'err');
            } finally {
                btns.forEach(b => b.disabled = false);
                document.getElementById('eraseUndo').disabled = eraseState.history.length === 0;
            }
        }
        async function combleEnvoyer(fichier) {
            if (!fichier) return;
            eraseStatus('⏳ Envoi de l’image…');
            const fd = new FormData();
            fd.append('file', fichier);
            let d = null;
            try { d = await (await fetch('/api/upload', {method: 'POST', body: fd})).json(); } catch (e) { d = null; }
            if (!d || d.error) { eraseStatus('❌ ' + ((d && d.error) || 'Envoi impossible'), 'err'); return; }
            eraseCombler(d.url);
        }
        document.getElementById('combleOuvrir').addEventListener('click', combleOuvrir);
        document.getElementById('combleFichier').addEventListener('change', (e) => { const f = e.target.files[0]; e.target.value = ''; combleEnvoyer(f); });

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

        // Lignes du titre remplies toutes seules (découpage fait par le serveur, voir titre.py : titre, sous-titre,
        // 2 lignes, numéro de suite en grand chiffre gris) si elles sont vides ou contiennent encore ce qui avait été
        // mis automatiquement la fois d'avant ; jamais si tu les as modifiées à la main
        // « nouveau » : un autre titre vient d'être choisi → ses lignes remplacent toujours les anciennes
        const CHAMPS_TITRE = ['text1', 'text2', 'text3', 'text4', 'text5'];
        let titreAuto = null, titreJeton = 0;
        function lignesActuelles() {
            const v = {};
            CHAMPS_TITRE.forEach(id => { v[id] = document.getElementById(id + 'Enabled').checked ? document.getElementById(id).value : ''; });
            return v;
        }
        function lignesIntactes() {
            const v = lignesActuelles();
            if (CHAMPS_TITRE.every(id => v[id].trim() === '')) return true;
            return !!titreAuto && CHAMPS_TITRE.every(id => v[id] === (titreAuto[id] || ''));
        }
        async function preremplirTitre(nom, nouveau) {
            if (!nom || !(nouveau || lignesIntactes())) return;
            const jeton = ++titreJeton;
            let d = null;
            try {
                d = await (await fetch('/api/decoupe', {method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({title: nom, media_type: selectedItem ? selectedItem.media_type : '', border_style: styleCadre()})})).json();
            } catch (e) {}
            if (jeton !== titreJeton) return;                   // un autre titre ou une mise en page reprise est passé entre-temps
            if (!d || d.error || !d.l2) d = {l1: '', l2: nom, l3: '', l4: '', l5: '', saison: ''};
            const auto = {};
            CHAMPS_TITRE.forEach((id, i) => {
                const val = d['l' + (i + 1)] || '';
                document.getElementById(id).value = val;
                document.getElementById(id).disabled = !val;
                document.getElementById(id + 'Enabled').checked = !!val;
                auto[id] = val;
            });
            const en = document.getElementById('seasonEnabled'), s = document.getElementById('season'), sp = document.getElementById('seasonSpecial');
            if (d.saison) { en.checked = true; sp.checked = false; s.value = d.saison; }
            else if (titreAuto && titreAuto.saison && en.checked && s.value === titreAuto.saison) { en.checked = false; s.value = ''; }
            s.disabled = !en.checked || sp.checked;
            auto.saison = d.saison || '';
            titreAuto = auto;
            generatePreview(true);
        }

        // Taille réelle des lettres de chaque ligne (renvoyée par l'aperçu) : on prévient quand une ligne a dû rétrécir
        function afficherTailles(lignes) {
            CHAMPS_TITRE.forEach((id, i) => {
                const info = document.getElementById(id + 'Info'), champ = document.getElementById(id);
                const l = (lignes || []).find(x => x.ligne === i + 1);
                info.className = 'taille-info'; info.textContent = ''; champ.classList.remove('trop-long');
                if (!l || l.mode === 'normal') return;
                if (l.mode === 'petits') {
                    info.classList.add('doux');
                    info.textContent = 'Petits mots écrits en petit pour garder les lettres à ' + l.base + ' px.';
                } else {
                    info.classList.add('rouge'); champ.classList.add('trop-long');
                    info.textContent = '⚠️ Trop long : lettres à ' + l.taille + ' px au lieu de ' + l.base + '. Raccourcis ou coupe autrement.';
                }
            });
        }

        // Poster fait avec une image perso (sans fiche TheMovieDB) : on le retrouve par son nom pour le modifier
        async function proposerPosterLibre(noms, resultsDiv) {
            for (const nom of [...new Set(noms.map(n => (n || '').trim()).filter(Boolean))]) {
                let data = {};
                try { data = await (await fetch('/api/layout?media_type=libre&tmdb_id=' + encodeURIComponent(nom))).json(); } catch (e) { continue; }
                if (!data.layout) continue;
                const div = document.createElement('div');
                div.className = 'result-item';
                div.textContent = '✏️ ' + nom + ' · ton poster perso' + (data.layout.saved ? ' du ' + data.layout.saved : '');
                div.addEventListener('click', () => {
                    if (div.classList.contains('active') && resultsDiv.classList.contains('collapsed')) { resultsDiv.classList.remove('collapsed'); return; }
                    selectedItem = {id: null, title: nom, original_title: '', media_type: 'tv', year: '?', libre: true};
                    preremplirTitre(nom, true);
                    allPosters = [];
                    document.getElementById('posters').innerHTML = '';
                    document.getElementById('langSelect').style.display = 'none';
                    document.getElementById('layoutBar').innerHTML = '';
                    applyLayout(data.layout);
                    document.querySelectorAll('.result-item').forEach(el => el.classList.remove('active'));
                    div.classList.add('active');
                    resultsDiv.classList.add('collapsed');
                    showMessage('Poster repris tel que tu l’avais fait : modifie-le puis renvoie-le', 'success');
                });
                resultsDiv.insertBefore(div, resultsDiv.firstChild);
            }
        }

        // Séries de MouFlanga qui portent ce nom et ont déjà un poster : proposées même sans fiche TheMovieDB (manga sans anime)
        async function proposerSeriesMouflanga(requete, resultsDiv) {
            let data = {};
            try { data = await (await fetch('/api/mouflanga/recherche?q=' + encodeURIComponent(requete))).json(); } catch (e) { return; }
            for (const s of (data.series || []).reverse()) {
                const nom = s.name;
                const div = document.createElement('div');
                div.className = 'result-item';
                div.textContent = '📚 ' + nom + ' · ton poster dans MouFlanga';
                div.addEventListener('click', async () => {
                    if (div.classList.contains('active') && resultsDiv.classList.contains('collapsed')) { resultsDiv.classList.remove('collapsed'); return; }
                    document.getElementById('postersLoading').classList.add('show');
                    try {
                        const res = await fetch('/api/mouflanga/reprendre', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({name: nom})});
                        const rep = await res.json();
                        if (rep.error) { showMessage(rep.error, 'error'); return; }
                        selectedItem = {id: null, title: nom, original_title: '', media_type: 'tv', year: '?', libre: true};
                        preremplirTitre(nom, true);
                        allPosters = [];
                        document.getElementById('langSelect').style.display = 'none';
                        document.getElementById('layoutBar').innerHTML = '';
                        const postersDiv = document.getElementById('posters');
                        postersDiv.innerHTML = '';
                        const poster = {url: rep.url, lang: '', upload: true};
                        const img = document.createElement('img');
                        img.src = rep.url;
                        img.title = 'Poster actuel dans MouFlanga';
                        img.className = 'poster-thumb';
                        img.onclick = () => { selectPoster(poster); img.classList.add('selected'); };
                        postersDiv.appendChild(img);
                        selectPoster(poster);
                        img.classList.add('selected');
                        document.querySelectorAll('.result-item').forEach(el => el.classList.remove('active'));
                        div.classList.add('active');
                        resultsDiv.classList.add('collapsed');
                        showMessage('Poster repris de MouFlanga : modifie-le ou renvoie-le', 'success');
                    } catch (err) {
                        showMessage('Erreur: ' + err.message, 'error');
                    } finally {
                        document.getElementById('postersLoading').classList.remove('show');
                    }
                });
                resultsDiv.insertBefore(div, resultsDiv.firstChild);
            }
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

        // Envoi manuel d'une image : bouton, ou image glissée sur la page (posters, aperçu)
        document.getElementById('uploadFile').addEventListener('change', (e) => {
            const file = e.target.files[0];
            e.target.value = '';
            if (file) envoyerImage(file);
        });
        (function () {
            let profondeur = 0;
            const voile = document.getElementById('depotVoile');
            const avecFichier = ev => ev.dataTransfer && [...ev.dataTransfer.types].includes('Files');
            document.addEventListener('dragenter', ev => { if (!avecFichier(ev)) return; ev.preventDefault(); profondeur++; voile.hidden = false; });
            document.addEventListener('dragover', ev => { if (avecFichier(ev)) ev.preventDefault(); });
            document.addEventListener('dragleave', ev => { if (!avecFichier(ev)) return; profondeur = Math.max(0, profondeur - 1); if (!profondeur) voile.hidden = true; });
            document.addEventListener('drop', ev => {
                if (!avecFichier(ev)) return;
                ev.preventDefault(); profondeur = 0; voile.hidden = true;
                if (document.getElementById('eraseModal') && document.getElementById('eraseModal').classList.contains('show')) return;
                const file = [...ev.dataTransfer.files].find(f => f.type.startsWith('image/'));
                if (!file) return showMessage('Dépose une image (JPG, PNG, WebP…)', 'error');
                envoyerImage(file);
            });
        })();
        async function envoyerImage(file) {
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
        }

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
                        border_style: styleCadre(),
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
                if (!res.ok || !(res.headers.get('Content-Type') || '').startsWith('image/')) {
                    let data = {};
                    try { data = await res.json(); } catch (e) {}
                    showMessage(data.error || 'Erreur ' + res.status, 'error');
                    return;
                }
                // L'aperçu arrive en image JPEG ; le débord (recadrage) et la taille des lignes dans les en-têtes
                const lire = (nom, defaut) => { try { return JSON.parse(res.headers.get(nom)) || defaut; } catch (e) { return defaut; } };
                const url = URL.createObjectURL(await res.blob());
                const previewDiv = document.getElementById('previewImage');
                const ancien = previewDiv.querySelector('img');
                if (ancien) { ancien.src = url; } else { previewDiv.innerHTML = '<img draggable="false" alt="Aperçu" />'; previewDiv.querySelector('img').src = url; }
                if (window.apercuUrl) setTimeout(((u) => () => URL.revokeObjectURL(u))(window.apercuUrl), 2000);
                window.apercuUrl = url;
                document.dispatchEvent(new CustomEvent('apercu', {detail: url}));
                previewOverflow = lire('X-Apercu-Debord', [0, 0]);
                afficherTailles(lire('X-Apercu-Lignes', []));
                previewDiv.classList.toggle('draggable', previewOverflow[0] > 1 || previewOverflow[1] > 1);
                // plus de message « Aperçu généré » : l'image suffit, et la bulle cachait le petit aperçu au téléphone
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
                        border_style: styleCadre(),
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
        // Ouverture depuis MouFlopening : ?de=MouFlopening&q=<titre>&tmdb=<id>&type=tv|movie&retour=<adresse>
        const venueOpening = (() => {
            const p = new URLSearchParams(location.search), retour = p.get('retour') || '';
            if (p.get('de') !== 'MouFlopening') return {};
            return {de: 'MouFlopening', q: p.get('q') || '', tmdb: /^\d+$/.test(p.get('tmdb') || '') ? Number(p.get('tmdb')) : null,
                    type: p.get('type') === 'movie' ? 'movie' : 'tv', retour: /^https?:\/\//i.test(retour) ? retour : '',
                    saison: /^\d{1,3}$/.test(p.get('saison') || '') ? Number(p.get('saison')) : null};
        })();
        function venueBandeau(fini) {
            document.getElementById('venueBandeau').hidden = false;
            document.getElementById('venueTexte').textContent = fini
                ? '✅ Poster de « ' + venueOpening.q + ' » envoyé dans la médiathèque.'
                : '🖼️ Poster pour « ' + venueOpening.q + ' » (envoyé par MouFlopening) : choisis une affiche, personnalise-la, puis « 📁 Envoyer vers la médiathèque ».';
            const a = document.getElementById('venueRetour');
            a.hidden = !venueOpening.retour; a.href = venueOpening.retour;
            a.textContent = fini ? '↩ Retour à MouFlopening' : '↩ Annuler et revenir';
        }
        function venueFini() {
            venueBandeau(true);
            if (!venueOpening.retour) return;
            MouConfirm('Poster envoyé 🎉\n\nRevenir à MouFlopening ?', {ok: 'Revenir', annuler: 'Rester ici'}).then(oui => { if (oui) location.href = venueOpening.retour; });
        }
        document.addEventListener('DOMContentLoaded', async () => {
            if (!venueOpening.de || !venueOpening.q) return;
            venueBandeau(false);
            document.getElementById('searchQuery').value = venueOpening.q;
            const item = await search({choisir: venueOpening.tmdb ? {id: venueOpening.tmdb, media_type: venueOpening.type} : null});
            if (venueOpening.saison !== null) {                   // une saison choisie dans MouFlopening : son poster
                const sp = document.getElementById('seasonSpecial'), en = document.getElementById('seasonEnabled'), s = document.getElementById('season');
                en.checked = true; sp.checked = venueOpening.saison === 0;
                if (venueOpening.saison > 0) s.value = String(venueOpening.saison);
                s.disabled = sp.checked;
            }
            if (item) { generatePreview(); document.getElementById('posters').scrollIntoView({behavior: 'smooth', block: 'start'}); }
            else showMessage('Choisis le bon titre dans la liste de recherche', 'error');
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
            const texte = 'Couverture envoyée 🎉\n\nLa couverture de « ' + nom + ' » est dans MouFlanga' + (sauvegarde ? ' (l’ancienne est sauvegardée).' : '.');
            if (!mfgVenue.retour) return MouAlert(texte);
            return MouConfirm(texte + '\n\nRevenir à MouFlanga ?', {ok: 'Revenir', annuler: 'Rester ici'}).then(oui => { if (oui) location.href = mfgVenue.retour; });
        }
        // Adresse de MouFlopening pour le navigateur : celle du réseau local ou l'adresse perso selon d'où l'on vient ;
        // rien de réglé mais MouFlopening installé sur ce serveur : même machine, son port
        async function adresseMouflopening() {
            let a = {};
            try { a = await (await fetch('/api/settings/mouflopening')).json(); } catch (e) { return ''; }
            const h = location.hostname;
            const local = /^(localhost|127\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)/.test(h) || /\.(local|lan|home)$/.test(h);
            const base = local ? (a.url || a.url_externe) : (a.url_externe || a.url);
            if (base) return base.replace(/\/$/, '');
            return a.ici && local ? location.protocol + '//' + h + ':' + a.port : '';
        }
        function lienMouflopening(base, dossier) {
            return base + '/?dossier=' + encodeURIComponent(dossier) + '&de=MouFloster&retour=' + encodeURIComponent(location.origin + location.pathname + '?reprise=1');
        }
        // Pas encore de générique : proposer d'ouvrir MouFlopening directement sur ce titre (raison : pourquoi il n'a rien trouvé seul)
        async function proposerGenerique(titre, dossier, raison) {
            const base = await adresseMouflopening();
            if (!base) return;
            const texte = raison
                ? 'Pas encore de générique 🎵\n\nMouFlopening n’a pas trouvé seul de générique pour « ' + titre + ' » (' + raison + '). Tu peux le choisir à l’écoute : il s’ouvre directement sur ce titre.\n\nAller sur MouFlopening ?'
                : 'Pas encore de générique 🎵\n\n« ' + titre + ' » n’a pas encore de générique dans la médiathèque. MouFlopening peut le trouver : il s’ouvre directement sur ce titre et lance la recherche.\n\nAller sur MouFlopening ?';
            if (await MouConfirm(texte, {ok: 'Aller', annuler: 'Rester ici'})) { garderFiche(); location.href = lienMouflopening(base, dossier); }
        }
        // Après l'envoi d'un poster : MouFlopening cherche lui-même le générique validé, sans quitter la page.
        // Il n'est proposé de l'ouvrir que s'il n'a rien trouvé (ou s'il ne répond pas).
        async function chercherGenerique(titre, dossier) {
            let r = {};
            try { r = await (await fetch('/api/generique/auto', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({dossier})})).json(); } catch (e) {}
            if (!r.ok || !r.job) {
                if (r.ok && r.deja) return;
                return proposerGenerique(titre, dossier);
            }
            const bandeau = document.getElementById('genAuto');
            bandeau.hidden = false; bandeau.className = 'lib-status';
            bandeau.textContent = '🎵 MouFlopening cherche le générique de « ' + titre + ' »…';
            const fin = Date.now() + 4 * 60 * 1000;
            while (Date.now() < fin) {
                await new Promise(ok => setTimeout(ok, 1500));
                let st = {};
                try { st = await (await fetch('/api/generique/job/' + r.job)).json(); } catch (e) { continue; }
                if (st.error) break;
                const etape = (st.steps || []).slice(-1)[0];
                if (!st.done) { if (etape) bandeau.textContent = '🎵 MouFlopening : ' + etape.text; continue; }
                const res = (st.result && st.result[0]) || {};
                if (res.ok) {
                    bandeau.className = 'lib-status ok';
                    bandeau.textContent = '🎵 Générique posé par MouFlopening pour « ' + titre + ' » : ' + (res.message || '');
                    setTimeout(() => { bandeau.hidden = true; }, 15000);
                } else {
                    bandeau.hidden = true;
                    proposerGenerique(titre, dossier, (res.message || res.error || '').replace(/^⏳ /, ''));
                }
                return;
            }
            bandeau.hidden = true;
            proposerGenerique(titre, dossier);
        }
        // Retour de MouFlopening : la fiche revient telle qu'elle était (recherche, titre, affiche, textes, saison)
        function garderFiche() {
            if (!selectedItem) return;
            try {
                localStorage.setItem('mfReprise', JSON.stringify({t: Date.now(), q: document.getElementById('searchQuery').value,
                    item: selectedItem, layout: captureLayout()}));
            } catch (e) {}
        }
        async function reprendreFiche() {
            let f = null;
            try { f = JSON.parse(localStorage.getItem('mfReprise') || 'null'); localStorage.removeItem('mfReprise'); } catch (e) {}
            history.replaceState(null, '', location.pathname);
            if (!f || !f.item || Date.now() - (f.t || 0) > 24 * 3600 * 1000) return;
            document.getElementById('searchQuery').value = f.q || f.item.title || '';
            if (!f.item.libre) await search({choisir: f.item});
            if (!selectedItem || selectedItem.id !== f.item.id) {           // titre absent des résultats : on le reprend quand même
                selectedItem = f.item;
                if (!f.item.libre) await loadPosters(f.item);
            }
            if (f.layout) applyLayout(f.layout);
            showMessage('↩ Fiche de « ' + f.item.title + ' » reprise : tu peux enchaîner (saisons…)', 'success');
        }
        document.addEventListener('DOMContentLoaded', () => {
            if (new URLSearchParams(location.search).get('reprise') === '1') reprendreFiche();
        });

        // ===== 🆕 Nouveautés : ajoutés dans Emby, pas encore de poster fait =====
        let nouvElements = [], nouvTout = false;
        let nouvMouflopening = '';             // adresse de MouFlopening (bouton 🎵 des nouveautés), '' si pas relié
        async function nouvInit() {
            adresseMouflopening().then(base => { nouvMouflopening = base; if (nouvElements.length) nouvAfficher(); });
            let r = {};
            try { const res = await fetch('/api/settings/nouveautes'); if (!res.ok) return; r = await res.json(); } catch (e) { return; }
            const sel = document.getElementById('nouvChoix');
            const debut = r.debut ? new Date(r.debut).toLocaleDateString('fr-FR') : '';
            (r.choix || []).forEach(c => { const o = document.createElement('option'); o.value = c.valeur; o.textContent = c.libelle + (c.valeur === 'debut' && debut ? ' (' + debut + ')' : ''); sel.appendChild(o); });
            sel.value = r.defaut;                         // choix par défaut : ⚙️ Réglages ; le menu change seulement l'affichage du moment
            sel.addEventListener('change', () => { nouvTout = false; nouvCharger(); });
            document.getElementById('nouvPanel').hidden = false;
            nouvCharger();
        }
        async function nouvCharger() {
            const liste = document.getElementById('nouvListe'), sel = document.getElementById('nouvChoix');
            if (document.getElementById('nouvPanel').hidden) return;
            if (!nouvElements.length) liste.innerHTML = '<div class="nouv-vide">Lecture d’Emby…</div>';
            let r = {};
            try { r = await (await fetch('/api/nouveautes?choix=' + encodeURIComponent(sel.value))).json(); } catch (e) { r = {error: 'Serveur injoignable'}; }
            nouvElements = r.elements || [];
            document.getElementById('nouvCount').textContent = nouvElements.length ? '(' + (r.total || nouvElements.length) + ')' : '';
            if (r.error) { liste.innerHTML = ''; const d = document.createElement('div'); d.className = 'nouv-vide'; d.textContent = '⚠️ ' + r.error; liste.appendChild(d); return; }
            nouvAfficher();
        }
        function nouvAfficher() {
            const liste = document.getElementById('nouvListe');
            liste.innerHTML = '';
            if (!nouvElements.length) { liste.innerHTML = '<div class="nouv-vide">Rien à faire : tous les nouveaux titres ont leur poster 🎉</div>'; return; }
            const max = nouvTout ? nouvElements.length : 5;
            nouvElements.slice(0, max).forEach(el => {
                const row = document.createElement('div'); row.className = 'nouv-item';
                const nom = document.createElement('div'); nom.className = 'nouv-nom';
                nom.textContent = el.titre + (el.annee && el.annee !== '?' ? ' (' + el.annee + ')' : '') + ' ';
                const pt = document.createElement('small'); pt.textContent = (el.media_type === 'tv' ? '· série · ' : '· film · ') + el.rel.split('/')[0]; nom.appendChild(pt);
                const act = document.createElement('div'); act.className = 'nouv-act';
                const bouton = (txt, fn, cls) => { const b = document.createElement('button'); b.type = 'button'; b.textContent = txt; if (cls) b.className = cls; b.onclick = fn; act.appendChild(b); };
                if (el.poster) bouton('🖼️ Poster', () => nouvOuvrir(el, null));
                el.saisons.forEach(n => bouton(n === 0 ? 'Spéciaux' : 'Saison ' + n, () => nouvOuvrir(el, n)));
                bouton('✓ Déjà bon', () => nouvDejaBon(el), 'secondary');
                // 🎵 générique : déjà là, ou bouton pour l'ouvrir directement dans MouFlopening
                const gen = document.createElement('span'); gen.className = 'nouv-gen';
                if (el.generique === true) { gen.textContent = '🎵 générique ✓'; gen.title = 'Ce titre a déjà son générique'; }
                else if (el.generique === false && nouvMouflopening) {
                    const a = document.createElement('a'); a.className = 'nouv-gen-lien'; a.textContent = '🎵 Générique';
                    a.href = lienMouflopening(nouvMouflopening, el.dossier); a.title = 'Pas encore de générique : ouvrir MouFlopening sur ce titre';
                    gen.appendChild(a);
                } else if (el.generique === false) gen.textContent = '🔇 pas de générique';
                if (gen.textContent || gen.firstChild) act.appendChild(gen);
                row.append(nom, act); liste.appendChild(row);
            });
            if (nouvElements.length > max) {
                const b = document.createElement('button'); b.type = 'button'; b.className = 'secondary'; b.style.marginTop = '8px';
                b.textContent = 'Voir les ' + (nouvElements.length - max) + ' autres';
                b.onclick = () => { nouvTout = true; nouvAfficher(); };
                liste.appendChild(b);
            }
        }
        async function nouvOuvrir(el, saison) {
            document.getElementById('searchQuery').value = el.titre;
            const item = await search({choisir: el.tmdb ? {id: el.tmdb, media_type: el.media_type} : null});
            const sp = document.getElementById('seasonSpecial'), en = document.getElementById('seasonEnabled'), s = document.getElementById('season');
            if (saison === null) { sp.checked = false; en.checked = false; }
            else if (saison === 0) { sp.checked = true; en.checked = true; }
            else { sp.checked = false; en.checked = true; s.value = String(saison); }
            s.disabled = !en.checked || sp.checked;
            if (item) generatePreview();
            else showMessage('Choisis le bon titre dans la liste de recherche', 'error');
            document.getElementById(item ? 'posters' : 'results').scrollIntoView({behavior: 'smooth', block: 'start'});
        }
        async function nouvDejaBon(el) {
            if (!confirm('Ne plus proposer « ' + el.titre + ' » dans les nouveautés ?')) return;
            try {
                await fetch('/api/nouveautes/deja-bon', {method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({rel: el.rel, poster: el.poster, saisons: el.saisons})});
            } catch (e) {}
            nouvElements = nouvElements.filter(x => x !== el);
            document.getElementById('nouvCount').textContent = nouvElements.length ? '(' + nouvElements.length + ')' : '';
            nouvAfficher();
        }
        document.addEventListener('DOMContentLoaded', nouvInit);
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

        // Liste des épisodes de la saison (spéciaux : souvent des films) : on choisit par le nom, pas par le numéro
        let libEpisodesCle = '';
        const plier = t => (t || '').normalize('NFD').replace(/[\\u0300-\\u036f]/g, '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
        async function libChargerEpisodes(rel) {
            const sel = document.getElementById('libEpisodeChoix'), saison = libType() === 'tv' ? libSeasonText().trim() : '';
            const cle = rel + '|' + saison;
            if (cle === libEpisodesCle) return false;
            libEpisodesCle = cle;
            sel.innerHTML = ''; sel.hidden = true;
            if (!rel || saison === '') return false;
            let data = {};
            try { data = await (await fetch('/api/library/episodes', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({rel, season_text: saison})})).json(); } catch (e) { return false; }
            const eps = data.episodes || [];
            if (libEpisodesCle !== cle || !eps.length) return false;
            const o0 = document.createElement('option'); o0.value = ''; o0.textContent = saison === '0' ? '— Poster des spéciaux (pas un épisode) —' : '— Poster de la saison (pas un épisode) —'; sel.appendChild(o0);
            eps.forEach(e => { const o = document.createElement('option'); o.value = e.episode; o.textContent = 'Épisode ' + e.episode + (e.titre ? ' – ' + e.titre : '') + (e.thumb ? '  🖼️' : ''); sel.appendChild(o); });
            sel.hidden = false;
            const ep = document.getElementById('libEpisode');
            if (ep.value === '' && saison === '0' && selectedItem && selectedItem.media_type === 'movie') {      // un film → le spécial du même nom
                const t = [selectedItem.title, selectedItem.original_title].map(plier).filter(Boolean);
                const hit = eps.find(e => { const n = plier(e.titre); return n && t.some(x => x === n || (x.length >= 6 && (n.includes(x) || x.includes(n)))); });
                if (hit) { ep.value = hit.episode; sel.value = String(hit.episode); return true; }
            }
            sel.value = ep.value;
            return false;
        }
        function libChoixEpisode() {
            document.getElementById('libEpisode').value = document.getElementById('libEpisodeChoix').value;
            libInspect();
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

            // un film n'a pas de saison : seulement « spéciaux » s'il va dans une série (sinon reste d'une série faite juste avant)
            const seasonTxt = document.getElementById('seasonSpecial').checked ? '0' : (!isTv ? '' : (document.getElementById('seasonEnabled').checked ? document.getElementById('season').value : ''));
            const m = seasonTxt.match(/[0-9]+/);
            document.getElementById('libSeason').value = m ? m[0] : '';
            document.getElementById('libSearch').value = '';
            document.getElementById('libEpisode').value = '';
            libEpisodesCle = ''; document.getElementById('libEpisodeChoix').innerHTML = ''; document.getElementById('libEpisodeChoix').hidden = true;
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
                if (has) {
                    libRender(data.target_name, data.inspect);
                    if (await libChargerEpisodes(document.getElementById('libFolder').value)) libInspect();   // spécial trouvé pour ce film
                } else libRender('', null);
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
            await libChargerEpisodes(rel);
            const choix = document.getElementById('libEpisodeChoix');
            if (!choix.hidden) choix.value = [...choix.options].some(o => o.value === document.getElementById('libEpisode').value) ? document.getElementById('libEpisode').value : '';
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
                if (data.generique === false) chercherGenerique(selectedItem.title, data.dossier);
                if (venueOpening.de) venueFini();
                nouvCharger();
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
<script>
// 👥 Compte copain : barre « Mon Emby », envoi vers son Emby, posters déjà faits par l’admin (lecture seule)
(async function () {
    let moi = {};
    try { moi = await (await fetch('/api/moi')).json(); } catch (e) { return; }
    if (moi.role !== 'copain') return;
    document.body.classList.add('copain');
    const barre = document.createElement('div');
    barre.className = 'copain-barre';
    barre.innerHTML = '<b>👤 ' + moi.id + '</b><span id="cpEtat"></span><button type="button" id="cpOuvrir">🔌 Mon Emby</button>' +
        '<div class="copain-form" id="cpForm"><small>Adresse EXTERNE de ton Emby (ex. https://emby.exemple.fr) et une clé API (Emby → Tableau de bord → Clés API). Laisse vide pour seulement télécharger.</small>' +
        '<input type="text" id="cpUrl" placeholder="https://…" autocapitalize="off" spellcheck="false"><input type="password" id="cpCle" placeholder="Clé API (inchangée si vide)" autocomplete="off">' +
        '<button type="button" id="cpSave">Enregistrer</button><span id="cpMsg"></span></div>';
    const cible = document.querySelector('.container') || document.body, entete = document.querySelector('.mou-header');
    if (entete) entete.after(barre); else cible.insertBefore(barre, cible.firstChild);
    const etat = () => { document.getElementById('cpEtat').textContent = moi.emby_url ? 'Emby relié ✅' : 'Emby pas relié : bouton Télécharger seulement'; };
    etat();
    document.getElementById('cpUrl').value = moi.emby_url || '';
    document.getElementById('cpOuvrir').onclick = () => document.getElementById('cpForm').classList.toggle('ouvert');
    document.getElementById('cpSave').onclick = async () => {
        const r = await (await fetch('/api/copain/profil', {method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({emby_url: document.getElementById('cpUrl').value, emby_cle: document.getElementById('cpCle').value})})).json();
        document.getElementById('cpMsg').textContent = r.message || r.error;
        if (r.ok) { moi.emby_url = document.getElementById('cpUrl').value.trim(); document.getElementById('cpCle').value = ''; etat(); majBouton(); }
    };
    const bouton = document.querySelector('button[onclick="sendToLibrary()"]');
    const majBouton = () => { if (bouton) { bouton.textContent = '📤 Envoyer dans mon Emby'; bouton.style.display = moi.emby_url ? '' : 'none'; } };
    majBouton();
    window.sendToLibrary = async function () {
        const data = await saveToServer();
        if (!data || !selectedItem) return;
        const r = await (await fetch('/api/copain/envoyer', {method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({filename: data.filename, media_type: selectedItem.media_type, tmdb_id: selectedItem.id, title: selectedItem.title})})).json();
        showMessage(r.message || r.error, r.ok ? 'success' : 'error');
    };
    // Poster déjà fait par l’admin pour ce titre : à télécharger ou à envoyer tel quel
    const vu = {};
    setInterval(async () => {
        const it = selectedItem;
        if (!it || !it.id || vu[it.media_type + it.id] !== undefined) return;
        vu[it.media_type + it.id] = null;
        const r = await (await fetch('/api/copain/existant', {method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({media_type: it.media_type, tmdb_id: it.id, title: it.title})})).json();
        document.querySelectorAll('.copain-existant').forEach(e => e.remove());
        if (!r.existe || selectedItem !== it) return;
        const url = '/api/copain/existant/' + (it.media_type === 'tv' ? 'tv' : 'movie') + '/' + it.id;
        const div = document.createElement('div');
        div.className = 'copain-existant';
        div.innerHTML = '<img src="' + url + '" alt=""><div><b>Poster déjà fait par l’admin</b><div class="act"><a class="button" href="' + url + '?dl=1">⬇️ Télécharger</a>' +
            (moi.emby_url ? '<button type="button" id="cpEnvoyerExistant">📤 Envoyer dans mon Emby</button>' : '') + '</div></div>';
        (document.getElementById('posters') || cible).insertAdjacentElement('beforebegin', div);
        const b = document.getElementById('cpEnvoyerExistant');
        if (b) b.onclick = async () => {
            const x = await (await fetch('/api/copain/envoyer', {method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({existant: true, media_type: it.media_type, tmdb_id: it.id, title: it.title})})).json();
            showMessage(x.message || x.error, x.ok ? 'success' : 'error');
        };
    }, 1500);
})();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    voisins.preparer_cle()
    threading.Thread(target=_menage_copains, daemon=True).start()
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
