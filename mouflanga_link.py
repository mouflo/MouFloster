"""
Envoi d'un poster comme couverture d'une série de MouFlanga (bibliothèque de mangas).

MouFlanga affiche en priorité l'image « cover.jpg » posée dans le dossier de la série :
on y copie le poster, directement dans le dossier des mangas (même serveur, pas d'Emby).
Le dossier des mangas est lu dans les réglages de MouFlanga (/opt/mouflanga/data/secrets.env,
clé MANGA_DIR), ou dans la variable MOUFLANGA_MANGA_DIR si elle est définie.
L'ancienne couverture est sauvegardée dans <OUTPUT_DIR>/_anciens-posters/MouFlanga/<série>/.
"""
import difflib
import os
import re
import shutil
import unicodedata
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


def appliquer(poster: str, nom: str, sauvegardes: str) -> dict:
    """Copie le poster en cover.jpg dans la série ; l'ancienne couverture est sauvegardée."""
    dossier = dossier_serie(nom)
    if dossier is None:
        raise ValueError("Série introuvable dans MouFlanga")
    if not os.path.isfile(poster):
        raise ValueError("Poster introuvable : sauvegarde-le d'abord")
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
