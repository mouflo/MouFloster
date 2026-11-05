"""
Combler une zone d'un poster (texte, logo) avec des morceaux d'une 2e image.

Cas visé : le poster porte un texte qu'on ne peut pas effacer proprement, et on a une autre image des mêmes personnages (fond d'écran…)
dont le montage est DIFFÉRENT. On ne peut donc pas superposer les deux images d'un bloc : chaque personnage (chaque élément) est replacé
séparément. Le calcul :
  1. des points de repère (SIFT) communs aux deux images, hors de la zone peinte ;
  2. plusieurs « transformations » (zoom + rotation + décalage), une par élément retrouvé (RANSAC répété) ;
  3. pour chaque transformation, où elle est FIABLE : là où, autour de la zone peinte, la 2e image remise en place ressemble au poster ;
  4. dans la zone peinte, chaque point prend les pixels de la transformation la plus fiable autour de lui (bords fondus, couleur recalée) ;
  5. ce que la 2e image ne couvre pas (ou dont aucune transformation n'est sûre) est rendu à l'appelant : l'effaceur (LaMa) le comble.
Rien n'est écrit sur le disque ici ; l'appelant enregistre le résultat comme n'importe quelle étape d'effacement.
"""

import cv2
import numpy as np

TAILLE_TRAVAIL = 1400          # côté le plus long sur lequel on cherche les points de repère
MIN_POINTS = 10                # points concordants pour accepter une transformation
MAX_MODELES = 7
ECART_ACCORD = 42              # écart de couleur (0-255) en dessous duquel on dit « pareil »
FIABILITE_MIN = 0.45           # part des pixels voisins qui concordent pour faire confiance à une transformation
LISSAGE_ACCORD = 12            # lissage (px) de la carte de concordance avant de la prolonger dans la zone à combler
DISTANCE_MAX = 260             # au-delà de cette distance (px) du bord de la zone, aucune transformation n'est crue
MARGE = 160                    # zone de travail autour de la zone peinte


def _petit(arr, cote):
    h, w = arr.shape[:2]
    s = min(1.0, cote / max(h, w))
    if s < 1.0:
        arr = cv2.resize(arr, (max(1, int(round(w * s))), max(1, int(round(h * s)))), interpolation=cv2.INTER_AREA)
    return arr, s


def transformations(poster, masque, source):
    """Transformations source → poster (matrices 3×3, pleine résolution) : [{"M", "points", "echelle", "rotation"}].
    poster, source : tableaux RGB. masque : tableau 0/255 de la taille du poster (zone à combler)."""
    p, sp = _petit(poster, TAILLE_TRAVAIL)
    s, ss = _petit(source, TAILLE_TRAVAIL)
    mk = cv2.resize(masque, (p.shape[1], p.shape[0]), interpolation=cv2.INTER_NEAREST)
    permis = np.where(cv2.dilate(mk, np.ones((15, 15), np.uint8)) > 0, 0, 255).astype(np.uint8)      # pas de points sur le texte
    sift = cv2.SIFT_create(5000)
    kp_s, d_s = sift.detectAndCompute(cv2.cvtColor(s, cv2.COLOR_RGB2GRAY), None)
    kp_p, d_p = sift.detectAndCompute(cv2.cvtColor(p, cv2.COLOR_RGB2GRAY), permis)
    if d_s is None or d_p is None or len(kp_s) < MIN_POINTS or len(kp_p) < MIN_POINTS:
        return []
    paires = [a for a, b in cv2.BFMatcher().knnMatch(d_s, d_p, k=2) if a.distance < 0.75 * b.distance]
    sortie = []
    cv2.setRNGSeed(12345)                                                    # RANSAC tire au hasard : même résultat à chaque essai
    for _ in range(MAX_MODELES):
        if len(paires) < MIN_POINTS:
            break
        src = np.float32([kp_s[m.queryIdx].pt for m in paires])
        dst = np.float32([kp_p[m.trainIdx].pt for m in paires])
        M, inl = cv2.estimateAffinePartial2D(src, dst, method=cv2.RANSAC, ransacReprojThreshold=3.0, maxIters=4000, confidence=0.995)
        if M is None or inl is None or int(inl.sum()) < MIN_POINTS:
            break
        inl = inl.ravel().astype(bool)
        echelle = float(np.hypot(M[0, 0], M[1, 0]))
        if 0.15 < echelle < 8.0:
            petit_vers_grand_p = np.diag([1 / sp, 1 / sp, 1.0])
            grand_vers_petit_s = np.diag([ss, ss, 1.0])
            Mg = petit_vers_grand_p @ np.vstack([M, [0, 0, 1.0]]) @ grand_vers_petit_s
            sortie.append({"M": Mg, "points": int(inl.sum()), "echelle": echelle * ss / sp,
                           "rotation": float(np.degrees(np.arctan2(M[1, 0], M[0, 0])))})
        paires = [m for m, ok in zip(paires, inl) if not ok]                # on enlève ces points : le modèle suivant est un autre élément
    return sortie


def _prolonger(carte, valide):
    """Chaque point prend la valeur du point valide LE PLUS PROCHE : un trou au milieu du personnage hérite de la concordance du personnage
    qui l'entoure, un trou sur le fond hérite de celle du fond (où la 2e image ne ressemble pas au poster)."""
    inv = np.where(valide, 0, 1).astype(np.uint8)
    if not valide.any():
        return np.zeros(carte.shape, np.float32)
    dist, labels = cv2.distanceTransformWithLabels(inv, cv2.DIST_L2, 5, labelType=cv2.DIST_LABEL_PIXEL)
    ys, xs = np.where(valide)                                             # un label par pixel valide, dans l'ordre de balayage
    idx = np.clip(labels - 1, 0, len(ys) - 1)
    sortie = carte[ys[idx], xs[idx]].astype(np.float32)
    sortie[dist > DISTANCE_MAX] = 0.0
    return cv2.GaussianBlur(sortie, (0, 0), 6)


def combler(poster, masque, source):
    """Remplace la zone « masque » du poster par les morceaux fiables de « source ».
    Renvoie {"image": RGB, "reste": masque 0/255 de ce qui n'a pas pu être comblé, "rapport": {...}}.
    Le poster et le masque ne sont jamais modifiés sur place."""
    H, W = poster.shape[:2]
    masque = (masque > 0).astype(np.uint8) * 255
    surface = int((masque > 0).sum())
    if surface == 0:
        return {"image": poster.copy(), "reste": masque, "rapport": {"couvert": 0.0, "reste": 0.0, "morceaux": [], "modeles": 0}}
    ys, xs = np.where(masque > 0)
    x0, x1 = max(0, int(xs.min()) - MARGE), min(W, int(xs.max()) + MARGE + 1)
    y0, y1 = max(0, int(ys.min()) - MARGE), min(H, int(ys.max()) + MARGE + 1)
    w, h = x1 - x0, y1 - y0
    P = poster[y0:y1, x0:x1].astype(np.float32)
    Pf = cv2.GaussianBlur(P, (0, 0), 1.5)
    trou = masque[y0:y1, x0:x1] > 0
    loin = ~(cv2.dilate(trou.astype(np.uint8), np.ones((25, 25), np.uint8)) > 0)      # on juge la ressemblance loin du texte
    vides = {"image": poster.copy(), "reste": masque, "rapport": {"couvert": 0.0, "reste": 100.0, "morceaux": [], "modeles": 0}}
    modeles = transformations(poster, masque, source)
    if not modeles:
        return vides
    decale = np.array([[1, 0, -x0], [0, 1, -y0], [0, 0, 1.0]])
    un = np.full(source.shape[:2], 255, np.uint8)
    fiabilites, images, couvertures = [], [], []
    for mod in modeles:
        Mr = (decale @ mod["M"])[:2]
        Wk = cv2.warpAffine(source, Mr, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0))
        cov = cv2.warpAffine(un, Mr, (w, h), flags=cv2.INTER_LINEAR) > 250
        cov = cv2.erode(cov.astype(np.uint8), np.ones((5, 5), np.uint8)) > 0
        Wf = cv2.GaussianBlur(Wk.astype(np.float32), (0, 0), 1.5)
        ecart = np.abs(Pf - Wf).max(axis=2)
        valide = cov & loin                                                   # là où on peut comparer : source présente, hors de la zone peinte
        accord = ((ecart < ECART_ACCORD) & valide).astype(np.float32)
        lisse = cv2.GaussianBlur(accord, (0, 0), LISSAGE_ACCORD) / np.maximum(cv2.GaussianBlur(valide.astype(np.float32), (0, 0), LISSAGE_ACCORD), 1e-6)
        fiab = _prolonger(lisse, valide)
        fiab[~cov] = 0.0
        # recalage de couleur : décalage médian entre les deux images là où elles concordent
        zone = (accord > 0)
        if zone.sum() > 200:
            decal = np.median((P - Wk.astype(np.float32))[zone], axis=0)
            decal = np.clip(decal, -25, 25)
        else:
            decal = np.zeros(3, np.float32)
        fiabilites.append(fiab)
        images.append(np.clip(Wk.astype(np.float32) + decal, 0, 255))
        couvertures.append(cov)
    S = np.stack(fiabilites)
    meilleur = S.argmax(axis=0)
    valeur = S.max(axis=0)
    choisi = trou & (valeur >= FIABILITE_MIN)
    resultat = P.copy()
    morceaux = []
    zone_fondu = cv2.dilate(trou.astype(np.uint8), np.ones((9, 9), np.uint8)) > 0
    for k, mod in enumerate(modeles):
        sel = choisi & (meilleur == k)
        n = int(sel.sum())
        if n < 30:
            continue
        alpha = cv2.GaussianBlur(sel.astype(np.float32), (0, 0), 2.0)
        alpha = np.where(zone_fondu, alpha, 0.0)[:, :, None]
        resultat = resultat * (1 - alpha) + images[k] * alpha
        morceaux.append({"part": round(100.0 * n / surface, 1), "echelle": round(mod["echelle"], 3), "rotation": round(mod["rotation"], 1),
                         "points": mod["points"], "fiabilite": round(float(valeur[sel].mean()), 2)})
    sortie = poster.copy()
    sortie[y0:y1, x0:x1] = np.clip(resultat, 0, 255).astype(np.uint8)
    reste = np.zeros((H, W), np.uint8)
    reste[y0:y1, x0:x1] = (trou & ~choisi).astype(np.uint8) * 255
    couvert = int(choisi.sum())
    rapport = {"couvert": round(100.0 * couvert / surface, 1), "reste": round(100.0 * (surface - couvert) / surface, 1),
               "morceaux": sorted(morceaux, key=lambda m: -m["part"]), "modeles": len(modeles)}
    return {"image": sortie, "reste": reste, "rapport": rapport}
