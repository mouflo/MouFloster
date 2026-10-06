"""
Alertes Telegram de MouFloster.

- Réglages : TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID dans data/secrets.env, sinon ceux de MouFlanimeXer
  (/opt/mouflanimexer/telegram_config.json, en lecture seule) ; TELEGRAM_THREAD_ID = sujet propre à MouFloster.
- Alertes (cases dans ⚙️ Réglages) : redémarrage après un arrêt inattendu (plantage ou coupure du serveur),
  erreur interne (au plus une toutes les 30 minutes pour la même erreur), lot de posters terminé, mise à jour installée.
- Le jeton n'apparaît jamais dans les journaux ni dans les réponses.
"""
import atexit
import json
import logging
import os
import re
import signal
import sys
import threading
import time
from pathlib import Path

import requests
from flask import got_request_exception, jsonify, request

logger = logging.getLogger(__name__)
APP = "MouFloster"
FALLBACK = Path("/opt/mouflanimexer/telegram_config.json")
_TOKEN_RE = re.compile(r"^\d{6,12}:[A-Za-z0-9_-]{30,50}$")
_CHAT_RE = re.compile(r"^-?\d{3,20}$")
_THREAD_RE = re.compile(r"^\d{1,10}$")
_LIEN = re.compile(r"t\.me/c/(\d{5,})/(\d+)(?:/(\d+))?")
OPTIONS = {  # nom -> (libellé, activée par défaut)
    "REDEMARRAGE": ("Redémarrage après un arrêt inattendu (plantage, coupure du serveur)", True),
    "ERREURS": ("Erreur interne pendant une action (au plus une alerte par demi-heure pour la même erreur)", True),
    "LOT": ("Lot de posters terminé (résumé)", True),
    "MAJ": ("Mise à jour installée (nouvelle version)", False),
}
WEBHOOK = ("Ce bot est déjà branché sur une autre application (par exemple Jeedom) : Telegram lui envoie directement "
           "les messages, l'appli ne peut donc pas les lire pour détecter quoi que ce soit. Utilise plutôt « Lien d'un message » "
           "(appui long sur un message du sujet → Copier le lien), ou crée un bot réservé à tes applis avec @BotFather.")
_dernieres_erreurs = {}


def config():
    """(jeton, discussion, origine) ; origine « ici » ou « MouFlanimeXer » ; jeton vide si rien n'est réglé."""
    tok, chat = os.getenv("TELEGRAM_BOT_TOKEN", "").strip(), os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if tok and chat:
        return tok, chat, "ici"
    try:
        d = json.loads(FALLBACK.read_text(encoding="utf-8"))
        if d.get("bot_token") and d.get("chat_id"):
            return str(d["bot_token"]), str(d["chat_id"]), "MouFlanimeXer"
    except (OSError, ValueError):
        pass
    return "", "", ""


def sujet():
    t = os.getenv("TELEGRAM_THREAD_ID", "").strip()
    return t if _THREAD_RE.match(t) else ""


def option(nom):
    v = os.getenv("NOTIF_" + nom, "").strip()
    return OPTIONS[nom][1] if v == "" else v == "1"


def envoyer(texte, token=None, chat=None, thread=None):
    """(réussi, message lisible). Ne lève jamais d'exception."""
    tok, ch, _ = config()
    token, chat = token or tok, chat or ch
    thread = sujet() if thread is None else thread
    if not token or not chat:
        return False, "Telegram n'est pas réglé"
    corps = {"chat_id": chat, "text": texte, "disable_web_page_preview": True}
    if thread and _THREAD_RE.match(str(thread)) and int(thread) != 1:   # 1 = sujet « Général » : rien à préciser
        corps["message_thread_id"] = int(thread)
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json=corps, timeout=15)
        if r.status_code == 200:
            return True, "Message envoyé."
        try:
            msg = r.json().get("description") or f"code {r.status_code}"
        except ValueError:
            msg = f"code {r.status_code}"
        return False, "Telegram refuse : " + msg
    except requests.exceptions.RequestException as e:
        return False, "Telegram injoignable : " + type(e).__name__


def alerter(nom, texte):
    """Alerte en arrière-plan si la case correspondante est cochée."""
    if not option(nom):
        return
    def go():
        ok, msg = envoyer(texte)
        logger.info("Alerte Telegram (%s) : %s", nom, "envoyée" if ok else msg)
    threading.Thread(target=go, daemon=True).start()


def lire_lien(lien):
    m = _LIEN.search(lien or "")
    return ("-100" + m.group(1), m.group(2) if m.group(3) else "") if m else None


# ---------------------------------------------------------------- Événements

def _dernieres_lignes_erreur(journal: Path, n=4):
    try:
        lignes = journal.read_text(encoding="utf-8", errors="ignore").splitlines()[-400:]
    except OSError:
        return ""
    err = [l for l in lignes if " ERROR " in l or "Traceback" in l or "Error:" in l]
    return "\n".join(l[-160:] for l in err[-n:])


def au_demarrage(base_dir, version, journal):
    """Repère un arrêt inattendu (le témoin « en marche » n'a pas été effacé) et une nouvelle version."""
    data = Path(base_dir) / "data"
    data.mkdir(parents=True, exist_ok=True)
    temoin, derniere = data / "en-marche", data / "derniere-version"
    if temoin.exists():
        try:
            demarrage_serveur = time.time() - float(Path("/proc/uptime").read_text().split()[0])
        except (OSError, ValueError, IndexError):
            demarrage_serveur = 0
        if demarrage_serveur > temoin.stat().st_mtime:
            texte = f"⚡ {APP} a redémarré : le serveur lui-même a redémarré (coupure de courant, mise à jour du système…)."
        else:
            texte = f"⚠️ {APP} s'était arrêté brutalement (plantage) et vient de redémarrer tout seul."
            erreurs = _dernieres_lignes_erreur(journal)
            if erreurs:
                texte += "\n\nDernières erreurs du journal :\n" + erreurs
        logger.warning("Arrêt inattendu détecté au démarrage")
        alerter("REDEMARRAGE", texte)
    try:
        ancienne = derniere.read_text().strip()
    except OSError:
        ancienne = ""
    if ancienne and ancienne != version:
        alerter("MAJ", f"🆕 {APP} mis à jour : {ancienne} → {version}")
    derniere.write_text(version)
    temoin.write_text(str(os.getpid()))

    def propre(*_):
        try:
            temoin.unlink()
        except OSError:
            pass
    atexit.register(propre)

    def sur_sigterm(signum, frame):        # arrêt normal par systemd (mise à jour, redémarrage demandé)
        propre()
        sys.exit(0)
    try:
        signal.signal(signal.SIGTERM, sur_sigterm)
    except ValueError:
        pass                               # pas dans le fil principal (tests)


def _sur_erreur(sender, exception, **extra):
    cle = f"{type(exception).__name__}:{request.path if request else ''}"
    if time.time() - _dernieres_erreurs.get(cle, 0) < 1800:
        return
    _dernieres_erreurs[cle] = time.time()
    alerter("ERREURS", f"❌ {APP} : erreur pendant « {request.path if request else '?'} »\n{type(exception).__name__} : {str(exception)[:300]}")


# ---------------------------------------------------------------- Routes

def init_app(app, base_dir, version, journal, ecrire_secret):
    """Routes des réglages et alerte d'erreur. (au_demarrage est appelé à part, seulement par le vrai service.)"""
    got_request_exception.connect(_sur_erreur, app)
    secrets = Path(base_dir) / "data" / "secrets.env"

    def enregistrer(nom, valeur):
        ecrire_secret(secrets, nom, valeur)
        os.environ[nom] = valeur

    @app.route("/api/settings/telegram")
    def telegram_etat():
        tok, chat, origine = config()
        return jsonify({"configured": bool(tok and chat), "chat_hint": ("…" + chat[-4:]) if chat else "",
                        "origine": origine, "thread_id": sujet(),
                        "options": [{"nom": n, "libelle": l, "actif": option(n)} for n, (l, _) in OPTIONS.items()]})

    @app.route("/api/settings/telegram", methods=["POST"])
    def telegram_reglage():
        body = request.get_json(silent=True) or {}
        action = body.get("action", "save")
        if action == "options":
            for nom in OPTIONS:
                if nom in (body.get("options") or {}):
                    enregistrer("NOTIF_" + nom, "1" if body["options"][nom] else "0")
            return jsonify({"ok": True, "message": "Choix des alertes enregistré."})
        if action == "lien":
            trouve = lire_lien(str(body.get("lien", "")))
            if not trouve:
                return jsonify({"ok": False, "error": "Ce lien ne ressemble pas à un lien de message de groupe (https://t.me/c/…). Dans le sujet, appui long sur un message → « Copier le lien »."}), 400
            return jsonify({"ok": True, "chat_id": trouve[0], "thread_id": trouve[1],
                            "message": "Groupe et sujet lus dans le lien : appuie sur « Enregistrer » (un message de test sera envoyé)."})
        tok_saisi = str(body.get("token", "")).strip()
        chat_saisi = str(body.get("chat_id", "")).strip()
        thread = str(body.get("thread_id", sujet())).strip()
        if action == "detect":
            tok = tok_saisi or config()[0]
            try:
                r = requests.post(f"https://api.telegram.org/bot{tok}/getUpdates", json={"limit": 50, "timeout": 0}, timeout=15)
            except requests.exceptions.RequestException as e:
                return jsonify({"ok": False, "error": "Telegram injoignable : " + type(e).__name__}), 400
            if r.status_code != 200:
                return jsonify({"ok": False, "error": WEBHOOK if "webhook" in r.text.lower() else "Telegram refuse ce jeton."}), 400
            for m in reversed(r.json().get("result", [])):
                msg = m.get("message") or {}
                c = msg.get("chat") or {}
                if c.get("type") == "supergroup" and msg.get("message_thread_id") and msg.get("is_topic_message"):
                    return jsonify({"ok": True, "chat_id": str(c["id"]), "thread_id": str(msg["message_thread_id"]),
                                    "message": "Groupe et sujet trouvés : appuie sur « Enregistrer »."})
            return jsonify({"ok": False, "error": "Aucun message de sujet reçu : écris « bonjour » dans le sujet de MouFloster, puis réessaie."}), 400
        if tok_saisi and not _TOKEN_RE.match(tok_saisi):
            return jsonify({"ok": False, "error": "Jeton invalide : il ressemble à 123456789:ABC… (donné par @BotFather), sans espace."}), 400
        if chat_saisi and not _CHAT_RE.match(chat_saisi):
            return jsonify({"ok": False, "error": "Identifiant invalide : un nombre (ex. 123456789, ou -100… pour un groupe)."}), 400
        if thread and not _THREAD_RE.match(thread):
            return jsonify({"ok": False, "error": "Le numéro du sujet est un nombre (ex. 4). Laisse vide si tu n'utilises pas de sujets."}), 400
        tok, chat, _ = config()
        ok, msg = envoyer(f"✅ {APP} : les alertes Telegram fonctionnent.", tok_saisi or tok, chat_saisi or chat, thread)
        if not ok:
            return jsonify({"ok": False, "error": msg + ("" if action == "test" else ". Rien n'a été enregistré.")}), 400
        if action == "test":
            return jsonify({"ok": True, "message": "Message de test envoyé : regarde Telegram."})
        if tok_saisi:
            enregistrer("TELEGRAM_BOT_TOKEN", tok_saisi)
        if chat_saisi:
            enregistrer("TELEGRAM_CHAT_ID", chat_saisi)
        enregistrer("TELEGRAM_THREAD_ID", thread)
        logger.info("Telegram réglé depuis la page web")
        return jsonify({"ok": True, "message": "Enregistré : un message de test vient d'arriver sur Telegram."})

    @app.route("/api/notif/lot", methods=["POST"])
    def notif_lot():
        b = request.get_json(silent=True) or {}
        n = lambda k: max(0, int(b.get(k) or 0))
        texte = f"📦 {APP} : lot de posters terminé — {n('envoyes')} envoyé(s) dans la médiathèque"
        if n("manuels"):
            texte += f", {n('manuels')} à envoyer à la main (dossier incertain)"
        if n("erreurs"):
            texte += f", {n('erreurs')} en erreur"
        alerter("LOT", texte + ".")
        return jsonify({"ok": True})
