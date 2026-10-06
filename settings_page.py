"""
Page « ⚙️ Réglages » de MouFloster : adresse d'Emby, clés (Emby, TheMovieDB) et dossiers.
(Les clés ont leurs routes dans emby_settings.py et tmdb_settings.py ; l'explorateur de dossiers est dans fs_browser.py.)
- Adresse d'Emby, dossiers : enregistrés dans data/secrets.env (jamais sur GitHub)
- Les dossiers sont lus au démarrage : l'appli redémarre toute seule (systemd) après l'enregistrement
"""
import logging
import os
import re
import threading
import time
from pathlib import Path

from flask import jsonify, render_template, request

from emby_settings import _write_secret

logger = logging.getLogger(__name__)
_URL_RE = re.compile(r"^https?://[^\s/]+(:\d{1,5})?(/\S*)?$")
_BAD_CHARS = set('"$`\\\n\r')     # interdits : ils casseraient le fichier data/secrets.env


def init_app(app, base_dir, version_fn, get_dirs):
    """get_dirs() -> {"library": dossier de la médiathèque, "output": dossier des posters}"""
    base_dir = Path(base_dir)
    secrets = base_dir / "data" / "secrets.env"
    import fs_browser
    fs_browser.init_app(app)
    import emby

    @app.route("/reglages")
    def settings_page():
        return render_template("reglages.html", version=version_fn())

    @app.route("/api/settings/paths")
    def paths_state():
        d = get_dirs()
        return jsonify({"emby_host": emby.base_url(), "library": d["library"], "output": d["output"],
                        "library_ok": Path(d["library"]).is_dir(), "output_ok": Path(d["output"]).is_dir()})

    @app.route("/api/settings/emby-host", methods=["POST"])
    def emby_host_save():
        host = str((request.get_json(silent=True) or {}).get("host", "")).strip().rstrip("/")
        if not _URL_RE.match(host) or _BAD_CHARS & set(host):
            return jsonify({"ok": False, "error": "Adresse invalide : elle doit ressembler à http://192.168.1.134:8096"}), 400
        _write_secret(secrets, "EMBY_URL", host)
        os.environ["EMBY_URL"] = host
        logger.info("Adresse d'Emby mise à jour depuis la page web : %s", host)
        return jsonify({"ok": True, "message": "Adresse enregistrée."})

    @app.route("/api/settings/paths", methods=["POST"])
    def paths_save():
        body = request.get_json(silent=True) or {}
        library, output = str(body.get("library", "")).strip().rstrip("/"), str(body.get("output", "")).strip().rstrip("/")
        for value in (library, output):
            if not value.startswith("/") or _BAD_CHARS & set(value):
                return jsonify({"ok": False, "error": f"Chemin invalide : « {value} » (chemin complet commençant par /, sans guillemets ni $)."}), 400
        if not library or not Path(library).is_dir():
            return jsonify({"ok": False, "error": f"Dossier de la médiathèque introuvable sur le serveur : « {library} ». Rien n'a été enregistré."}), 400
        if not output or not (Path(output).is_dir() or Path(output).parent.is_dir()):
            return jsonify({"ok": False, "error": f"Dossier des posters inaccessible : « {output} » (le partage est-il monté ?). Rien n'a été enregistré."}), 400
        probe = Path(output) if Path(output).is_dir() else Path(output).parent
        if not os.access(probe, os.W_OK):
            return jsonify({"ok": False, "error": f"Impossible d'écrire dans « {probe} » (droits ? partage en lecture seule ?). Rien n'a été enregistré."}), 400
        _write_secret(secrets, "MEDIATHEQUE_DIR", library)
        _write_secret(secrets, "OUTPUT_DIR", output)
        logger.info("Dossiers mis à jour depuis la page web : médiathèque %s · posters %s", library, output)
        threading.Thread(target=lambda: (time.sleep(1.5), os._exit(0)), daemon=True).start()   # systemd relance l'appli
        return jsonify({"ok": True, "message": "Enregistré. L'appli redémarre pour relire les dossiers…"})

    # ------------------------------------------------------------------
    # MouFlanga : adresse + clé API (générée dans MouFlanga → ⚙️ Réglages)
    # ------------------------------------------------------------------
    import mouflanga_link

    @app.route("/api/settings/mouflanga")
    def mouflanga_state():
        cle = os.getenv("MOUFLANGA_CLE", "").strip()
        return jsonify({"url": os.getenv("MOUFLANGA_URL", "").strip(), "configured": bool(cle),
                        "hint": ("…" + cle[-4:]) if len(cle) >= 8 else "", "mode": mouflanga_link.mode(),
                        "local": mouflanga_link.installe()})

    @app.route("/api/settings/mouflanga", methods=["POST"])
    def mouflanga_save():
        body = request.get_json(silent=True) or {}
        url = str(body.get("url", "")).strip().rstrip("/")
        cle = str(body.get("api_key", "")).strip() or os.getenv("MOUFLANGA_CLE", "").strip()
        if body.get("effacer"):
            for nom in ("MOUFLANGA_URL", "MOUFLANGA_CLE"):
                _write_secret(secrets, nom, "")
                os.environ[nom] = ""
            return jsonify({"ok": True, "message": "Connexion à MouFlanga effacée."})
        if not _URL_RE.match(url) or _BAD_CHARS & set(url):
            return jsonify({"ok": False, "error": "Adresse invalide : elle doit ressembler à http://192.168.1.141:5002"}), 400
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", cle):
            return jsonify({"ok": False, "error": "Colle la clé API générée dans MouFlanga → ⚙️ Réglages (sans espace)."}), 400
        try:
            msg = mouflanga_link.tester(url, cle)
        except mouflanga_link.ErreurMouflanga as e:
            return jsonify({"ok": False, "error": str(e) + ("" if body.get("test") else " Rien n'a été enregistré.")}), 400
        if body.get("test"):
            return jsonify({"ok": True, "message": msg})
        _write_secret(secrets, "MOUFLANGA_URL", url)
        _write_secret(secrets, "MOUFLANGA_CLE", cle)
        os.environ["MOUFLANGA_URL"], os.environ["MOUFLANGA_CLE"] = url, cle
        logger.info("Connexion à MouFlanga enregistrée : %s", url)
        return jsonify({"ok": True, "message": "Enregistré. " + msg})

    # ------------------------------------------------------------------
    # MouFlopening (génériques) : adresse locale et adresse perso
    # ------------------------------------------------------------------
    @app.route("/api/settings/mouflopening", methods=["GET", "POST"])
    def mouflopening_adresses():
        if request.method == "POST":
            body = request.get_json(silent=True) or {}
            adresses = {"MOUFLOPENING_URL": str(body.get("url", "")).strip().rstrip("/"),
                        "MOUFLOPENING_URL_EXTERNE": str(body.get("url_externe", "")).strip().rstrip("/")}
            for url in adresses.values():
                if url and (not _URL_RE.match(url) or _BAD_CHARS & set(url)):
                    return jsonify({"ok": False, "error": f"Adresse invalide : « {url} » (elle commence par http:// ou https://)"}), 400
            for nom, url in adresses.items():
                _write_secret(secrets, nom, url)
                os.environ[nom] = url
            return jsonify({"ok": True, "message": "Adresses enregistrées." if any(adresses.values()) else "Adresses effacées : la proposition est désactivée."})
        return jsonify({"url": os.getenv("MOUFLOPENING_URL", "").strip(), "url_externe": os.getenv("MOUFLOPENING_URL_EXTERNE", "").strip()})
