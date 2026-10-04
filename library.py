"""
Envoi des posters vers la médiathèque (Emby/Jellyfin).

- Reconnaît le dossier d'un film/série à partir de son titre (accents, majuscules,
  "(année)", préfixe "[01]" ignorés), ou de l'identifiant TMDB s'il est dans le nom.
- Calcule le nom du fichier (poster.jpg, season02-poster.jpg, ...).
- Copie en toute sécurité: l'ancien poster est sauvegardé AVANT d'être remplacé.
"""

import difflib
import json
import os
import re
import shutil
import threading
import time
import unicodedata
from datetime import datetime
from pathlib import Path

LIBRARY_ROOT = Path(os.getenv("MEDIATHEQUE_DIR", "/mnt/mouflosyno/Emby-Media"))

# Familles (dossiers à la racine de la médiathèque) selon le type TMDB
FAMILIES = {
    "movie": ["Films HD", "Films 4k"],
    "tv": ["Series", "Manga", "Sentai"],
}
ALL_FAMILIES = [f for fams in FAMILIES.values() for f in fams]

# Dossiers techniques jamais proposés ni modifiés (@eaDir = miniatures Synology, etc.)
def _is_ignored(name: str) -> bool:
    return name.startswith((".", "@", "_", "#")) or name in ("extrafanart", "lost+found")


MAP_FILE = Path(__file__).parent / "data" / "library_map.json"
_map_lock = threading.Lock()

_list_cache = {}  # famille -> (timestamp, [noms])
_LIST_TTL = 120


# ---------------------------------------------------------------------------
# Normalisation des noms
# ---------------------------------------------------------------------------

def fold(text: str) -> str:
    """minuscules, sans accents, sans ponctuation"""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower().replace("&", " and ")
    folded = re.sub(r"[^a-z0-9]+", " ", text).strip()
    return folded or (text.strip() or "")


_ARTICLES = re.compile(r"^(the|le|la|les|l|un|une|des|du|de)\s+")


def loose(folded: str) -> str:
    return _ARTICLES.sub("", folded)


def split_folder_name(name: str):
    """'[01] Titre (2024)' -> (titre_normalisé, 2024, tmdb_id|None)"""
    tmdb = None
    m = re.search(r"[\[{(]\s*tmdb(?:id)?\s*[-=:]\s*(\d+)\s*[\]})]", name, re.I)
    if m:
        tmdb = int(m.group(1))
        name = name.replace(m.group(0), "")
    name = re.sub(r"^\s*\[\d+\]\s*", "", name)  # préfixe Sentai [01]
    year = None
    m = re.search(r"\((\d{4})\)\s*$", name.strip())
    if m:
        year = int(m.group(1))
        name = name.strip()[: m.start()]
    return fold(name), year, tmdb


# ---------------------------------------------------------------------------
# Lecture de la médiathèque
# ---------------------------------------------------------------------------

def list_family(family: str):
    now = time.time()
    cached = _list_cache.get(family)
    if cached and now - cached[0] < _LIST_TTL:
        return cached[1]
    path = LIBRARY_ROOT / family
    names = []
    try:
        for entry in os.scandir(path):
            if entry.is_dir() and not _is_ignored(entry.name):
                names.append(entry.name)
    except OSError:
        names = []
    names.sort(key=str.lower)
    _list_cache[family] = (now, names)
    return names


def clear_cache():
    _list_cache.clear()


def _score(query_titles, query_year, query_tmdb, folder_name):
    norm, year, tmdb = split_folder_name(folder_name)
    if query_tmdb and tmdb and query_tmdb == tmdb:
        return 2.0
    best = 0.0
    for qt in query_titles:
        q = fold(qt)
        if not q or not norm:
            continue
        if q == norm:
            s = 1.0
        elif loose(q) == loose(norm):
            s = 0.97
        else:
            ratio = difflib.SequenceMatcher(None, q, norm).ratio()
            qw, nw = set(q.split()), set(norm.split())
            contained = (qw <= nw or nw <= qw) and min(len(q), len(norm)) / max(len(q), len(norm)) >= 0.6
            s = max(ratio, 0.85 if contained else 0.0)
        best = max(best, s)
    if best and query_year and year:
        if year == query_year:
            best = min(best + 0.05, 1.0)
        elif abs(year - query_year) > 1:
            best -= 0.15
    return best


def find_candidates(media_type, titles, year=None, tmdb_id=None, families=None, limit=8, min_score=0.5):
    families = families or FAMILIES.get(media_type, ALL_FAMILIES)
    found = []
    for family in families:
        for name in list_family(family):
            s = _score(titles, year, tmdb_id, name)
            if s >= min_score:
                found.append({"rel": f"{family}/{name}", "family": family, "name": name, "score": round(s, 3),
                              "media_type": "tv" if family in FAMILIES["tv"] else "movie"})
    found.sort(key=lambda c: (-c["score"], c["name"].lower()))
    return found[:limit]


def search_folders(text, media_type=None, limit=12):
    """Recherche libre (quand la détection se trompe): dans TOUS les dossiers, films et séries
    (ex: un film TMDB qui doit aller dans les spéciaux d'une série)"""
    return find_candidates(media_type, [text], families=ALL_FAMILIES, limit=limit, min_score=0.45)


# ---------------------------------------------------------------------------
# Mémoire des choix (tmdb -> dossier)
# ---------------------------------------------------------------------------

def _map_key(media_type, tmdb_id):
    return f"{media_type}:{tmdb_id}"


def load_map():
    try:
        with open(MAP_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def remember(media_type, tmdb_id, rel):
    if not tmdb_id:
        return
    with _map_lock:
        data = load_map()
        data[_map_key(media_type, tmdb_id)] = rel
        try:
            MAP_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = MAP_FILE.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, MAP_FILE)
        except OSError:
            pass  # mémoire facultative


def remembered(media_type, tmdb_id):
    rel = load_map().get(_map_key(media_type, tmdb_id))
    if rel and resolve_folder(rel) is not None:
        return rel
    return None


# ---------------------------------------------------------------------------
# Noms de fichiers et sécurité des chemins
# ---------------------------------------------------------------------------

def parse_season(text):
    """'2', 'S02', 'Saison 2' -> 2 ; vide ou sans chiffre -> None"""
    if text is None:
        return None
    m = re.search(r"\d+", str(text))
    return int(m.group()) if m else None


def target_name(media_type, season_text=None):
    if media_type != "tv":
        return "poster.jpg"
    n = parse_season(season_text)
    if n is None:
        return "poster.jpg"
    if n == 0:
        return "season-specials-poster.jpg"
    return f"season{n:02d}-poster.jpg"


_ALLOWED_TARGET = re.compile(r"^(poster|season\d{2,3}-poster|season-specials-poster)(-new(-\d{8}-\d{6})?)?\.jpg$")


def valid_target(name):
    return bool(name) and name == os.path.basename(name) and bool(_ALLOWED_TARGET.match(name))


_VIDEO_EXT = (".mkv", ".mp4", ".avi", ".m4v", ".ts", ".mov", ".wmv")
_EP_RE = re.compile(r"(?<![A-Za-z0-9])[Ss](\d{1,3})[ ._-]?[Ee](\d{1,4})(?!\d)")
_EP_TARGET = re.compile(r"^[^/\\]+-thumb(-new(-\d{8}-\d{6})?)?\.jpg$")


def valid_episode_target(name):
    return bool(name) and name == os.path.basename(name) and bool(_EP_TARGET.match(name))


def resolve_folder(rel):
    """Chemin réel du dossier 'Famille/Titre' s'il est valide et dans la médiathèque, sinon None"""
    if not rel or not isinstance(rel, str):
        return None
    parts = [p for p in rel.replace("\\", "/").split("/") if p]
    if len(parts) != 2 or parts[0] not in ALL_FAMILIES or any(_is_ignored(p) for p in parts[1:]):
        return None
    if any(p in (".", "..") for p in parts):
        return None
    path = LIBRARY_ROOT.joinpath(*parts)
    try:
        real_root = LIBRARY_ROOT.resolve()
        real = path.resolve()
        if real_root not in real.parents:
            return None
    except OSError:
        return None
    return real if real.is_dir() else None


def resolve_dir(rel, sub=""):
    """Dossier 'Famille/Titre' (+ sous-dossier éventuel, ex: saison) validé, ou None"""
    folder = resolve_folder(rel)
    if folder is None or not sub:
        return folder
    parts = [p for p in str(sub).replace("\\", "/").split("/") if p]
    if not parts or any(p in (".", "..") or _is_ignored(p) for p in parts):
        return None
    path = folder.joinpath(*parts)
    try:
        real = path.resolve()
        if folder not in real.parents:
            return None
    except OSError:
        return None
    return real if real.is_dir() else None


def find_episode_video(rel, season, episode):
    """Cherche dans le dossier de la série le fichier vidéo S{season}E{episode}. Retourne (sous-dossier, nom) ou None"""
    folder = resolve_folder(rel)
    if folder is None:
        return None
    found = []
    for dirpath, dirnames, filenames in os.walk(folder, onerror=lambda e: None):
        dirnames[:] = [d for d in dirnames if not _is_ignored(d)]
        for fn in filenames:
            if fn.lower().endswith(_VIDEO_EXT) and not _is_ignored(fn):
                m = _EP_RE.search(fn)
                if m and int(m.group(1)) == season and int(m.group(2)) == episode:
                    sub = os.path.relpath(dirpath, folder)
                    found.append(("" if sub == "." else sub.replace(os.sep, "/"), fn))
    found.sort(key=lambda t: (t[0].count("/"), t[1].lower()))
    return found[0] if found else None


def resolve_target(rel, media_type, season_text=None, episode_text=None):
    """
    Où va le poster ? Retourne {"sub", "name", "episode": bool, "video"}.
    - film: poster.jpg ; série: poster.jpg / seasonNN-poster.jpg / season-specials-poster.jpg
    - épisode (série + numéro d'épisode): <nom de la vidéo>-thumb.jpg, à côté de la vidéo
    Lève ValueError avec un message clair si l'épisode est introuvable.
    """
    ep = parse_season(episode_text) if media_type == "tv" else None
    if ep is None:
        return {"sub": "", "name": target_name(media_type, season_text), "episode": False, "video": None}
    season = parse_season(season_text)
    if season is None:
        raise ValueError("Indique aussi le numéro de saison (0 pour les spéciaux)")
    hit = find_episode_video(rel, season, ep)
    if not hit:
        raise ValueError(f"Aucun fichier vidéo S{season:02d}E{ep:02d} trouvé dans ce dossier")
    sub, video = hit
    return {"sub": sub, "name": os.path.splitext(video)[0] + "-thumb.jpg", "episode": True, "video": video}


def inspect_target(rel, name, sub="", episode=False):
    """Infos sur le poster déjà présent (ou None)"""
    folder = resolve_dir(rel, sub)
    if folder is None or not (valid_episode_target(name) if episode else valid_target(name)):
        return None
    info = {"exists": False, "name": name, "others": [], "sub": sub, "episode": episode}
    path = folder / name
    if path.is_file():
        st = path.stat()
        info.update(
            exists=True,
            size=st.st_size,
            mtime=datetime.fromtimestamp(st.st_mtime).strftime("%d/%m/%Y %H:%M"),
            mtime_raw=int(st.st_mtime),
        )
        try:
            from PIL import Image
            with Image.open(path) as im:
                info["dimensions"] = f"{im.width}×{im.height}"
        except Exception:
            pass
    stem = name[:-4]
    for ext in (".png", ".jpeg", ".webp"):
        if (folder / f"{stem}{ext}").is_file():
            info["others"].append(f"{stem}{ext}")
    return info


# ---------------------------------------------------------------------------
# Copie sécurisée
# ---------------------------------------------------------------------------

def apply_poster(source_file, rel, name, action, backup_root, sub="", episode=False):
    """
    action: 'replace' (ancien sauvegardé puis remplacé) | 'both' (nouveau à côté) | 'copy' (pas d'existant)
    Retourne un dict {written, backup}. Lève ValueError en cas de problème.
    """
    folder = resolve_dir(rel, sub)
    if folder is None:
        raise ValueError("Dossier de destination invalide")
    if not (valid_episode_target(name) if episode else valid_target(name)):
        raise ValueError("Nom de fichier non autorisé")
    shown = f"{rel}/{sub}" if sub else rel
    source = Path(source_file)
    if not source.is_file():
        raise ValueError("Poster sauvegardé introuvable")

    target = folder / name
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = None

    if action == "both":
        stem = name[:-4]
        target = folder / f"{stem}-new.jpg"
        if target.exists():
            target = folder / f"{stem}-new-{stamp}.jpg"
    elif action == "replace":
        if target.exists():
            backup_dir = Path(backup_root) / shown
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup_path = backup_dir / f"{name[:-4]}.{stamp}.jpg"
            shutil.copy2(target, backup_path)  # si ça échoue, on s'arrête avant de remplacer
            if not backup_path.is_file() or backup_path.stat().st_size != target.stat().st_size:
                raise ValueError("La sauvegarde de l'ancien poster a échoué, rien n'a été remplacé")
    elif action == "copy":
        if target.exists():
            raise ValueError("Un poster existe déjà, choisis Remplacer ou Garder les deux")
    else:
        raise ValueError("Action inconnue")

    tmp = folder / f".{target.name}.tmp"
    try:
        shutil.copyfile(source, tmp)
        try:
            os.chmod(tmp, 0o644)
        except OSError:
            pass  # certains partages réseau refusent chmod: sans importance
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass

    return {
        "written": f"{shown}/{target.name}",
        "backup": str(backup_path) if backup_path else None,
    }
