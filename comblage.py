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
FONDU_SIGMA = 7                # largeur (px) du fondu vers le poster intact, autour des morceaux recopiés
LISSAGE_ACCORD = 12            # lissage (px) de la carte de concordance avant de la prolonger dans la zone à combler
DISTANCE_MAX = 260             # au-delà de cette distance (px) du bord de la zone, aucune transformation n'est crue
MARGE = 160                    # zone de travail autour de la zone peinte
RAYON_REPERES = 250            # les corrections (angle, netteté, couleur) se calent sur les pixels à moins de ce rayon (px) de la zone peinte
AFFINER, NETTETE, COULEUR, RACCORD = True, True, True, True        # chaque correction peut être coupée (essais de mesure)
MODE_COULEUR = "cases"         # « cases » (selon la nuance), « polynome » (courbe globale) ou « decalage » (un seul décalage moyen)


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


def _gris(a):
    return cv2.cvtColor(np.ascontiguousarray(a, dtype=np.float32), cv2.COLOR_RGB2GRAY)


def _erreur(P, W, zone):
    """Écart moyen (0-255, coupé à 60 pour ignorer les vraies différences de contenu) entre deux images sur une zone."""
    if not zone.any():
        return 0.0
    return float(np.minimum(np.abs(P - W).max(axis=2)[zone], 60).mean())


def _affiner(P, Wk, cov, zone):
    """Petite correction affine (angle, échelle, cisaillement, décalage) de la 2e image remise en place pour qu'elle colle au poster
    autour de la zone peinte (la transformation globale est parfois un peu fausse à cet endroit). Gardée seulement si l'écart diminue.
    Renvoie (image, couverture, corrigée ?)."""
    if zone.sum() < 800:
        return Wk, cov, False
    h, w = Wk.shape[:2]
    s = 0.5
    g1 = cv2.resize(_gris(P), None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
    g2 = cv2.resize(_gris(Wk), None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
    mz = cv2.resize(zone.astype(np.uint8), (g1.shape[1], g1.shape[0]), interpolation=cv2.INTER_NEAREST)
    R = np.eye(2, 3, dtype=np.float32)
    try:
        _, R = cv2.findTransformECC(g1, g2, R, cv2.MOTION_AFFINE, (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 80, 1e-5), mz, 5)
    except cv2.error:
        return Wk, cov, False
    R[:, 2] /= s                                                           # décalage en pleine résolution
    if abs(R[0, 0] - 1) > 0.08 or abs(R[1, 1] - 1) > 0.08 or abs(R[0, 1]) > 0.08 or abs(R[1, 0]) > 0.08 or abs(R[0, 2]) > 30 or abs(R[1, 2]) > 30:
        return Wk, cov, False
    flags = cv2.INTER_CUBIC | cv2.WARP_INVERSE_MAP
    W2 = cv2.warpAffine(Wk, R, (w, h), flags=flags, borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0))
    c2 = cv2.warpAffine(cov.astype(np.uint8) * 255, R, (w, h), flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP) > 250
    z = zone & c2 & cov
    if _erreur(P, W2.astype(np.float32), z) < 0.97 * _erreur(P, Wk.astype(np.float32), z):
        return W2, c2, True
    return Wk, cov, False


def _nettete(P, Wk, zone):
    """Flou à ajouter à la 2e image pour avoir la même netteté que le poster (le poster a souvent été réduit ou compressé)."""
    if zone.sum() < 800:
        return Wk
    Wf = Wk.astype(np.float32)
    meilleur, sigma = _erreur(P, Wf, zone), 0.0
    for sig in (0.7, 1.0, 1.4, 2.0, 2.8):
        e = _erreur(P, cv2.GaussianBlur(Wf, (0, 0), sig), zone)
        if e < meilleur * 0.98:
            meilleur, sigma = e, sig
    return cv2.GaussianBlur(Wf, (0, 0), sigma) if sigma else Wf


def _traits(x):
    r, g, b = x[..., 0], x[..., 1], x[..., 2]
    return np.stack([np.ones_like(r), r, g, b, r * r, g * g, b * b, r * g, r * b, g * b], axis=-1)


def _couleur(P, Wk, zone, decal_secours):
    """Courbe de couleur complète (polynôme de degré 2 sur R, V, B) qui fait ressembler la 2e image au poster, apprise sur les pixels
    voisins qui se correspondent : corrige teinte, saturation et clarté DIFFÉREMMENT selon les couleurs (le crème d'une jupe, le noir d'un
    manteau…), là où un simple décalage moyen donnerait une jupe grise. Retombe sur le décalage médian s'il y a trop peu de pixels."""
    Wf = Wk.astype(np.float32) if Wk.dtype != np.float32 else Wk
    ys, xs = np.where(zone)
    if len(ys) < 600:
        return np.clip(Wf + decal_secours, 0, 255)
    if len(ys) > 50000:
        pick = np.random.default_rng(1).choice(len(ys), 50000, replace=False)
        ys, xs = ys[pick], xs[pick]
    X = Wf[ys, xs] / 255.0
    Y = P[ys, xs] / 255.0
    F = _traits(X)
    keep = np.ones(len(F), bool)
    coef = None
    for _ in range(3):                                                         # on écarte les pixels qui ne sont pas vraiment les mêmes
        A = F[keep].T @ F[keep] + 1e-2 * len(F[keep]) * np.eye(F.shape[1], dtype=np.float32) * 1e-2
        coef = np.linalg.solve(A, F[keep].T @ Y[keep])
        res = np.linalg.norm(F @ coef - Y, axis=1)
        seuil = max(0.04, 2.5 * float(np.median(res[keep])))
        keep = res < seuil
        if keep.sum() < 400:
            return np.clip(Wf + decal_secours, 0, 255)
    sortie = _traits(np.clip(Wf / 255.0, 0, 1)) @ coef
    return np.clip(sortie * 255.0, 0, 255).astype(np.float32)


def _lab(rgb):
    return cv2.cvtColor(np.clip(rgb, 0, 255).astype(np.float32) / 255.0, cv2.COLOR_RGB2LAB)


def _lisser3d(a):
    """Lissage gaussien (5 points) d'un tableau 3D, axe par axe."""
    k = np.array([1, 4, 6, 4, 1], np.float32) / 16.0
    for axe in range(3):
        p = np.pad(a, [(2, 2) if i == axe else (0, 0) for i in range(3)], mode="edge")
        sl = lambda d: tuple(slice(d, d + a.shape[axe]) if i == axe else slice(None) for i in range(3))
        a = sum(k[d] * p[sl(d)] for d in range(5))
    return a


def _couleur_cases(P, Wk, zone, decal_secours):
    """Correction de couleur qui dépend de la couleur d'origine : pour chaque nuance de la 2e image (en Lab : clarté, vert-rouge,
    bleu-jaune), le décalage moyen constaté sur les pixels voisins qui se correspondent et ont CETTE nuance. Un crème de jupe est corrigé
    comme le crème de la jupe voisine, un noir de manteau comme le noir du manteau : pas de jupe grise ni de teinte qui bave d'une couleur
    sur l'autre. Les nuances sans voisin servent des nuances proches (lissage), sinon le décalage moyen."""
    Wf = Wk.astype(np.float32)
    ys, xs = np.where(zone)
    if len(ys) < 600:
        return np.clip(Wf + decal_secours, 0, 255)
    labW, labP = _lab(Wf), _lab(P)
    # cases : L par 5 (0-100), a et b par 5 (-64 à 64)
    def cases(lab):
        i = np.clip((lab[..., 0] / 5.0).astype(np.int32), 0, 20)
        j = np.clip(((lab[..., 1] + 64) / 5.0).astype(np.int32), 0, 25)
        k = np.clip(((lab[..., 2] + 64) / 5.0).astype(np.int32), 0, 25)
        return i, j, k
    i, j, k = cases(labW[ys, xs][None])
    i, j, k = i[0], j[0], k[0]
    delta = labP[ys, xs] - labW[ys, xs]
    forme = (21, 26, 26)
    flat = (i * forme[1] + j) * forme[2] + k
    n = np.bincount(flat, minlength=int(np.prod(forme))).astype(np.float32).reshape(forme)
    somme = np.stack([np.bincount(flat, weights=delta[:, c], minlength=int(np.prod(forme))).astype(np.float32).reshape(forme) for c in range(3)], axis=-1)
    ns = _lisser3d(n)
    ss = np.stack([_lisser3d(somme[..., c]) for c in range(3)], axis=-1)
    global_med = np.median(delta, axis=0).astype(np.float32)
    table = np.where(ns[..., None] > 4.0, ss / np.maximum(ns[..., None], 1e-6), global_med)
    table = np.clip(table, [-30, -25, -25], [30, 25, 25])
    # lecture de la table par interpolation continue entre les 8 cases voisines (pas de paliers visibles dans les dégradés)
    fi = np.clip(labW[..., 0] / 5.0, 0, 20 - 1e-4)
    fj = np.clip((labW[..., 1] + 64) / 5.0, 0, 25 - 1e-4)
    fk = np.clip((labW[..., 2] + 64) / 5.0, 0, 25 - 1e-4)
    i0, j0, k0 = fi.astype(np.int32), fj.astype(np.int32), fk.astype(np.int32)
    ti, tj, tk = (fi - i0)[..., None], (fj - j0)[..., None], (fk - k0)[..., None]
    corr = 0.0
    for di in (0, 1):
        for dj in (0, 1):
            for dk in (0, 1):
                poids = (ti if di else 1 - ti) * (tj if dj else 1 - tj) * (tk if dk else 1 - tk)
                corr = corr + poids * table[np.minimum(i0 + di, 20), np.minimum(j0 + dj, 25), np.minimum(k0 + dk, 25)]
    out = labW + corr
    out[..., 0] = np.clip(out[..., 0], 0, 100)
    rgb = cv2.cvtColor(out.astype(np.float32), cv2.COLOR_LAB2RGB) * 255.0
    return np.clip(rgb, 0, 255).astype(np.float32)


def _raccord(P, M, zone_ring, trou):
    """Reste de différence de teinte entre le poster et la 2e image corrigée, mesuré juste autour de la zone, étalé en douceur dans la zone :
    la zone collée se raccorde au poster sans bord visible. Renvoie le champ de correction (h, w, 3)."""
    poids = zone_ring.astype(np.float32)
    diff = (P - M) * poids[..., None]
    num = cv2.GaussianBlur(diff, (0, 0), 30)
    den = cv2.GaussianBlur(poids, (0, 0), 30)[..., None]
    champ = np.where(den > 0.02, num / np.maximum(den, 1e-6), 0.0)
    return np.clip(champ, -35, 35).astype(np.float32)


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
    d_trou = cv2.distanceTransform((~trou).astype(np.uint8), cv2.DIST_L2, 5)
    decale = np.array([[1, 0, -x0], [0, 1, -y0], [0, 0, 1.0]])
    un = np.full(source.shape[:2], 255, np.uint8)
    fiabilites, images, couvertures = [], [], []
    for mod in modeles:
        Mr = (decale @ mod["M"])[:2]
        Wk = cv2.warpAffine(source, Mr, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0))
        cov = cv2.warpAffine(un, Mr, (w, h), flags=cv2.INTER_LINEAR) > 250
        cov = cv2.erode(cov.astype(np.uint8), np.ones((5, 5), np.uint8)) > 0
        pres = cov & loin & (d_trou < RAYON_REPERES)                         # pixels voisins de la zone, où l'on peut comparer
        if AFFINER:
            Wk, cov, _ = _affiner(P, Wk, cov, pres)
            cov = cv2.erode(cov.astype(np.uint8), np.ones((5, 5), np.uint8)) > 0
            pres = cov & loin & (d_trou < RAYON_REPERES)
        Wf = cv2.GaussianBlur(Wk.astype(np.float32), (0, 0), 1.5)
        ecart = np.abs(Pf - Wf).max(axis=2)
        valide = cov & loin                                                   # là où on peut comparer : source présente, hors de la zone peinte
        accord = ((ecart < ECART_ACCORD) & valide).astype(np.float32)
        lisse = cv2.GaussianBlur(accord, (0, 0), LISSAGE_ACCORD) / np.maximum(cv2.GaussianBlur(valide.astype(np.float32), (0, 0), LISSAGE_ACCORD), 1e-6)
        fiab = _prolonger(lisse, valide)
        fiab[~cov] = 0.0
        # même rendu que le poster : netteté, couleurs (courbe complète), puis raccord des teintes avec le pourtour de la zone
        zone = pres & (ecart < 90)                                            # voisins qui se correspondent (large : la couleur peut différer)
        decal = np.zeros(3, np.float32)
        if (accord > 0).sum() > 200:
            decal = np.clip(np.median((P - Wk.astype(np.float32))[accord > 0], axis=0), -25, 25)
        img = Wk.astype(np.float32)
        if NETTETE:
            img = _nettete(P, img, zone)
        if not COULEUR or MODE_COULEUR == "decalage":
            img = np.clip(img + decal, 0, 255)
        elif MODE_COULEUR == "polynome":
            img = _couleur(P, img, zone, decal)
        else:
            img = _couleur_cases(P, img, zone & (d_trou < 110), decal)
        if RACCORD:
            img = np.clip(img + _raccord(P, img, zone & (d_trou < 70), trou), 0, 255)
        fiabilites.append(fiab)
        images.append(img.astype(np.float32))
        couvertures.append(cov)
    S = np.stack(fiabilites)
    meilleur = S.argmax(axis=0)
    valeur = S.max(axis=0)
    choisi = trou & (valeur >= FIABILITE_MIN)
    resultat = P.copy()
    morceaux = []
    for k, mod in enumerate(modeles):
        sel = choisi & (meilleur == k)
        n = int(sel.sum())
        if n < 30:
            continue
        # dedans : bords légèrement fondus ; dehors (poster intact) : long fondu, seulement là où la 2e image ressemble au poster
        dedans = cv2.GaussianBlur(sel.astype(np.float32), (0, 0), 2.0) * trou
        dehors = cv2.GaussianBlur(sel.astype(np.float32), (0, 0), FONDU_SIGMA) * (~trou) * (fiabilites[k] >= FIABILITE_MIN) * couvertures[k]
        alpha = np.clip(dedans + dehors, 0.0, 1.0)[:, :, None].astype(np.float32)
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
