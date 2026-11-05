"""
Cohérence des posters d'une série.

Quand on choisit un poster pour une saison, MouFloster :
  1. retrouve les posters DÉJÀ présents dans le dossier de la série (peu importe qu'ils aient été faits avec MouFloster ou non) ;
  2. compare chaque poster proposé par TMDB à ceux-là (couleurs, ambiance, composition) ;
  3. dit si le poster a du texte (titre, logo) et si ce texte a l'air facile à effacer.

Tout est calculé ici, sur le serveur, sans IA : des statistiques d'image (histogramme de couleurs, répartition de la lumière, contours).
Ce sont des ESTIMATIONS : l'interface les présente comme telles, et rien n'est jamais modifié sans que l'utilisateur choisisse.
"""

import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO

import cv2
import numpy as np
from PIL import Image

import library

_POSTER_RE = re.compile(r"^(poster|season(\d{2,3})-poster|season-specials-poster)\.(jpg|jpeg|png|webp)$", re.I)
_CHEMIN_TMDB = re.compile(r"^/[A-Za-z0-9_-]{5,60}\.(jpg|jpeg|png)$")
TAILLE = (48, 72)                    # image réduite sur laquelle on compare (largeur, hauteur)
GRILLE = (6, 9)                      # répartition de la lumière : 6 colonnes, 9 lignes
MAX_POSTERS = 60
MAX_REFERENCES = 8

# Bonus / pénalité de classement selon le texte du poster (le style reste le critère principal)
BONUS_TEXTE = {"sans": 0.08, "facile": 0.03, "moyen": -0.03, "difficile": -0.10}

_NOMS_TEINTES = {0: "rouges", 1: "orangés", 2: "verts", 3: "verts", 4: "verts", 5: "verts",
                 6: "bleus", 7: "bleus", 8: "bleus", 9: "violets", 10: "roses", 11: "rouges"}

_cache_empreintes = {}               # (clé) -> empreinte
_verrou = threading.Lock()


# ---------------------------------------------------------------------------
# Posters déjà dans la médiathèque
# ---------------------------------------------------------------------------

def _libelle(nom):
    m = _POSTER_RE.match(nom)
    if not m:
        return None
    if m.group(1).lower() == "poster":
        return "Série", 0
    if m.group(1).lower() == "season-specials-poster":
        return "Spéciaux", 1000
    n = int(m.group(2))
    return f"Saison {n}", n


def posters_existants(rel, exclure=None):
    """Posters du dossier de la série : [{"name", "label", "ordre"}], série d'abord puis saisons dans l'ordre.
    « exclure » : nom du fichier qu'on est en train de refaire (il ne sert pas de modèle)."""
    dossier = library.resolve_folder(rel)
    if dossier is None:
        return []
    sorties = {}
    try:
        noms = os.listdir(dossier)
    except OSError:
        return []
    for nom in noms:
        info = _libelle(nom)
        if not info or nom.lower() == str(exclure or "").lower():
            continue
        # un même poster en .jpg et en .png : on garde le .jpg
        cle = nom.rsplit(".", 1)[0].lower()
        if cle in sorties and not nom.lower().endswith(".jpg"):
            continue
        try:
            if not (dossier / nom).is_file():
                continue
        except OSError:
            continue
        sorties[cle] = {"name": nom, "label": info[0], "ordre": info[1]}
    return sorted(sorties.values(), key=lambda p: p["ordre"])[:MAX_REFERENCES]


# ---------------------------------------------------------------------------
# Empreinte d'une image
# ---------------------------------------------------------------------------

def _ouvrir_leger(source):
    """Ouvre une image sans la charger en entier (JPEG : décodage réduit) : peu de mémoire même pour un poster de 4000 px."""
    img = Image.open(source)
    try:
        img.draft("RGB", (320, 480))
    except Exception:
        pass
    img = img.convert("RGB")
    img.thumbnail((400, 600))
    return img


def empreinte(img):
    """Résumé d'un poster : histogramme de couleurs (28 cases), répartition de la lumière, ambiance, texte probable."""
    petite = np.asarray(img.convert("RGB").resize(TAILLE, Image.BOX), dtype=np.uint8)
    hsv = cv2.cvtColor(petite, cv2.COLOR_RGB2HSV).reshape(-1, 3).astype(np.int32)
    h, s, v = hsv[:, 0], hsv[:, 1], hsv[:, 2]
    colore = (s >= 40) & (v >= 50)
    hist = np.zeros(28, dtype=np.float64)
    idx = (h[colore] // 15) * 2 + (s[colore] >= 130)                     # 12 teintes × 2 intensités
    hist[:24] = np.bincount(idx, minlength=24)
    hist[24:] = np.bincount(np.minimum(v[~colore] // 64, 3), minlength=4)    # noirs, gris foncés, gris clairs, blancs
    hist /= max(1.0, hist.sum())

    gris = cv2.cvtColor(petite, cv2.COLOR_RGB2GRAY).astype(np.float32)
    grille = cv2.resize(gris, GRILLE, interpolation=cv2.INTER_AREA).reshape(-1)
    grille = grille - grille.mean()
    ecart = float(grille.std())
    grille = grille / ecart if ecart > 1e-3 else np.zeros_like(grille)

    teintes = {}
    for i in range(12):
        nom = _NOMS_TEINTES[i]
        teintes[nom] = teintes.get(nom, 0.0) + float(hist[2 * i] + hist[2 * i + 1])
    dominante = max(teintes, key=teintes.get) if teintes else None
    return {
        "hist": hist,
        "grille": grille,
        "teinte": dominante if dominante and teintes[dominante] >= 0.18 else None,
        "ambiance": "sombre" if float(v.mean()) < 85 else ("claire" if float(v.mean()) > 150 else None),
    }


def ressemblance(a, b):
    """0 à 1 : à quel point deux empreintes se ressemblent. Couleurs d'abord, composition ensuite."""
    couleurs = float(np.minimum(a["hist"], b["hist"]).sum())
    corr = float(np.dot(a["grille"], b["grille"]) / max(1, len(a["grille"])))
    composition = min(1.0, max(0.0, (corr + 1) / 2))
    return 0.65 * couleurs + 0.35 * composition, couleurs, composition


def _raison(a, b, couleurs, composition):
    morceaux = []
    if a["teinte"] and a["teinte"] == b["teinte"]:
        morceaux.append("mêmes tons " + a["teinte"])
    elif couleurs >= 0.6:
        morceaux.append("couleurs proches")
    if a["ambiance"] and a["ambiance"] == b["ambiance"]:
        morceaux.append("ambiance " + a["ambiance"])
    if composition >= 0.72:
        morceaux.append("composition proche")
    if not morceaux:
        morceaux.append("style différent" if couleurs < 0.4 else "un peu proche")
    return ", ".join(morceaux)


def texte_probable(img, langue):
    """Le poster porte-t-il du texte, et sera-t-il facile à effacer ?
    « sans » : TMDB le dit (langue vide). Sinon on regarde la bande (haut ou bas) la plus chargée en contours fins (les lettres) :
    si le fond autour est lisse, l'effacement donnera un résultat propre ; si le fond est chargé (visages, décor), non.
    Renvoie (« sans » | « facile » | « moyen » | « difficile » | « inconnu »)."""
    if not langue:
        return "sans"
    try:
        g = np.asarray(img.convert("L").resize((185, 278), Image.BOX), dtype=np.float32)
        flou = cv2.GaussianBlur(g, (0, 0), 1.2)
        gx = cv2.Sobel(flou, cv2.CV_32F, 1, 0, ksize=3)
        gy = cv2.Sobel(flou, cv2.CV_32F, 0, 1, ksize=3)
        grad = np.sqrt(gx * gx + gy * gy)
        bandes = {"haut": grad[int(278 * 0.02):int(278 * 0.30)], "bas": grad[int(278 * 0.68):int(278 * 0.98)]}
        # la bande qui porte le texte = celle où les contours forts sont les plus nombreux
        nom = max(bandes, key=lambda k: float(np.percentile(bandes[k], 97)))
        bande = bandes[nom]
        fond = float(np.percentile(bande, 55))                   # le fond autour des lettres
        if float(np.percentile(bande, 97)) < 60:                 # aucun contour marqué : pas vraiment de texte dans ces bandes
            return "sans"
        if fond < 9:
            return "facile"
        if fond < 20:
            return "moyen"
        return "difficile"
    except Exception:
        return "inconnu"


# ---------------------------------------------------------------------------
# Lecture des images (médiathèque et TMDB)
# ---------------------------------------------------------------------------

def _empreinte_fichier(dossier, nom):
    chemin = dossier / nom
    try:
        st = chemin.stat()
    except OSError:
        return None
    cle = ("f", str(chemin), st.st_mtime_ns, st.st_size)
    with _verrou:
        if cle in _cache_empreintes:
            return _cache_empreintes[cle]
    try:
        with open(chemin, "rb") as f:
            donnees = f.read(30_000_000)
        emp = empreinte(_ouvrir_leger(BytesIO(donnees)))
    except Exception:
        return None
    with _verrou:
        if len(_cache_empreintes) > 800:
            _cache_empreintes.clear()
        _cache_empreintes[cle] = emp
    return emp


def _empreinte_tmdb(chemin, langue, telecharger):
    cle = ("t", chemin, langue)
    with _verrou:
        if cle in _cache_empreintes:
            return _cache_empreintes[cle]
    octets = telecharger(f"https://image.tmdb.org/t/p/w185{chemin}")
    if not octets:
        return None
    try:
        img = _ouvrir_leger(BytesIO(octets))
        emp = empreinte(img)
        emp["texte"] = texte_probable(img, langue)
    except Exception:
        return None
    with _verrou:
        if len(_cache_empreintes) > 800:
            _cache_empreintes.clear()
        _cache_empreintes[cle] = emp
    return emp


def classer(rel, exclure, posters, telecharger):
    """posters : [{"path", "lang"}] de TMDB. telecharger(url) -> octets ou None.
    Renvoie {"refs": [{"name", "label"}], "scores": {path: {"score": 0-100, "rang": float, "raison", "texte"}}}."""
    refs = posters_existants(rel, exclure)
    dossier = library.resolve_folder(rel)
    valides = [p for p in (posters or [])[:MAX_POSTERS]
               if isinstance(p, dict) and _CHEMIN_TMDB.match(str(p.get("path", "")))]
    refs_emp = []
    refs_ok = []
    if dossier is not None:
        for r in refs:
            e = _empreinte_fichier(dossier, r["name"])
            if e is not None:
                refs_emp.append(e)
                refs_ok.append(r)
    with ThreadPoolExecutor(max_workers=6) as pool:
        resultats = list(pool.map(lambda p: _empreinte_tmdb(p["path"], str(p.get("lang") or ""), telecharger), valides))
    scores = {}
    for p, emp in zip(valides, resultats):
        if emp is None:
            continue
        texte = emp.get("texte", "inconnu")
        fiche = {"texte": texte}
        if refs_emp:
            sims = [ressemblance(emp, r) for r in refs_emp]
            meilleure = max(range(len(sims)), key=lambda i: sims[i][0])
            note = 0.7 * sims[meilleure][0] + 0.3 * (sum(s[0] for s in sims) / len(sims))
            fiche.update(score=int(round(note * 100)),
                         rang=note + BONUS_TEXTE.get(texte, 0.0),
                         raison=_raison(emp, refs_emp[meilleure], sims[meilleure][1], sims[meilleure][2]),
                         proche_de=refs_ok[meilleure]["label"])
        scores[p["path"]] = fiche
    return {"refs": [{"name": r["name"], "label": r["label"]} for r in refs_ok], "scores": scores}
