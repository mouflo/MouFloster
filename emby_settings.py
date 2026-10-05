"""
Réglage de la clé API Emby depuis la page web (évite les copier-coller en ligne de commande).

- La clé est enregistrée dans data/secrets.env (jamais sur GitHub), droits 600
- « Tester » vérifie la clé auprès d'Emby avant de l'enregistrer
- La clé n'est jamais renvoyée à la page (seulement ses 4 derniers caractères)
- Les routes /api/... sont protégées par la connexion de l'appli
"""

import logging
import os
import re
from pathlib import Path

import requests
from flask import jsonify, request

logger = logging.getLogger(__name__)
_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")


def _write_secret(file: Path, name: str, value: str) -> None:
    file.parent.mkdir(parents=True, exist_ok=True)
    lines = file.read_text(encoding="utf-8").splitlines() if file.exists() else []
    lines = [l for l in lines if not l.startswith(name + "=")]
    lines.append(f'{name}="{value}"')
    tmp = file.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, file)


def _check(host: str, key: str):
    """(ok, message) : la clé est-elle acceptée par Emby ?"""
    try:
        r = requests.get(host.rstrip("/") + "/System/Info", headers={"X-Emby-Token": key}, timeout=8)
    except requests.exceptions.ConnectionError:
        return False, f"Emby injoignable ({host})"
    except requests.exceptions.Timeout:
        return False, "Emby ne répond pas (délai dépassé)"
    except requests.exceptions.RequestException as e:
        return False, f"Erreur de connexion à Emby : {e}"
    if r.status_code in (401, 403):
        return False, "Emby refuse cette clé"
    if r.status_code >= 400:
        return False, f"Emby a répondu {r.status_code}"
    try:
        name = r.json().get("ServerName") or "Emby"
    except ValueError:
        name = "Emby"
    return True, f"Clé acceptée par « {name} »"


def init_app(app, base_dir, host_fn, on_change=None):
    """host_fn() -> adresse d'Emby ; on_change(clé) est appelé après un enregistrement."""
    secrets_file = Path(base_dir) / "data" / "secrets.env"

    @app.route("/api/settings/emby")
    def emby_state():
        key = os.getenv("EMBY_API_KEY", "").strip()
        return jsonify({"host": host_fn(), "configured": bool(key and "your_emby" not in key),
                        "hint": ("…" + key[-4:]) if len(key) >= 8 else ""})

    @app.route("/api/settings/emby", methods=["POST"])
    def emby_save():
        body = request.get_json(silent=True) or {}
        key = str(body.get("api_key", "")).strip()
        if not _KEY_RE.match(key):
            return jsonify({"ok": False, "error": "Clé invalide : lettres et chiffres uniquement (colle-la sans espace ni guillemets)."}), 400
        ok, msg = _check(host_fn(), key)
        if body.get("test"):
            return jsonify({"ok": ok, "message": msg}), 200
        if not ok and not body.get("force"):
            return jsonify({"ok": False, "error": msg + ". Rien n'a été enregistré."}), 400
        _write_secret(secrets_file, "EMBY_API_KEY", key)
        os.environ["EMBY_API_KEY"] = key
        if on_change:
            on_change(key)
        logger.info("Clé API Emby mise à jour depuis la page web")
        return jsonify({"ok": True, "message": "Clé enregistrée. " + msg})
