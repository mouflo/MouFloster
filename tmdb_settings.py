"""
Réglage de la clé API TheMovieDB depuis la page web (même principe que la clé Emby).

- Accepte la clé « API » (32 caractères) ou le « jeton d'accès » (commence par eyJ)
- La clé est enregistrée dans data/secrets.env (jamais sur GitHub), droits 600
- « Tester » vérifie la clé auprès de TheMovieDB avant de l'enregistrer
- La clé n'est jamais renvoyée à la page (seulement ses 4 derniers caractères)
"""

import logging
import os
import re
from pathlib import Path

import requests
from flask import jsonify, request

from emby_settings import _write_secret

logger = logging.getLogger(__name__)
_KEY_RE = re.compile(r"^(?:[A-Fa-f0-9]{32}|eyJ[A-Za-z0-9._-]{20,1000})$")


def _check(key: str):
    params, headers = {}, {}
    if key.startswith("eyJ"):
        headers["Authorization"] = f"Bearer {key}"
    else:
        params["api_key"] = key
    try:
        r = requests.get("https://api.themoviedb.org/3/configuration", params=params, headers=headers, timeout=8)
    except requests.exceptions.RequestException as e:
        return False, f"TheMovieDB injoignable : {type(e).__name__}"
    if r.status_code in (401, 403):
        return False, "TheMovieDB refuse cette clé"
    if r.status_code >= 400:
        return False, f"TheMovieDB a répondu {r.status_code}"
    return True, "Clé acceptée par TheMovieDB"


def init_app(app, base_dir, on_change=None):
    """on_change(clé) est appelé après un enregistrement."""
    secrets_file = Path(base_dir) / "data" / "secrets.env"

    @app.route("/api/settings/tmdb")
    def tmdb_state():
        key = os.getenv("TMDB_API_KEY", "").strip()
        ok = bool(key and "TON_CLE" not in key)
        return jsonify({"configured": ok, "hint": ("…" + key[-4:]) if ok and len(key) >= 8 else ""})

    @app.route("/api/settings/tmdb", methods=["POST"])
    def tmdb_save():
        body = request.get_json(silent=True) or {}
        key = str(body.get("api_key", "")).strip()
        if not _KEY_RE.match(key):
            return jsonify({"ok": False, "error": "Clé invalide : colle la clé API (32 caractères) ou le jeton d'accès (commence par eyJ), sans espace ni guillemets."}), 400
        ok, msg = _check(key)
        if body.get("test"):
            return jsonify({"ok": ok, "message": msg}), 200
        if not ok:
            return jsonify({"ok": False, "error": msg + ". Rien n'a été enregistré."}), 400
        _write_secret(secrets_file, "TMDB_API_KEY", key)
        os.environ["TMDB_API_KEY"] = key
        if on_change:
            on_change(key)
        logger.info("Clé API TheMovieDB mise à jour depuis la page web")
        return jsonify({"ok": True, "message": "Clé enregistrée. " + msg})
