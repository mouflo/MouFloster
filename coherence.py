"""
Cohérence des posters d'une série.

Quand on choisit un poster pour une saison, MouFloster :
  1. retrouve les posters DÉJÀ présents dans le dossier de la série (peu importe qu'ils aient été faits avec MouFloster ou non) ;
  2. compare chaque poster proposé par TMDB à ceux-là (couleurs, ambiance, composition) ;
  3. dit si le poster a du texte (titre, logo) et si ce texte a l'air facile à effacer ;
  4. propose un cadrage (zoom + position) qui met le personnage (ou le groupe) au même endroit et à la même taille que sur les posters modèles.

Rien n'est écrit sur le disque : les empreintes restent en mémoire vive (environ 1 Ko par poster, vidées au redémarrage) et les images
TMDB sont téléchargées en petit format le temps du calcul, puis oubliées.

Tout est calculé ici, sur le serveur, sans IA : des statistiques d'image (histogramme de couleurs, répartition de la lumière, contours).
Ce sont des ESTIMATIONS : l'interface les présente comme telles, et rien n'est jamais modifié sans que l'utilisateur choisisse.
"""

import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path

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
        sorties[cle] = {"rel": rel, "name": nom, "label": info[0], "ordre": info[1]}
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


def classer(refs, posters, telecharger):
    """refs : modèles [{"rel", "name", "label"}] (dossiers de la médiathèque). posters : [{"path", "lang"}] de TMDB.
    telecharger(url) -> octets ou None.
    Renvoie {"refs": [{"rel", "name", "label"}], "scores": {path: {"score": 0-100, "rang": float, "raison", "texte", "proche_de"}}}."""
    valides = [p for p in (posters or [])[:MAX_POSTERS]
               if isinstance(p, dict) and _CHEMIN_TMDB.match(str(p.get("path", "")))]
    refs_emp = []
    refs_ok = []
    for r in refs or []:
        dossier = library.resolve_folder(r.get("rel"))
        e = _empreinte_fichier(dossier, r["name"]) if dossier is not None else None
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
            note = 0.7 * sims[meilleure][0] + 0.3 * (sum(x[0] for x in sims) / len(sims))
            fiche.update(score=int(round(note * 100)),
                         rang=note + BONUS_TEXTE.get(texte, 0.0),
                         raison=_raison(emp, refs_emp[meilleure], sims[meilleure][1], sims[meilleure][2]),
                         proche_de=refs_ok[meilleure]["label"])
        scores[p["path"]] = fiche
    return {"refs": [{"rel": r["rel"], "name": r["name"], "label": r["label"]} for r in refs_ok], "scores": scores}


# ---------------------------------------------------------------------------
# Alignement du personnage (ou du groupe)
# ---------------------------------------------------------------------------

IMG_W, IMG_H = 1000, 1500            # taille du poster fini (comme app.IMG_WIDTH / IMG_HEIGHT)
BANDE_BAS = 0.70                     # au-delà : dégradé noir et titre, le personnage n'y est plus mesurable
ROGNE_MODELE = 0.06                  # cadre blanc (jusqu'à 4,5 %) retiré avant de chercher le personnage dans un poster fini
_cache_sujets = {}


def sujet(img, rogne=0.0):
    """Où est le personnage (ou le groupe) ? Découpe automatique du premier plan (GrabCut) sur une petite image.
    Renvoie {"x0","y0","x1","y1"} en fractions de l'image entière + "masque" (tableau 0/1 réduit, sur l'image rognée) ; None si rien de net."""
    rgb = img.convert("RGB")
    W, H = rgb.size
    gx0, gy0 = int(W * rogne), int(H * rogne)
    rgb = rgb.crop((gx0, gy0, W - gx0, H - gy0))
    larg = 240
    haut = max(60, int(round(larg * rgb.height / rgb.width)))
    petite = np.ascontiguousarray(np.asarray(rgb.resize((larg, haut), Image.BOX), dtype=np.uint8)[:, :, ::-1])
    m = np.zeros((haut, larg), np.uint8)
    rect = (int(larg * 0.03), int(haut * 0.02), int(larg * 0.94), int(haut * 0.96))
    try:
        cv2.grabCut(petite, m, rect, np.zeros((1, 65), np.float64), np.zeros((1, 65), np.float64), 4, cv2.GC_INIT_WITH_RECT)
    except cv2.error:
        return None
    masque = np.where((m == 1) | (m == 3), 1, 0).astype(np.uint8)
    masque = cv2.morphologyEx(masque, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    n, lab, st, _ = cv2.connectedComponentsWithStats(masque)
    if n < 2:
        return None
    plus = int(st[1:, 4].max())
    garde = [i for i in range(1, n) if st[i, 4] >= 0.08 * plus]      # le groupe : toutes les grosses taches
    masque = np.isin(lab, garde).astype(np.uint8)
    ys, xs = np.where(masque > 0)
    if not len(xs):
        return None
    # un vrai personnage (photo) a beaucoup de nuances ; un logo ou un titre seul en a deux ou trois : on le sait pour ne pas l'« aligner »
    pixels = petite[masque > 0][:, ::-1] // 64                          # RGB en 4 niveaux par couche = 64 cases de couleur
    cases = np.bincount(pixels[:, 0] * 16 + pixels[:, 1] * 4 + pixels[:, 2], minlength=64)
    riche = int((cases >= 0.01 * max(1, len(pixels))).sum()) >= 6
    fw, fh = 1 - 2 * rogne, 1 - 2 * rogne
    return {"riche": riche, "x0": rogne + fw * xs.min() / larg, "x1": rogne + fw * (xs.max() + 1) / larg,
            "y0": rogne + fh * ys.min() / haut, "y1": rogne + fh * (ys.max() + 1) / haut,
            "masque": masque, "rogne": rogne}


MODELE_VISAGE = Path(__file__).resolve().parent / "data" / "modeles" / "yunet.onnx"
URL_MODELE_VISAGE = "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx"
_detecteur = {"obj": None, "essaye": False}


def _modele_visage():
    """Petit modèle public de détection de visage (YuNet, 230 Ko), téléchargé une seule fois dans data/modeles. None s'il manque."""
    if MODELE_VISAGE.exists() and MODELE_VISAGE.stat().st_size > 100_000:
        return MODELE_VISAGE
    if _detecteur["essaye"]:
        return None
    _detecteur["essaye"] = True
    try:
        import requests
        r = requests.get(URL_MODELE_VISAGE, timeout=30)
        r.raise_for_status()
        if len(r.content) < 100_000:
            return None
        MODELE_VISAGE.parent.mkdir(parents=True, exist_ok=True)
        MODELE_VISAGE.write_bytes(r.content)
        return MODELE_VISAGE
    except Exception:
        return None


def visage(img):
    """Le plus grand visage de l'image : {"cx","cy","h","w"} en fractions de l'image entière, ou None (pas de visage, ou modèle absent).
    Sert à aligner deux portraits comme sous Photoshop : même taille de visage, même place."""
    chemin = _modele_visage()
    if chemin is None:
        return None
    rgb = img.convert("RGB")
    W, H = rgb.size
    k = min(1.0, 800.0 / max(W, H))
    petite = np.ascontiguousarray(np.asarray(rgb.resize((max(32, int(W * k)), max(32, int(H * k))), Image.BOX), dtype=np.uint8)[:, :, ::-1])
    h, w = petite.shape[:2]
    try:
        with _verrou:
            det = cv2.FaceDetectorYN.create(str(chemin), "", (w, h), 0.6, 0.3, 50)
            _, faces = det.detect(petite)
    except Exception:
        return None
    if faces is None or not len(faces):
        return None
    f = max(faces, key=lambda r: float(r[2]) * float(r[3]))
    if float(f[2]) * float(f[3]) < 0.004 * w * h:                        # un visage minuscule dans un groupe ou un décor : pas un portrait
        return None
    x, y, fw, fh = (float(v) for v in f[:4])
    return {"cx": (x + fw / 2) / w, "cy": (y + fh / 2) / h, "w": fw / w, "h": fh / h}


def _largeur_bande(su, y_haut, y_bas):
    """Largeur (fraction de l'image) du personnage entre deux hauteurs (fractions de l'image)."""
    mk = su["masque"]
    h, w = mk.shape
    fh = 1 - 2 * su["rogne"]
    a = max(0, int((y_haut - su["rogne"]) / fh * h))
    b = min(h, max(a + 1, int(np.ceil((y_bas - su["rogne"]) / fh * h))))
    cols = np.where(mk[a:b].any(axis=0))[0]
    if not len(cols):
        return None
    return (cols.max() + 1 - cols.min()) / w * fh


def mesurer_modele(img):
    """Cadrage du personnage dans un poster FINI de la médiathèque : haut, milieu, largeur du buste, bas (si visible)."""
    su = sujet(img, ROGNE_MODELE)
    if su is None:
        return None
    haut = su["y0"]
    bas = min(su["y1"], BANDE_BAS)
    larg = _largeur_bande(su, haut, max(bas, haut + 0.05))
    if larg is None:
        return None
    return {"haut": haut, "centre": (su["x0"] + su["x1"]) / 2, "largeur": larg, "bas": su["y1"],
            "touche_bas": su["y1"] >= 0.80, "visage": visage(img)}


def _mediane(valeurs):
    v = sorted(valeurs)
    return v[len(v) // 2] if len(v) % 2 else (v[len(v) // 2 - 1] + v[len(v) // 2]) / 2


def resume_modeles(mesures):
    """Cadrage visé d'après les posters modèles (médiane) + quelle mesure est la plus régulière entre eux (le mode conseillé)."""
    ms = [m for m in mesures if m]
    if not ms:
        return None
    cible = {"haut": _mediane([m["haut"] for m in ms]), "centre": _mediane([m["centre"] for m in ms]),
             "largeur": _mediane([m["largeur"] for m in ms]), "bas": _mediane([m["bas"] for m in ms]),
             "touche_bas": sum(1 for m in ms if m["touche_bas"]) * 2 >= len(ms), "n": len(ms),
             # fourchette de TES posters : un cadrage compris dedans « colle » même si ce n'est pas la médiane
             "haut_min": min(m["haut"] for m in ms), "haut_max": max(m["haut"] for m in ms),
             "centre_min": min(m["centre"] for m in ms), "centre_max": max(m["centre"] for m in ms)}

    vs = [m["visage"] for m in ms if m.get("visage")]
    if vs and len(vs) * 2 >= len(ms):                         # la plupart de tes posters montrent un visage : on aligne sur lui
        cible["visage"] = {"cx": _mediane([v["cx"] for v in vs]), "cy": _mediane([v["cy"] for v in vs]), "h": _mediane([v["h"] for v in vs]),
                           "n": len(vs)}

    def ecart_rel(cle):
        med = _mediane([m[cle] for m in ms])
        return (_mediane([abs(m[cle] - med) for m in ms]) / med) if med > 1e-6 else 9.0
    cible["ecarts"] = {"largeur": ecart_rel("largeur"), "haut": _mediane([abs(m["haut"] - cible["haut"]) for m in ms])}
    if cible["touche_bas"]:
        cible["ecarts"]["hauteur"] = 0.0 if len(ms) == 1 else sum(1 for m in ms if not m["touche_bas"]) / len(ms)
    else:
        cible["ecarts"]["hauteur"] = ecart_rel("bas")
    return cible


def conseiller(cible):
    """« largeur », « hauteur » ou « tete » : la mesure la plus régulière entre les modèles (largeur par défaut)."""
    e = cible["ecarts"]
    choix, meilleur = "largeur", e["largeur"]
    if e["hauteur"] + 0.02 < meilleur and cible["bas"] - cible["haut"] > 0.3:
        choix, meilleur = "hauteur", e["hauteur"]
    return choix


def _axe(n, taille, cible, point_px):
    """Un axe : image agrandie à n pixels dans un cadre de « taille » ; le point (à point_px dans l'image agrandie) doit tomber à « cible ».
    Renvoie (position 0 à 1 du curseur de recadrage, écart restant en px, bords flous ?)."""
    if n > taille + 0.5:                                                      # de la marge : le curseur déplace l'image
        c = min(1.0, max(0.0, (point_px - cible) / (n - taille)))
        return c, (-(n - taille) * c + point_px) - cible, False
    if n >= taille - 0.5:                                                     # image juste de la taille du cadre : rien à déplacer
        return 0.5, point_px - cible, False
    return 0.5, ((taille - n) / 2 + point_px) - cible, True                   # plus petite que le cadre : centrée, bords flous


def _cadrage(sw, sh, b, zoom, cible_x, cible_y, su_cx, su_cy):
    """Curseurs de recadrage qui mettent le point (su_cx, su_cy) du poster source (fractions) à (cible_x, cible_y) du poster fini
    (fractions), pour ce zoom. Renvoie (crop_x, crop_y, ecart_x_px, ecart_y_px, bords_flous)."""
    iw, ih = IMG_W - 2 * b, IMG_H - 2 * b
    scale = max(iw / sw, ih / sh) * zoom
    cx, ex, fx = _axe(sw * scale, iw, cible_x * IMG_W - b, su_cx * sw * scale)
    cy, ey, fy = _axe(sh * scale, ih, cible_y * IMG_H - b, su_cy * sh * scale)
    return cx, cy, ex, ey, fx or fy


def _hors_fourchette(valeur, mini, maxi, marge=20):
    """0 si la valeur est dans [mini - marge, maxi + marge] ; sinon l'écart (négatif = en dessous, positif = au-dessus)."""
    if valeur < mini - marge:
        return valeur - (mini - marge)
    if valeur > maxi + marge:
        return valeur - (maxi + marge)
    return 0.0


def proposer(img, cible, border=22, modes=("largeur", "hauteur", "tete"), conseille=None):
    """Cadrages proposés pour le poster candidat « img » : [{mode, label, zoom, crop_x, crop_y, ecart_haut, ecart_centre, bords_flous, sur}].
    Rien n'est appliqué ici : l'interface montre l'aperçu et l'utilisatrice choisit."""
    su = sujet(img)
    if su is None or cible is None:
        return []
    sw, sh = img.size
    vis = visage(img) if cible.get("visage") else None
    if vis:
        modes = ("visage",) + tuple(modes)
    aire = (su["x1"] - su["x0"]) * (su["y1"] - su["y0"])
    sur = 0.06 <= aire <= 0.85 and su.get("riche", True)
    base = max((IMG_W - 2 * border) / sw, (IMG_H - 2 * border) / sh)
    cx_su = (su["x0"] + su["x1"]) / 2
    sorties = []
    for mode in modes:
        if mode == "visage":                                     # même taille de visage, même place : comme sous Photoshop
            zoom = min(4.0, max(0.5, (cible["visage"]["h"] * IMG_H) / (vis["h"] * sh) / base))
            cx, cy, ex, ey, flous = _cadrage(sw, sh, border, zoom, cible["visage"]["cx"], cible["visage"]["cy"], vis["cx"], vis["cy"])
            sorties.append({"mode": mode, "zoom": round(float(zoom), 3), "crop_x": round(float(cx), 3), "crop_y": round(float(cy), 3),
                            "ecart_haut": int(round(float(ey))), "ecart_centre": int(round(float(ex))), "bords_flous": bool(flous),
                            "sur": True, "conseille": False})
            continue
        if mode == "largeur":
            scale = base
            for _ in range(4):                                   # la bande visible dépend de l'échelle : on affine
                lignes = (BANDE_BAS - cible["haut"]) * IMG_H / (scale * sh)
                lw = _largeur_bande(su, su["y0"], min(su["y1"], su["y0"] + max(0.05, lignes / 1.0)))
                if not lw:
                    break
                scale = (cible["largeur"] * IMG_W) / (lw * sw)
            zoom = scale / base
        elif mode == "hauteur":
            haut_su = (su["y1"] - su["y0"]) * sh
            bas_cible = 1.0 if cible["touche_bas"] else cible["bas"]
            zoom = ((bas_cible - cible["haut"]) * IMG_H / haut_su) / base
        else:                                                    # « tete » : même haut et même milieu, avec le plus petit zoom qui permet de les placer
            iw, ih = IMG_W - 2 * border, IMG_H - 2 * border
            tx, ty = cible["centre"] * IMG_W - border, cible["haut"] * IMG_H - border

            def besoin(point, taille_src, vise, cadre):
                """Échelle minimale pour que le point (fraction de l'image) puisse tomber à « vise » sans laisser de vide."""
                m = 0.0
                if point > 1e-6:
                    m = max(m, vise / (point * taille_src))
                if point < 1 - 1e-6:
                    m = max(m, (cadre - vise) / ((1 - point) * taille_src))
                return m
            echelle = max(base, besoin(cx_su, sw, tx, iw), besoin(su["y0"], sh, ty, ih))
            zoom = echelle / base
        zoom = min(3.0, max(0.5, zoom))
        cx, cy, ex, ey, flous = _cadrage(sw, sh, border, zoom, cible["centre"], cible["haut"], cx_su, su["y0"])
        # écart « utile » : 0 tant que la tête / le milieu tombent dans la fourchette de tes posters (marge 20 px), sinon la distance au bord
        ey = _hors_fourchette(cible["haut"] * IMG_H + ey, cible.get("haut_min", cible["haut"]) * IMG_H, cible.get("haut_max", cible["haut"]) * IMG_H)
        ex = _hors_fourchette(cible["centre"] * IMG_W + ex, cible.get("centre_min", cible["centre"]) * IMG_W, cible.get("centre_max", cible["centre"]) * IMG_W)
        sorties.append({"mode": mode, "zoom": round(float(zoom), 3), "crop_x": round(float(cx), 3), "crop_y": round(float(cy), 3),
                        "ecart_haut": int(round(float(ey))), "ecart_centre": int(round(float(ex))), "bords_flous": bool(flous), "sur": bool(sur),
                        "conseille": False})
    # le mode conseillé d'après les modèles, sauf si CE poster ne peut pas le suivre (bords flous, zoom aberrant, tête qui ne rejoint pas sa place)
    ordre = [conseille] + [m for m in ("hauteur", "largeur", "tete") if m != conseille]

    def erreur(pr):
        return abs(pr["ecart_haut"]) + abs(pr["ecart_centre"]) + (400 if pr["bords_flous"] else 0) + (300 if not 0.7 <= pr["zoom"] <= 1.8 else 0)
    for pr in sorties:                                           # « acceptable » : assez bon pour être appliqué sans demander
        pr["acceptable"] = bool(pr["sur"] and not pr["bords_flous"] and abs(pr["ecart_haut"]) <= (25 if pr["mode"] == "visage" else 60)
                                and abs(pr["ecart_centre"]) <= (25 if pr["mode"] == "visage" else 60)
                                and (0.6 <= pr["zoom"] <= 2.5 if pr["mode"] == "visage" else 0.7 <= pr["zoom"] <= 1.8))
    if sorties:
        bons = [pr for pr in sorties if pr["acceptable"]] or sorties          # un cadrage acceptable passe toujours avant un mauvais
        visages = [pr for pr in bons if pr["mode"] == "visage" and pr["acceptable"]]
        meilleur = visages[0] if visages else min(bons, key=lambda pr: (round(erreur(pr) / 40), ordre.index(pr["mode"]) if pr["mode"] in ordre else 9))
        meilleur["conseille"] = True
    return sorties


def modeles_mesures(refs):
    """Cadrage de chaque poster modèle (lu sur le fichier, gardé en mémoire tant que le fichier ne change pas)."""
    sorties = []
    for r in refs or []:
        dossier = library.resolve_folder(r.get("rel"))
        if dossier is None:
            continue
        chemin = dossier / r["name"]
        try:
            st = chemin.stat()
        except OSError:
            continue
        cle = ("m", str(chemin), st.st_mtime_ns, st.st_size)
        with _verrou:
            connu = _cache_sujets.get(cle, False)
        if connu is False:
            try:
                with open(chemin, "rb") as f:
                    connu = mesurer_modele(_ouvrir_leger(BytesIO(f.read(30_000_000))))
            except Exception:
                connu = None
            with _verrou:
                if len(_cache_sujets) > 400:
                    _cache_sujets.clear()
                _cache_sujets[cle] = connu
        if connu:
            sorties.append(connu)
    return sorties
