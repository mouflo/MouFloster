"""
🔗 Applis voisines (fichier identique dans MouFloster et MouFlopening) : les applis MouFl se parlent directement,
de serveur à serveur, sans passer par le navigateur (générique présent ? nouveautés sans poster ? …).

- Chaque appli a sa clé « voisines » (MOUFL_CLE_VOISINES dans data/secrets.env, créée toute seule au démarrage,
  jamais sur GitHub). Une voisine l'envoie dans l'en-tête « X-MouFl-Cle ». Les adresses /api/voisin/… ne passent pas
  par l'écran de connexion : la clé les protège (trop de mauvaises clés → adresse bloquée 10 minutes).
- Sur le même serveur, rien à régler : l'appli lit la clé de sa voisine dans le dossier de celle-ci
  (<dossier des applis>/<voisine>/data/secrets.env, lisible seulement par root) et l'appelle en local (127.0.0.1).
  Sur une autre machine : adresse et clé de la voisine dans ⚙️ Réglages → Connexions (<VOISINE>_URL, <VOISINE>_CLE).
- 📥 « Reprendre d'une appli voisine » : copie les réglages communs (Emby, TheMovieDB, Telegram) d'une autre appli
  MouFl du même serveur. Les valeurs ne passent jamais par la page : seulement les 4 derniers caractères.
"""
import functools
import hmac
import json
import logging
import os
import secrets as _secrets
import threading
import time
from pathlib import Path

import requests
from flask import jsonify, request

logger = logging.getLogger(__name__)

APPLIS = {"moufloster": ("MouFloster", 8000), "mouflopening": ("MouFlopening", 8001),
          "mouflanga": ("MouFlanga", 5002), "mouflanimexer": ("MouFlanimeXer", 5000)}
COMMUNS = (("EMBY_URL", "Adresse d'Emby"), ("EMBY_API_KEY", "Clé API d'Emby"), ("TMDB_API_KEY", "Clé TheMovieDB"),
           ("TELEGRAM_BOT_TOKEN", "Jeton du bot Telegram"), ("TELEGRAM_CHAT_ID", "Groupe Telegram"))
LISIBLES = {"EMBY_URL"}                 # pas un secret : affiché en entier
CLE = "MOUFL_CLE_VOISINES"
ENTETE = "X-MouFl-Cle"
_INTERDITS = set('"$`\\\n\r')

_etat = {"base": None, "ecrire": None, "apres_reprise": None}
_echecs, _verrou = {}, threading.Lock()
_MAX_ECHECS, _FENETRE = 10, 600


class ErreurVoisin(Exception):
    """Message lisible, à afficher tel quel."""


def _moi():
    return Path(_etat["base"]).name.lower() if _etat["base"] else ""


def nom(app_id):
    return APPLIS.get(app_id, (app_id, 0))[0]


def lire_env(chemin):
    """{NOM: valeur} d'un fichier secrets.env (lignes NOM=valeur, guillemets retirés)."""
    out = {}
    try:
        for ligne in Path(chemin).read_text(encoding="utf-8", errors="ignore").splitlines():
            ligne = ligne.strip()
            if not ligne or ligne.startswith("#") or "=" not in ligne:
                continue
            k, _, v = ligne.partition("=")
            out[k.replace("export ", "").strip()] = v.strip().strip("\"'")
    except OSError:
        pass
    return out


def dossier(app_id):
    """Dossier de la voisine si elle est installée sur ce serveur (à côté de cette appli), sinon None."""
    if not _etat["base"] or app_id not in APPLIS or app_id == _moi():
        return None
    d = Path(_etat["base"]).parent / app_id
    return d if (d / "data").is_dir() else None


def reglages_de(app_id):
    """Réglages communs d'une voisine du même serveur : {NOM: valeur} (seulement ceux qui sont remplis)."""
    d = dossier(app_id)
    return _reglages(app_id, d) if d is not None else {}


def mes_reglages():
    """Les mêmes réglages pour cette appli-ci (MouFlopening garde Emby dans config.json, pas dans l'environnement)."""
    env = {k: os.getenv(k, "").strip() for k, _ in COMMUNS if os.getenv(k, "").strip()}
    if _etat["base"]:
        for k, v in _reglages(_moi(), Path(_etat["base"])).items():
            env.setdefault(k, v)
    return env


def _reglages(app_id, d):
    env = lire_env(d / "data" / "secrets.env")
    if app_id == "mouflopening":                     # Emby réglé dans config.json
        try:
            emby = json.loads((d / "config.json").read_text(encoding="utf-8")).get("emby") or {}
            env.setdefault("EMBY_URL", str(emby.get("host") or ""))
            key = str(emby.get("api_key") or "")
            if "your_emby" not in key:
                env.setdefault("EMBY_API_KEY", key)
        except (OSError, ValueError, AttributeError):
            pass
    if app_id == "mouflanimexer":                    # Telegram réglé dans telegram_config.json
        try:
            t = json.loads((d / "telegram_config.json").read_text(encoding="utf-8"))
            env.setdefault("TELEGRAM_BOT_TOKEN", str(t.get("bot_token") or ""))
            env.setdefault("TELEGRAM_CHAT_ID", str(t.get("chat_id") or ""))
        except (OSError, ValueError, AttributeError):
            pass
    return {k: env[k].strip() for k, _ in COMMUNS if env.get(k, "").strip()}


def apercu(nom_reglage, valeur):
    if not valeur:
        return ""
    return valeur if nom_reglage in LISIBLES else "…" + valeur[-4:]


# ---------------- appeler une voisine ----------------

def joindre(app_id):
    """(adresse, clé) pour appeler la voisine, ou None si elle n'est ni sur ce serveur ni réglée."""
    prefixe = app_id.upper()
    url, cle = os.getenv(f"{prefixe}_URL", "").strip().rstrip("/"), os.getenv(f"{prefixe}_CLE", "").strip()
    d = dossier(app_id)
    if d is not None:
        cle_locale = lire_env(d / "data" / "secrets.env").get(CLE, "")
        if cle_locale:
            port = os.getenv(f"{prefixe}_PORT", "").strip() or str(APPLIS[app_id][1])
            return f"http://127.0.0.1:{port}", cle_locale
    return (url, cle) if url and cle else None


def disponible(app_id):
    return joindre(app_id) is not None


def appeler(app_id, methode, chemin, timeout=15, **kw):
    """Appelle /api/voisin/… chez la voisine -> réponse JSON (dict). ErreurVoisin si elle ne répond pas ou refuse."""
    acces = joindre(app_id)
    if acces is None:
        raise ErreurVoisin(f"{nom(app_id)} n'est pas relié (ni sur ce serveur, ni réglé dans ⚙️ Réglages → Connexions)")
    url, cle = acces
    try:
        from src.netfix import repair_urllib3      # MouFlopening : yt-dlp abîme urllib3 (voir src/netfix.py)
        repair_urllib3()
    except Exception:
        pass
    try:
        r = requests.request(methode, url + chemin, headers={ENTETE: cle}, timeout=timeout, **kw)
    except requests.exceptions.ConnectionError:
        raise ErreurVoisin(f"{nom(app_id)} injoignable ({url})")
    except requests.exceptions.Timeout:
        raise ErreurVoisin(f"{nom(app_id)} ne répond pas (délai dépassé)")
    except requests.exceptions.RequestException as e:
        raise ErreurVoisin(f"Erreur de connexion à {nom(app_id)} : {type(e).__name__}")
    try:
        data = r.json()
    except ValueError:
        data = {}
    if r.status_code >= 400:
        raise ErreurVoisin(data.get("error") or f"{nom(app_id)} a répondu {r.status_code}")
    return data


# ---------------- être appelé par une voisine ----------------

def _bloquee(ip):
    now = time.time()
    with _verrou:
        essais = [t for t in _echecs.get(ip, []) if now - t < _FENETRE]
        _echecs[ip] = essais
        return len(essais) >= _MAX_ECHECS


def refus():
    """None si la requête porte la bonne clé, sinon la réponse de refus (toujours 403)."""
    ip = request.remote_addr or "?"
    attendue = os.getenv(CLE, "").strip()
    if _bloquee(ip):
        return jsonify({"ok": False, "error": "Trop d'essais avec une mauvaise clé : réessaie dans 10 minutes."}), 403
    donnee = (request.headers.get(ENTETE) or "").strip()
    if attendue and donnee and hmac.compare_digest(donnee.encode(), attendue.encode()):
        return None
    with _verrou:
        _echecs.setdefault(ip, []).append(time.time())
    return jsonify({"ok": False, "error": "Clé refusée"}), 403


def route(app, regle, **options):
    """@voisins.route(app, "/api/voisin/…") : adresse réservée aux applis voisines (clé obligatoire)."""
    def deco(f):
        @functools.wraps(f)
        def garde(*a, **k):
            r = refus()
            return r if r is not None else f(*a, **k)
        app.add_url_rule(regle, "voisin_" + f.__name__, garde, **options)
        return garde
    return deco


# ---------------- réglages ----------------

def preparer_cle():
    """Au lancement du vrai service seulement (jamais pendant un essai) : crée la clé si elle n'existe pas encore."""
    if os.getenv(CLE, "").strip() or not _etat["base"]:
        return
    cle = "mfv_" + _secrets.token_urlsafe(30).replace("-", "x").replace("_", "y")
    try:
        _etat["ecrire"](Path(_etat["base"]) / "data" / "secrets.env", CLE, cle)
        os.environ[CLE] = cle
        logger.info("Clé « applis voisines » créée")
    except OSError as e:
        logger.warning("Clé « applis voisines » impossible à enregistrer : %s", e)


def _valide(valeur):
    return bool(valeur) and not (_INTERDITS & set(valeur)) and len(valeur) < 500


def init_app(app, base_dir, ecrire_secret, apres_reprise=None):
    """ecrire_secret(fichier, nom, valeur) ; apres_reprise(noms) est appelé après « Reprendre d'une appli voisine »."""
    _etat.update(base=Path(base_dir), ecrire=ecrire_secret, apres_reprise=apres_reprise)
    fichier = Path(base_dir) / "data" / "secrets.env"

    @app.route("/api/settings/voisines")
    def voisines_etat():
        voisines, ici = [], mes_reglages()
        for app_id, (titre, _port) in APPLIS.items():
            if app_id == _moi():
                continue
            regl = reglages_de(app_id)
            voisines.append({"id": app_id, "nom": titre, "ici": dossier(app_id) is not None,
                             "reliee": disponible(app_id),
                             "url": os.getenv(f"{app_id.upper()}_URL", "").strip(),
                             "url_externe": os.getenv(f"{app_id.upper()}_URL_EXTERNE", "").strip(),
                             "cle_reglee": bool(os.getenv(f"{app_id.upper()}_CLE", "").strip()),
                             "reglages": [{"nom": k, "libelle": lib, "apercu": apercu(k, regl.get(k, "")),
                                           "ici": apercu(k, ici.get(k, "")), "pareil": regl.get(k) == ici.get(k)}
                                          for k, lib in COMMUNS if regl.get(k)]})
        cle = os.getenv(CLE, "").strip()
        return jsonify({"moi": nom(_moi()), "cle_fin": ("…" + cle[-4:]) if cle else "", "voisines": voisines})

    @app.route("/api/settings/voisines", methods=["POST"])
    def voisines_action():
        body = request.get_json(silent=True) or {}
        action = str(body.get("action") or "")
        if action == "reprendre":
            app_id = str(body.get("app") or "")
            regl = reglages_de(app_id)
            if not regl:
                return jsonify({"ok": False, "error": "Appli voisine introuvable sur ce serveur, ou rien à reprendre."}), 400
            voulus = [k for k in (body.get("noms") or []) if k in regl and _valide(regl[k])]
            if not voulus:
                return jsonify({"ok": False, "error": "Coche au moins un réglage à reprendre."}), 400
            for k in voulus:
                ecrire_secret(fichier, k, regl[k])
                os.environ[k] = regl[k]
            logger.info("Réglages repris de %s : %s", nom(app_id), ", ".join(voulus))
            if _etat["apres_reprise"]:
                try:
                    _etat["apres_reprise"](voulus)
                except Exception:
                    logger.exception("Mise à jour après reprise des réglages")
            return jsonify({"ok": True, "message": f"{len(voulus)} réglage(s) repris de {nom(app_id)}."})
        if action == "nouvelle_cle":                  # montrée une seule fois, comme la clé API de MouFlanga
            cle = "mfv_" + _secrets.token_urlsafe(30).replace("-", "x").replace("_", "y")
            ecrire_secret(fichier, CLE, cle)
            os.environ[CLE] = cle
            logger.info("Nouvelle clé « applis voisines » (l'ancienne ne marche plus)")
            return jsonify({"ok": True, "cle": cle, "message": "Nouvelle clé créée. Les voisines du même serveur la liront toutes seules ; "
                                                               "pour une voisine sur une autre machine, copie-la maintenant dans ses réglages "
                                                               "(elle ne sera plus affichée)."})
        app_id = str(body.get("app") or "")
        if action in ("distante", "adresses") and (app_id not in APPLIS or app_id == _moi()):
            return jsonify({"ok": False, "error": "Appli inconnue"}), 400
        prefixe = app_id.upper()
        if action == "distante":                      # voisine sur une autre machine : sa clé (son adresse : « adresses »)
            cle = str(body.get("cle") or "").strip()
            if cle and not _valide(cle):
                return jsonify({"ok": False, "error": "Clé invalide"}), 400
            ecrire_secret(fichier, f"{prefixe}_CLE", cle)
            os.environ[f"{prefixe}_CLE"] = cle
            return jsonify({"ok": True, "message": "Clé enregistrée." if cle else "Clé effacée."})
        if action == "adresses":                      # adresses de la voisine pour le navigateur : réseau local et perso
            adresses = {f"{prefixe}_URL": str(body.get("url") or "").strip().rstrip("/"),
                        f"{prefixe}_URL_EXTERNE": str(body.get("url_externe") or "").strip().rstrip("/")}
            for url in adresses.values():
                if url and (not url.startswith(("http://", "https://")) or not _valide(url) or " " in url):
                    return jsonify({"ok": False, "error": f"Adresse invalide : « {url} » (elle commence par http:// ou https://)"}), 400
            for k, url in adresses.items():
                ecrire_secret(fichier, k, url)
                os.environ[k] = url
            return jsonify({"ok": True, "message": "Adresses enregistrées." if any(adresses.values()) else "Adresses effacées."})
        return jsonify({"ok": False, "error": "Action inconnue"}), 400
