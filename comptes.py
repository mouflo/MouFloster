"""
Comptes « copain » (fichier identique dans MouFloster et MouFlopening) : l'admin les crée dans ⚙️ Réglages → Comptes ;
chaque copain peut enregistrer l'adresse et la clé de SON Emby (jamais réaffichée). Les adresses du réseau local
(et les domaines de l'admin) sont refusées : un copain ne peut pas faire parler l'appli aux machines de la maison.
"""
import ipaddress
import json
import logging
import os
import re
import socket
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from flask import jsonify, request

logger = logging.getLogger(__name__)
_ID = re.compile(r"[A-Za-z0-9._@+-]{2,64}")
_ETAT = {"dossier": Path("copains")}


def profil(uid):
    try:
        return json.loads((_ETAT["dossier"] / f"{uid}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _ecrire_profil(uid, d):
    _ETAT["dossier"].mkdir(parents=True, exist_ok=True)
    f = _ETAT["dossier"] / f"{uid}.json"
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, f)


def _domaines_admin():
    """Domaines de l'admin (ex. mouflo.ovh, tiré des adresses de ses applis) : interdits aux copains."""
    out = set()
    for k, v in os.environ.items():
        if k.endswith(("_URL", "_URL_EXTERNE", "APP_URL")) and v.startswith("http"):
            h = (urlparse(v).hostname or "").lower()
            if h and not re.fullmatch(r"[\d.]+", h):
                out.add(".".join(h.split(".")[-2:]))
    return out


def adresse_permise(url):
    """(bonne, message) : http(s), et l'hôte ne mène ni au réseau local ni chez l'admin."""
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        return False, "L'adresse commence par http:// ou https://"
    h = u.hostname.lower()
    if any(h == d or h.endswith("." + d) for d in _domaines_admin()):
        return False, "Cette adresse n'est pas permise."
    try:
        ips = {ai[4][0] for ai in socket.getaddrinfo(h, u.port or (443 if u.scheme == "https" else 80))}
    except OSError:
        return False, "Adresse introuvable (vérifie l'orthographe)."
    for ip in ips:
        a = ipaddress.ip_address(ip.split("%")[0])
        if a.is_private or a.is_loopback or a.is_link_local or a.is_reserved or a.is_multicast or a.is_unspecified:
            return False, "Mets l'adresse externe de ton Emby (pas une adresse de réseau local)."
    return True, ""


def init_app(app, auth, data_dir, nom_role="copain"):
    _ETAT["dossier"] = Path(data_dir) / "copains"

    @app.route("/api/utilisateurs", methods=["GET", "POST"])
    def comptes_admin():
        from werkzeug.security import generate_password_hash
        if auth.role() != "admin":
            return jsonify({"error": "Réservé à l'admin"}), 403
        if request.method == "GET":
            d = auth.lire_utilisateurs()
            return jsonify({"utilisateurs": [{"id": k, "actif": v.get("actif", True), "cree": v.get("cree", ""),
                                              "emby": bool(profil(k).get("emby_url"))} for k, v in sorted(d.items())]})
        body = request.get_json(silent=True) or {}
        action, uid, mdp = str(body.get("action", "")), str(body.get("id", "")).strip(), str(body.get("mdp", ""))
        d = auth.lire_utilisateurs()
        if action == "ajouter":
            if not _ID.fullmatch(uid):
                return jsonify({"ok": False, "error": "Identifiant : 2 à 64 caractères (lettres, chiffres, . _ @ + -)."}), 400
            if uid in d or uid == os.getenv("APP_USER", ""):
                return jsonify({"ok": False, "error": f"L'identifiant « {uid} » est déjà pris."}), 409
        elif uid not in d:
            return jsonify({"ok": False, "error": "Compte introuvable"}), 404
        if action in ("ajouter", "motdepasse"):
            if len(mdp) < 8:
                return jsonify({"ok": False, "error": "Mot de passe trop court (8 caractères minimum)."}), 400
            d.setdefault(uid, {"actif": True, "cree": datetime.now().strftime("%Y-%m-%d")})["hash"] = generate_password_hash(mdp)
            message = f"Compte « {uid} » créé." if action == "ajouter" else f"Mot de passe de « {uid} » changé."
        elif action == "activer":
            d[uid]["actif"] = bool(body.get("actif"))
            message = f"Compte « {uid} » " + ("réactivé." if d[uid]["actif"] else "désactivé.")
        elif action == "supprimer":
            d.pop(uid)
            message = f"Compte « {uid} » supprimé."
        else:
            return jsonify({"ok": False, "error": "Action inconnue"}), 400
        auth.ecrire_utilisateurs(d)
        logger.info("Comptes %s : %s « %s »", nom_role, action, uid)
        return jsonify({"ok": True, "message": message})

    @app.route("/api/moi")
    def moi():
        r = auth.role()
        rep = {"role": r, "id": auth.utilisateur()}
        if r == nom_role:
            p = profil(auth.utilisateur())
            rep.update(emby_url=p.get("emby_url", ""), emby_cle=bool(p.get("emby_cle")))
        return jsonify(rep)

    @app.route("/api/copain/profil", methods=["POST"])
    def copain_profil():
        uid = auth.utilisateur()
        if auth.role() != nom_role:
            return jsonify({"ok": False, "error": "Réservé aux comptes copain"}), 403
        body = request.get_json(silent=True) or {}
        p = profil(uid)
        url = str(body.get("emby_url", "")).strip().rstrip("/")
        if url:
            ok, msg = adresse_permise(url)
            if not ok:
                return jsonify({"ok": False, "error": msg}), 400
        p["emby_url"] = url
        if str(body.get("emby_cle", "")).strip():
            p["emby_cle"] = str(body["emby_cle"]).strip()
        if not url:
            p.pop("emby_cle", None)
        _ecrire_profil(uid, p)
        return jsonify({"ok": True, "message": "Ton Emby est relié." if url else "Emby retiré : tu gardes le bouton Télécharger."})
