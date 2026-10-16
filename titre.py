"""
Titre du poster : découpage automatique en lignes et écriture sans rétrécir les lettres.

But : tous les posters gardent la même taille de lettres (89 px pour le titre, 49 px dessous), pour que la liste
des posters reste homogène. Règles tirées des posters faits à la main sous Photoshop :
- « Titre : sous-titre » (ou « - ») : le titre en grand, la suite en petit dessous ; « Mission : Impossible » garde ses « : ».
- « Indiana Jones et le Temple maudit », « Vaiana, la légende… » : coupés avant « et le… » ou à la virgule,
  sauf si le titre commence par un article (« La Belle et la Bête », « Le Bon, la Brute et le Truand »).
- « Ant-Man et la Guêpe » (un seul mot après « et la ») reste sur une ligne : « et / la » empilés en tout petit.
  Taper « et/la » (avec une barre) dans un champ les empile toujours, même quand la ligne tiendrait sans ça.
- Titre trop long : petits mots en petit (« LE SEIGNEUR des ANNEAUX »), sinon 2 lignes de 89 px ; un article seul
  passe au-dessus (« LES » / « INDESTRUCTIBLES ») ; sous-titre trop long : 2 lignes de 49 px.
- Numéro de suite (« Conjuring 2 », « Vol. 2 », « Partie 1 ») : grand chiffre gris derrière le titre (films).
- « Star Wars, Épisode IV : … » : « Épisode IV » est retiré.
Les lettres ne rétrécissent qu'en dernier recours, et la page le signale.
"""
import re

GRAND, PETIT = 89, 49
PETIT_DANS_GRAND = 49 / 89      # petits mots sur une ligne de 89 px (« LE SEIGNEUR des ANNEAUX »)
PILE = 0.44                     # « et / la » empilés : deux mots dont la hauteur totale égale celle des capitales
TAILLE_MINI = 24
MARGE = 16                      # px entre le texte et le bord intérieur du cadre (mesuré sur les posters faits à la main)

PETITS_MOTS = {"le", "la", "les", "de", "des", "du", "en", "et", "au", "aux", "à", "un", "une", "sur",
               "of", "the", "and", "&"}
ARTICLES = {"le", "la", "les", "l", "un", "une", "des", "du", "de", "the", "a", "an"}
# Titres dont les « : » font partie du nom
DEUX_POINTS_GARDES = ("mission : impossible", "mission: impossible")

_SEP = re.compile(r"\s+[:\-–—]\s+|\s*:\s+")
_EPISODE = re.compile(r",?\s+[ÉéEe]pisode\s+[IVXLC\d]+\s*(?=$|[:\-–—])")
_ET = re.compile(r"\s+(?:et|and|&)\s+(?:le|la|les|l['’]|un|une|des|du|the)\b", re.I)
_LIAISON = re.compile(r"\s+(?:à|dans|pour)\s+", re.I)
_SUITE = re.compile(
    r"[\s,:\-–—]*(?:\b(?:vol(?:ume)?\.?|partie|chapitre|part)\s*(\d{1,2})"
    r"|\b(\d{1,2})\s*(?:e|re|ère|eme|ème|nde|nd)\s+partie"
    r"|\s(\d{1,2})"
    r"|\s((?-i:II|III|IV|VI|VII|VIII|IX)))\s*$", re.I)
_ROMAINS = {"II": 2, "III": 3, "IV": 4, "VI": 6, "VII": 7, "VIII": 8, "IX": 9}


def _premier_mot(texte):
    m = re.match(r"\s*([^\s'’]+)(['’])?", texte or "")
    return (m.group(1) or "").lower() if m else ""


def _commence_par_article(texte):
    return _premier_mot(texte) in ARTICLES


# ---------------------------------------------------------------------------------------------------------------
# Écriture d'une ligne : taille gardée, petits mots en petit, rétrécissement seulement en dernier recours
# ---------------------------------------------------------------------------------------------------------------

def _bloc_mot(mot):
    """Mot écrit en grand, ou « ET/LA » tapé avec une barre : les deux petits mots toujours empilés.
    Seulement entre deux petits mots (« AC/DC » reste tel quel)."""
    a, barre, b = mot.partition("/")
    if barre and a.lower() in PETITS_MOTS and b.lower() in PETITS_MOTS:
        return ("pile", a, b)
    return ("mot", mot)


def _blocs(mots, avec_petits):
    """Découpe une ligne en blocs : ("mot", texte) en grand, ("petit", texte) ou ("pile", haut, bas)."""
    if not avec_petits or len(mots) < 2:
        return [_bloc_mot(m) for m in mots]
    petit = [m.lower() in PETITS_MOTS and i < len(mots) - 1 for i, m in enumerate(mots)]
    if all(petit[:-1]) and len(mots) <= 2 or not any(petit):
        return [_bloc_mot(m) for m in mots]
    blocs, i = [], 0
    while i < len(mots):
        if not petit[i]:
            blocs.append(_bloc_mot(mots[i]))
            i += 1
            continue
        j = i
        while j < len(mots) and petit[j]:
            j += 1
        groupe = mots[i:j]
        if len(groupe) == 2:
            blocs.append(("pile", groupe[0], groupe[1]))
        else:
            blocs.extend(("petit", m) for m in groupe)
        i = j
    return blocs


def _tailles(taille):
    return taille, max(8, round(taille * PETIT_DANS_GRAND)), max(8, round(taille * PILE))


def _mesure(blocs, taille, police):
    """Largeur totale et largeur de chaque bloc."""
    g, p, s = _tailles(taille)
    fg, fp, fs = police(g), police(p), police(s)
    larges, total = [], 0
    for k, b in enumerate(blocs):
        if b[0] == "mot":
            w = fg.getlength(b[1])
        elif b[0] == "petit":
            w = fp.getlength(b[1])
        else:
            w = max(fs.getlength(b[1]), fs.getlength(b[2]))
        larges.append(w)
        total += w
        if k:   # espace : celle du plus petit des deux blocs voisins
            voisins = [blocs[k - 1][0], b[0]]
            total += (fg if voisins == ["mot", "mot"] else fp).getlength(" ")
    return total, larges


def plan_ligne(texte, taille_base, largeur_max, police, avec_petits=None):
    """Comment écrire la ligne : {"blocs", "taille", "mode" (normal / petits / reduit), "largeur"}.
    avec_petits : None = seulement pour les lignes de 89 px."""
    mots = (texte or "").upper().split()
    if not mots:
        return {"blocs": [], "taille": taille_base, "mode": "normal", "largeur": 0}
    if avec_petits is None:
        avec_petits = taille_base >= GRAND
    simples = _blocs(mots, False)
    w, _ = _mesure(simples, taille_base, police)
    if w <= largeur_max:
        return {"blocs": simples, "taille": taille_base, "mode": "normal", "largeur": w}
    blocs = _blocs(mots, avec_petits)
    if blocs != simples:
        w, _ = _mesure(blocs, taille_base, police)
        if w <= largeur_max:
            return {"blocs": blocs, "taille": taille_base, "mode": "petits", "largeur": w}
    taille = taille_base
    while taille > TAILLE_MINI:
        taille -= 2
        w, _ = _mesure(blocs, taille, police)
        if w <= largeur_max:
            break
    return {"blocs": blocs, "taille": taille, "mode": "reduit", "largeur": w}


def _hauteur_capitale(font):
    return -font.getbbox("H", anchor="ls")[1]


def ecrire_ligne(draw, texte, taille_base, centre_x, ligne_base, largeur_max, police, couleur="white"):
    """Écrit la ligne centrée (petits mots et mots empilés compris) ; renvoie le plan (taille réelle, mode)."""
    plan = plan_ligne(texte, taille_base, largeur_max, police)
    if not plan["blocs"]:
        return plan
    g, p, s = _tailles(plan["taille"])
    fg, fp, fs = police(g), police(p), police(s)
    total, larges = _mesure(plan["blocs"], plan["taille"], police)
    x = centre_x - total / 2
    cap_g, cap_s = _hauteur_capitale(fg), _hauteur_capitale(fs)
    for k, (b, w) in enumerate(zip(plan["blocs"], larges)):
        if k:
            voisins = [plan["blocs"][k - 1][0], b[0]]
            x += (fg if voisins == ["mot", "mot"] else fp).getlength(" ")
        if b[0] == "mot":
            draw.text((x, ligne_base), b[1], fill=couleur, font=fg, anchor="ls")
        elif b[0] == "petit":
            draw.text((x, ligne_base), b[1], fill=couleur, font=fp, anchor="ls")
        else:   # pile : le mot du haut affleure le haut des capitales, celui du bas est posé sur la ligne
            draw.text((x + w / 2, ligne_base - (cap_g - cap_s)), b[1], fill=couleur, font=fs, anchor="ms")
            draw.text((x + w / 2, ligne_base), b[2], fill=couleur, font=fs, anchor="ms")
        x += w
    return plan


# ---------------------------------------------------------------------------------------------------------------
# Découpage automatique du titre choisi dans la recherche
# ---------------------------------------------------------------------------------------------------------------

def _tient(texte, taille, largeur, police, avec_petits=True):
    plan = plan_ligne(texte, taille, largeur, police, avec_petits if taille >= GRAND else False)
    return plan["mode"] != "reduit", plan["mode"]


def _deux_lignes(texte, taille, largeur, police):
    """Meilleure coupe en 2 lignes qui tiennent toutes les deux (lignes les plus égales), ou None."""
    mots = texte.split()
    meilleure = None
    for i in range(1, len(mots)):
        a, b = " ".join(mots[:i]), " ".join(mots[i:])
        if all(m.lower() in PETITS_MOTS for m in mots[:i]) or all(m.lower() in PETITS_MOTS for m in mots[i:]):
            continue
        pa, pb = plan_ligne(a, taille, largeur, police), plan_ligne(b, taille, largeur, police,
                                                                    None if taille >= GRAND else False)
        if pa["mode"] == "reduit" or pb["mode"] == "reduit":
            continue
        # pas de ligne qui finit par un petit mot (« PIRATES DES »), puis le moins de petits mots, puis lignes égales
        note = (a.split()[-1].lower() in PETITS_MOTS, (pa["mode"] == "petits") + (pb["mode"] == "petits"),
                max(pa["largeur"], pb["largeur"]))
        if meilleure is None or note < meilleure[0]:
            meilleure = (note, a, b)
    return (meilleure[1], meilleure[2]) if meilleure else None


def _coupe_equilibree(texte):
    mots = texte.split()
    if len(mots) < 2:
        return texte, ""
    i = min(range(1, len(mots)), key=lambda i: abs(len(" ".join(mots[:i])) - len(" ".join(mots[i:]))))
    return " ".join(mots[:i]), " ".join(mots[i:])


def _numero_de_suite(texte, vide_permis=False):
    """(texte sans le numéro final, numéro) ou (texte, "").
    vide_permis : un sous-titre fait seulement de « Partie 1 » / « 1re partie » disparaît entièrement."""
    m = _SUITE.search(texte)
    if not m:
        return texte, ""
    reste = texte[:m.start()].rstrip(" ,:-–—")
    mot_cle = bool(m.group(1) or m.group(2))
    if not re.search(r"[A-Za-zÀ-ÿ]", reste) and not (vide_permis and mot_cle and not reste):
        return texte, ""
    n = next(g for g in m.groups() if g)
    return reste, str(_ROMAINS.get(n, n)).lstrip("0") or n


def largeur_utile(bordure):
    """Largeur disponible pour le texte avec un cadre de « bordure » px."""
    return 1000 - 2 * bordure - 2 * MARGE


def decouper(titre, largeur, police, film=True):
    """Lignes proposées pour un titre : {"l1".."l5", "saison"} (chaînes vides si rien)."""
    res = {"l1": "", "l2": "", "l3": "", "l4": "", "l5": "", "saison": ""}
    t = re.sub(r"\s+", " ", (titre or "").replace("‎", "")).strip()
    if not t:
        return res
    t = _EPISODE.sub(" ", t).strip()                         # « Star Wars, Épisode IV : … »

    garde = next((g for g in DEUX_POINTS_GARDES if t.lower().startswith(g)), None)
    protege = t[:len(garde)] if garde else ""
    corps = t[len(garde):] if garde else t
    parts = _SEP.split(corps, maxsplit=1)
    titre_p = (protege + parts[0]).strip()
    sous = parts[1].strip() if len(parts) > 1 else ""
    if sous:
        sous = _SEP.sub(" : ", sous)
    titre_p = titre_p.strip(" :-–—")

    if film:                                                  # numéro de suite → grand chiffre gris
        n = ""
        if sous:
            reste, n = _numero_de_suite(sous, vide_permis=True)
            if n:
                sous = reste
        if not n:
            titre_p, n = _numero_de_suite(titre_p)
        res["saison"] = n

    if not sous and not _commence_par_article(titre_p):
        m = re.match(r"^([^,]+),\s+(.+)$", titre_p)           # « Vaiana, la légende du bout du monde »
        if m and len(m.group(1).split()) <= 4:
            titre_p, sous = m.group(1).strip(), m.group(2).strip()
    if not sous and not _commence_par_article(titre_p):
        m = _ET.search(titre_p)                               # « Indiana Jones et le Temple maudit »
        if m:
            apres = titre_p[m.end():].strip()
            if len(apres.split()) >= 2:
                titre_p, sous = titre_p[:m.start()].strip(), titre_p[m.start():].strip()
    if not sous and not _commence_par_article(titre_p) and not _tient(titre_p, GRAND, largeur, police)[0]:
        for m in _LIAISON.finditer(titre_p):                  # « Harry Potter à l'école des sorciers »
            avant = titre_p[:m.start()].strip()
            if _tient(avant, GRAND, largeur, police, False)[0]:
                titre_p, sous = avant, titre_p[m.start():].strip()
                break

    # Titre en grand : une ligne, petits mots en petit, ou deux lignes
    mots = titre_p.split()
    # « Ant-Man et la Guêpe » : « et / la » empilés sur une ligne ; « La Belle / et la Bête » : deux lignes
    pile = bool(re.search(r"\s(?:et|and|&)\s(?:le|la|les|the)\s\S+$", titre_p, re.I)) and not _commence_par_article(titre_p)
    lignes_titre = 1
    if plan_ligne(titre_p, GRAND, largeur, police, False)["mode"] == "normal":
        res["l2"] = titre_p
    elif len(mots) == 2 and mots[0].lower() in ARTICLES and _tient(mots[1], GRAND, largeur, police, False)[0]:
        res["l1"], res["l2"] = mots[0], mots[1]               # « LES » au-dessus de « INDESTRUCTIBLES »
    else:
        une = _tient(titre_p, GRAND, largeur, police)[0]
        deux = _deux_lignes(titre_p, GRAND, largeur, police)
        if une and (sous or pile or not deux):
            res["l2"] = titre_p
        elif deux:
            res["l2"], res["l3"] = deux
            lignes_titre = 2
        elif len(mots) >= 2:                                  # ne tient pas : la page l'indiquera en rouge
            res["l2"], res["l3"] = _coupe_equilibree(titre_p)
            lignes_titre = 2
        else:
            res["l2"] = titre_p

    if sous:
        if lignes_titre == 2:
            res["l5"] = sous
        elif _tient(sous, PETIT, largeur, police, False)[0]:
            res["l4"] = sous
        else:
            res["l4"], res["l5"] = _deux_lignes(sous, PETIT, largeur, police) or _coupe_equilibree(sous)
    return res
