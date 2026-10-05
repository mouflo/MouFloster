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
        if not _URL_RE.match(host):
            return jsonify({"ok": False, "error": "Adresse invalide : elle doit ressembler à http://192.168.1.134:8096"}), 400
        _write_secret(secrets, "EMBY_URL", host)
        os.environ["EMBY_URL"] = host
        logger.info("Adresse d'Emby mise à jour depuis la page web : %s", host)
        return jsonify({"ok": True, "message": "Adresse enregistrée."})

    @app.route("/api/settings/paths", methods=["POST"])
    def paths_save():
        body = request.get_json(silent=True) or {}
        library, output = str(body.get("library", "")).strip(), str(body.get("output", "")).strip()
        if not library or not Path(library).is_dir():
            return jsonify({"ok": False, "error": f"Dossier de la médiathèque introuvable sur le serveur : « {library} ». Rien n'a été enregistré."}), 400
        if not output or not (Path(output).is_dir() or Path(output).parent.is_dir()):
            return jsonify({"ok": False, "error": f"Dossier des posters inaccessible : « {output} » (le partage est-il monté ?). Rien n'a été enregistré."}), 400
        _write_secret(secrets, "MEDIATHEQUE_DIR", library)
        _write_secret(secrets, "OUTPUT_DIR", output)
        logger.info("Dossiers mis à jour depuis la page web : médiathèque %s · posters %s", library, output)
        threading.Thread(target=lambda: (time.sleep(1.5), os._exit(0)), daemon=True).start()   # systemd relance l'appli
        return jsonify({"ok": True, "message": "Enregistré. L'appli redémarre pour relire les dossiers…"})
