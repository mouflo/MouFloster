"""🎨 Apparence de MouFloster : skins (CSS seul) et couleurs au choix, par compte.

Le style commun des quatre applis (ui/mou-ui.css) n'est jamais modifié. Une page de MouFloster charge en dernier
« /skin.css », que ce module assemble pour la personne connectée : skin choisi, puis ses couleurs.

- Skins officiels : dossiers `ui/skins/<id>/` (skin.css + skin.json), dans le dépôt, relus avant d'arriver.
- Skins brouillons : `data/skins_perso/<compte>/<id>/`, visibles seulement par leur auteur (jamais sur GitHub).
- Choix : `data/apparence/<compte>.json` ({"skin": "", "couleurs": {...}}).
Un skin ne contient que du CSS : pas de JavaScript, pas d'@import, pas d'adresse extérieure.
"""
import json
import os
import re
import threading
from pathlib import Path

from flask import Response, jsonify, redirect, request

BASE_DIR = Path(__file__).resolve().parent
SKINS_DIR = BASE_DIR / "ui" / "skins"
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data")))
TAILLE_MAX = 150_000            # octets de CSS par skin
BROUILLONS_MAX = 10             # par compte
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
_verrou = threading.Lock()

# Couleurs d'origine (celles de ui/mou-ui.css) : le menu les propose tant que rien n'est choisi.
COULEURS = {"fond": "#101010", "cartes": "#1c1c1c", "texte": "#ffffff", "accent": "#52b54b"}
LIBELLES = {"fond": "Fond de page", "cartes": "Cartes et fenêtres", "texte": "Texte", "accent": "Couleur d'accent (boutons)"}


# ---------------------------------------------------------------- validation du CSS

def _decoder_echappements(css: str) -> str:
    """Pour la vérification seulement : « \\75 rl( » ne doit pas passer pour autre chose que « url( »."""
    def un(m):
        try:
            return chr(int(m.group(1), 16))
        except (ValueError, OverflowError):
            return ""
    css = re.sub(r"\\([0-9a-fA-F]{1,6})\s?", un, css)
    return re.sub(r"\\(.)", r"\1", css, flags=re.S)


def verifier_css(css: str):
    """None si le CSS est acceptable, sinon la raison du refus (en français)."""
    if not isinstance(css, str):
        return "CSS illisible."
    if len(css.encode("utf-8")) > TAILLE_MAX:
        return f"CSS trop gros (plus de {TAILLE_MAX // 1000} Ko)."
    if "\x00" in css:
        return "Caractère interdit dans le CSS."
    sans = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    net = _decoder_echappements(sans).lower()
    if "@import" in net:
        return "« @import » est interdit (un skin tient dans un seul fichier)."
    if "<" in net or "javascript:" in net or "expression(" in net or "-moz-binding" in net or "behavior:" in net:
        return "Le CSS contient quelque chose d'interdit (balise, script)."
    for m in re.finditer(r"url\(\s*(['\"]?)(.*?)\1\s*\)", net, flags=re.S):
        cible = m.group(2).strip()
        if not (cible.startswith("/ui/skins/") or cible.startswith("data:image/") or cible.startswith("data:font/")
                or cible.startswith("data:application/font")):
            return ("Adresse refusée dans url(...) : seules les images de ton dossier « /ui/skins/ » et les "
                    "images intégrées (data:image/…) sont permises.")
    if "src:" in net and "http" in net:
        return "Police ou ressource extérieure refusée."
    return None


# ---------------------------------------------------------------- couleurs

def _rgb(h):
    return int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16)


def _hex(c):
    return "#%02x%02x%02x" % tuple(max(0, min(255, round(x))) for x in c)


def _melange(a, b, t):
    """a, puis t (0 à 1) de b."""
    ra, rb = _rgb(a), _rgb(b)
    return _hex(tuple(ra[i] + (rb[i] - ra[i]) * t for i in range(3)))


def _clair(h):
    r, g, b = _rgb(h)
    return (0.299 * r + 0.587 * g + 0.114 * b) > 140


def couleurs_valides(c):
    """Garde seulement les couleurs connues, au format #rrggbb."""
    if not isinstance(c, dict):
        return {}
    return {k: v.lower() for k, v in c.items() if k in COULEURS and isinstance(v, str) and _HEX.match(v)}


def css_couleurs(c):
    """Variables redéfinies pour les couleurs choisies (le reste du style d'origine ne bouge pas)."""
    c = couleurs_valides(c)
    if not c:
        return ""
    v = {}
    fond = c.get("fond", COULEURS["fond"])
    if "fond" in c:
        v["--bg"] = fond
    if "cartes" in c:
        carte = c["cartes"]
        vers = "#000000" if _clair(carte) else "#ffffff"
        v.update({"--card": carte, "--surface": carte, "--box-bg": carte,
                  "--surface2": _melange(carte, vers, .05), "--field": _melange(carte, vers, .10),
                  "--input-bg": _melange(carte, vers, .10), "--line": _melange(carte, vers, .18),
                  "--box-border": _melange(carte, vers, .18), "--input-border": _melange(carte, vers, .18),
                  "--thumb-border": _melange(carte, vers, .18)})
    if "texte" in c:
        t = c["texte"]
        v.update({"--text": t, "--fg": t, "--input-fg": t, "--text2": _melange(t, fond, .15), "--muted": _melange(t, fond, .38)})
    if "accent" in c:
        a = c["accent"]
        v.update({"--accent": a, "--accent-hover": _melange(a, "#000000" if _clair(a) else "#ffffff", .12)})
    sortie = ":root, :root[data-theme] {\n"
    if "fond" in c:
        sortie += f"    color-scheme: {'light' if _clair(fond) else 'dark'};\n"
    sortie += "".join(f"    {k}: {val};\n" for k, val in v.items()) + "}\n"
    if "texte" in c:
        sortie += ".mou-title .w { color: var(--text); }\n"
    return sortie


# ---------------------------------------------------------------- comptes, choix, skins

def _compte():
    """(clé de fichier, est_connecté) de la personne connectée ; l'admin a la clé « _admin ».
    Les comptes « lecteur » (MouFlanga, MouFloster) et « copain » (MouFloster, MouFlopening) ont chacun leur choix ;
    une appli à compte unique (MouFlanimeXer) n'a que l'admin."""
    import auth
    if not hasattr(auth, "role"):
        return "_admin" if auth.is_logged_in() else None
    r = auth.role()
    if r == "admin":
        return "_admin"
    if r in ("lecteur", "copain"):
        return re.sub(r"[^A-Za-z0-9._@+-]", "_", auth.utilisateur() or "inconnu")
    return None


def _lire(path, defaut):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return defaut


def _ecrire(path, donnees):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(donnees, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _fichier_choix(cle):
    return DATA_DIR / "apparence" / f"{cle}.json"


def lire_choix(cle):
    d = _lire(_fichier_choix(cle), {})
    skin = d.get("skin") if isinstance(d.get("skin"), str) else ""
    return {"skin": skin, "couleurs": couleurs_valides(d.get("couleurs"))}


def _dossier_skin(cle, ident):
    """Dossier d'un skin pour ce compte : « perso:<id> » = brouillon du compte, sinon skin officiel."""
    if ident.startswith("perso:"):
        i = ident[6:]
        return (DATA_DIR / "skins_perso" / cle / i) if cle and _ID.match(i) else None
    return (SKINS_DIR / ident) if _ID.match(ident) else None


def _fiche(dossier, ident, perso):
    info = _lire(dossier / "skin.json", {})
    if not isinstance(info, dict):
        info = {}
    return {"id": ("perso:" + ident) if perso else ident, "nom": str(info.get("nom") or ident)[:60],
            "auteur": str(info.get("auteur") or "")[:60], "description": str(info.get("description") or "")[:200],
            "perso": perso}


def lister_skins(cle):
    out = []
    if SKINS_DIR.is_dir():
        for d in sorted(SKINS_DIR.iterdir()):
            if d.is_dir() and _ID.match(d.name) and (d / "skin.css").is_file():
                out.append(_fiche(d, d.name, False))
    perso = DATA_DIR / "skins_perso" / (cle or "")
    if cle and perso.is_dir():
        for d in sorted(perso.iterdir()):
            if d.is_dir() and _ID.match(d.name) and (d / "skin.css").is_file():
                out.append(_fiche(d, d.name, True))
    return out


def css_de(cle, choix):
    """CSS final de la personne : skin choisi (s'il existe et reste valide), puis ses couleurs."""
    morceaux = []
    ident = choix.get("skin") or ""
    if ident:
        dossier = _dossier_skin(cle, ident)
        try:
            css = (dossier / "skin.css").read_text(encoding="utf-8") if dossier else ""
        except OSError:
            css = ""
        if css and verifier_css(css) is None:
            morceaux.append(f"/* skin : {ident} */\n{css}")
    couleurs = css_couleurs(choix.get("couleurs"))
    if couleurs:
        morceaux.append("/* couleurs choisies */\n" + couleurs)
    return "\n".join(morceaux)


# ---------------------------------------------------------------- routes

def init_app(app):
    @app.route("/skin.css")
    def skin_css():
        cle = _compte()
        css = ""
        if cle and request.args.get("off") != "1":
            css = css_de(cle, lire_choix(cle))
        r = Response(css, mimetype="text/css")
        r.headers["Cache-Control"] = "private, no-store, max-age=0"
        return r

    @app.context_processor
    def _skin_url():
        """Adresse de la feuille de style avec une empreinte de son contenu : une copie ancienne gardée par un
        relais ou le navigateur ne peut plus être resservie dès que les couleurs changent."""
        def skin_url(off=False):
            if off:
                return "/skin.css?off=1"
            try:
                import hashlib
                cle = _compte()
                css = css_de(cle, lire_choix(cle)) if cle else ""
                return "/skin.css?v=" + hashlib.md5(css.encode("utf-8")).hexdigest()[:10]
            except Exception:
                return "/skin.css"
        return {"skin_url": skin_url}

    @app.route("/apparence/reset")
    def apparence_reset():
        """Secours si un skin rend la page inutilisable : /apparence/reset remet le style d'origine."""
        cle = _compte()
        if cle:
            try:
                _fichier_choix(cle).unlink(missing_ok=True)
            except OSError:
                pass
        return redirect("/")

    @app.route("/api/apparence", methods=["GET", "POST"])
    def api_apparence():
        cle = _compte()
        if not cle:
            return jsonify({"error": "Session expirée : reconnecte-toi."}), 401
        if request.method == "GET":
            return jsonify({"skins": lister_skins(cle), "choix": lire_choix(cle), "defauts": COULEURS,
                            "libelles": LIBELLES, "brouillons_max": BROUILLONS_MAX})
        d = request.get_json(silent=True) or {}
        if d.get("reset"):
            with _verrou:
                _fichier_choix(cle).unlink(missing_ok=True)
            return jsonify({"ok": True, "choix": lire_choix(cle)})
        skin = d.get("skin", "")
        if not isinstance(skin, str) or (skin and skin not in {s["id"] for s in lister_skins(cle)}):
            return jsonify({"ok": False, "error": "Skin inconnu."}), 400
        with _verrou:
            _ecrire(_fichier_choix(cle), {"skin": skin, "couleurs": couleurs_valides(d.get("couleurs"))})
        return jsonify({"ok": True, "choix": lire_choix(cle)})

    @app.route("/api/apparence/brouillon", methods=["POST"])
    def api_apparence_brouillon():
        """Skin brouillon de la personne connectée : pour essayer en direct avant de le proposer (Pull Request)."""
        cle = _compte()
        if not cle:
            return jsonify({"error": "Session expirée : reconnecte-toi."}), 401
        d = request.get_json(silent=True) or {}
        base = DATA_DIR / "skins_perso" / cle
        if d.get("supprimer"):
            i = str(d["supprimer"]).replace("perso:", "", 1)
            if not _ID.match(i):
                return jsonify({"ok": False, "error": "Nom invalide."}), 400
            import shutil
            shutil.rmtree(base / i, ignore_errors=True)
            c = lire_choix(cle)
            if c["skin"] == "perso:" + i:
                _ecrire(_fichier_choix(cle), {"skin": "", "couleurs": c["couleurs"]})
            return jsonify({"ok": True})
        nom = str(d.get("nom", "")).strip()[:60]
        i = re.sub(r"[^a-z0-9]+", "-", nom.lower()).strip("-")[:40]
        if not i or not _ID.match(i):
            return jsonify({"ok": False, "error": "Donne un nom au skin (lettres et chiffres)."}), 400
        css = str(d.get("css", ""))
        if not css.strip():
            return jsonify({"ok": False, "error": "Le CSS est vide."}), 400
        raison = verifier_css(css)
        if raison:
            return jsonify({"ok": False, "error": raison}), 400
        existants = [x.name for x in base.iterdir() if x.is_dir()] if base.is_dir() else []
        if i not in existants and len(existants) >= BROUILLONS_MAX:
            return jsonify({"ok": False, "error": f"{BROUILLONS_MAX} brouillons au maximum : supprime-en un."}), 400
        with _verrou:
            (base / i).mkdir(parents=True, exist_ok=True)
            (base / i / "skin.css").write_text(css, encoding="utf-8")
            _ecrire(base / i / "skin.json", {"nom": nom, "auteur": cle if cle != "_admin" else "", "description": "Brouillon"})
        return jsonify({"ok": True, "id": "perso:" + i, "message": f"Brouillon « {nom} » enregistré."})
