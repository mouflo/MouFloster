"""
Envoi d'un poster comme couverture d'une série de MouFlanga (bibliothèque de mangas), sans Emby.

Deux façons de joindre MouFlanga :
- **par le réseau** (recommandé, marche aussi si MouFlanga est sur une autre machine) : adresse de
  MouFlanga + clé API générée dans MouFlanga → ⚙️ Réglages, enregistrées ici dans ⚙️ Réglages → MouFlanga
  (MOUFLANGA_URL, MOUFLANGA_CLE dans data/secrets.env) ;
- **en local** (même serveur, sans réglage) : le poster est copié en « cover.jpg » dans le dossier de la
  série ; le dossier des mangas est lu dans /opt/mouflanga/data/secrets.env (MANGA_DIR) ou MOUFLANGA_MANGA_DIR.
Dans les deux cas, l'ancienne couverture est sauvegardée dans <OUTPUT_DIR>/_anciens-posters/MouFlanga/<série>/.
"""
import difflib
import os
import re
import shutil
import unicodedata

import requests
from datetime import datetime
from pathlib import Path

REGLAGES_MOUFLANGA = Path(os.getenv("MOUFLANGA_DIR", "/opt/mouflanga")) / "data" / "secrets.env"
DEFAUT = "/mnt/mouflosyno/Manga"            # valeur par défaut de MouFlanga
COUVERTURE = "cover.jpg"
_ARCHIVES = (".cbz", ".cbr", ".zip", ".rar", ".cb7", ".7z", ".pdf")


def manga_dir() -> Path:
    """Dossier des mangas de MouFlanga."""
    perso = os.getenv("MOUFLANGA_MANGA_DIR", "").strip()
    if perso:
        return Path(perso)
    try:
        for ligne in REGLAGES_MOUFLANGA.read_text(encoding="utf-8", errors="ignore").splitlines():
            cle, _, valeur = ligne.strip().partition("=")
            if cle.replace("export ", "").strip() == "MANGA_DIR" and valeur.strip():
                return Path(valeur.strip().strip("\"'"))
    except OSError:
        pass
    return Path(DEFAUT)


def installe() -> bool:
    return REGLAGES_MOUFLANGA.parent.parent.is_dir()


# ---------------- par le réseau ----------------

class ErreurMouflanga(Exception):
    """Message lisible à afficher tel quel."""


def distant() -> tuple[str, str] | None:
    """(adresse, clé) si la connexion réseau est réglée."""
    url = os.getenv("MOUFLANGA_URL", "").strip().rstrip("/")
    cle = os.getenv("MOUFLANGA_CLE", "").strip()
    return (url, cle) if url and cle else None


def mode() -> str:
    return "reseau" if distant() else ("local" if installe() else "")


def _appel(methode: str, chemin: str, url: str | None = None, cle: str | None = None, **kw):
    if url is None:
        url, cle = distant()
    try:
        r = requests.request(methode, url.rstrip("/") + chemin, headers={"X-Cle-API": cle}, timeout=20, **kw)
    except requests.exceptions.ConnectionError:
        raise ErreurMouflanga(f"MouFlanga injoignable ({url}) : vérifie l'adresse.")
    except requests.exceptions.Timeout:
        raise ErreurMouflanga("MouFlanga ne répond pas (délai dépassé).")
    except requests.exceptions.RequestException as e:
        raise ErreurMouflanga(f"Erreur de connexion à MouFlanga : {e.__class__.__name__}")
    if r.status_code == 404 and chemin.startswith("/api/externe/couverture") and methode == "GET":
        return r
    if r.status_code >= 400:
        try:
            msg = r.json().get("error")
        except ValueError:
            msg = None
        if r.status_code == 404 and not msg:
            msg = "Cette adresse ne répond pas comme MouFlanga (version trop ancienne ?)."
        raise ErreurMouflanga(msg or f"MouFlanga a répondu {r.status_code}")
    return r


def tester(url: str, cle: str) -> str:
    """Message de réussite, ou ErreurMouflanga."""
    r = _appel("GET", "/api/externe/series", url, cle)
    try:
        n = len(r.json().get("series", []))
    except ValueError:
        raise ErreurMouflanga("Cette adresse ne répond pas comme MouFlanga.")
    return f"Connexion réussie : {n} série(s) dans MouFlanga."


def _a_des_chapitres(dossier: Path) -> bool:
    try:
        for _, _, fichiers in os.walk(dossier):
            if any(f.lower().endswith(_ARCHIVES) and not f.startswith(".") for f in fichiers):
                return True
    except OSError:
        pass
    return False


def series() -> list[dict]:
    """Séries de la bibliothèque MouFlanga (un sous-dossier par série)."""
    if distant():
        return _appel("GET", "/api/externe/series").json().get("series", [])
    racine = manga_dir()
    if not racine.is_dir():
        return []
    out = []
    try:
        dossiers = sorted(racine.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return []
    for d in dossiers:
        if d.name.startswith((".", "@", "#")) or not d.is_dir() or not _a_des_chapitres(d):
            continue
        out.append({"name": d.name, "cover": (d / COUVERTURE).is_file()})
    return out


def _simple(texte: str) -> str:
    texte = unicodedata.normalize("NFKD", texte or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", texte).strip()


def meilleure(noms: list[str], titres: list[str]) -> str | None:
    """Série dont le nom ressemble le plus au titre du poster (ou None si rien de proche)."""
    meilleur, score = None, 0.0
    for nom in noms:
        n = _simple(nom)
        for t in titres:
            t = _simple(t)
            if not n or not t:
                continue
            s = 1.0 if n == t else difflib.SequenceMatcher(None, n, t).ratio()
            if s > score:
                meilleur, score = nom, s
    return meilleur if score >= 0.6 else None


def dossier_serie(nom: str) -> Path | None:
    """Dossier de la série, uniquement s'il fait partie de la bibliothèque (pas de « .. » ni de chemin)."""
    if not nom or "/" in nom or nom.startswith("."):
        return None
    if not any(s["name"] == nom for s in series()):
        return None
    return manga_dir() / nom


def couverture(nom: str) -> bytes | None:
    """Couverture choisie actuelle d'une série (None : couverture automatique)."""
    if distant():
        r = _appel("GET", "/api/externe/couverture", params={"id": nom})
        return r.content if r.status_code == 200 else None
    dossier = dossier_serie(nom)
    c = dossier / COUVERTURE if dossier else None
    return c.read_bytes() if c and c.is_file() else None


def _sauvegarder(ancienne: bytes, nom: str, sauvegardes: str) -> str:
    rangement = Path(sauvegardes) / "MouFlanga" / re.sub(r'[\\/:*?"<>|]', " ", nom).strip(" .")
    rangement.mkdir(parents=True, exist_ok=True)
    chemin = rangement / f"cover.{datetime.now().strftime('%Y%m%d-%H%M%S')}.jpg"
    chemin.write_bytes(ancienne)
    return str(chemin)


def appliquer(poster: str, nom: str, sauvegardes: str) -> dict:
    """Le poster devient la couverture de la série ; l'ancienne couverture est sauvegardée."""
    if not os.path.isfile(poster):
        raise ValueError("Poster introuvable : sauvegarde-le d'abord")
    if distant():
        ancienne = couverture(nom)
        sauvegarde = _sauvegarder(ancienne, nom, sauvegardes) if ancienne else None
        with open(poster, "rb") as f:
            _appel("POST", "/api/externe/couverture", data={"id": nom},
                   files={"image": (os.path.basename(poster), f, "image/jpeg")})
        return {"written": f"MouFlanga · {nom}", "backup": sauvegarde}
    dossier = dossier_serie(nom)
    if dossier is None:
        raise ValueError("Série introuvable dans MouFlanga")
    cible = dossier / COUVERTURE
    ancienne = None
    if cible.is_file():
        rangement = Path(sauvegardes) / "MouFlanga" / nom
        rangement.mkdir(parents=True, exist_ok=True)
        ancienne = rangement / f"cover.{datetime.now().strftime('%Y%m%d-%H%M%S')}.jpg"
        shutil.copy2(cible, ancienne)
    tmp = dossier / (COUVERTURE + ".tmp")
    shutil.copyfile(poster, tmp)
    os.replace(tmp, cible)
    return {"written": str(cible), "backup": str(ancienne) if ancienne else None}
