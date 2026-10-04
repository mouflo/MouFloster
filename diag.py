"""
Journal et diagnostic intégrés à l'appli: une fenêtre « Journal » affiche un rapport complet (état du serveur,
état de LaMa, dernières lignes des journaux) à copier-coller directement, sans se connecter au serveur.

Les clés et mots de passe sont masqués automatiquement dans le rapport.
"""

import logging
import logging.handlers
import os
import platform
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

from flask import jsonify, request

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
LOG_FILE = DATA_DIR / "moufloster.log"
LAMA_LOG = DATA_DIR / "lama-install.log"

logger = logging.getLogger("moufloster.diag")

_SECRET_PATTERNS = [
    (re.compile(r"(api_key=)[^&\s\"']+", re.I), r"\1***"),
    (re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]+", re.I), r"\1***"),
    (re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"), "***jeton***"),
    (re.compile(r"(X-Emby-Token['\":= ]+)[A-Za-z0-9]+", re.I), r"\1***"),
    (re.compile(r"(APP_PASSWORD_HASH=)\S+"), r"\1***"),
    (re.compile(r"(\b(?:password|passwd|mot_de_passe)=)[^&\s]+", re.I), r"\1***"),
]


def redact(text: str) -> str:
    for pattern, repl in _SECRET_PATTERNS:
        text = pattern.sub(repl, text)
    return text


def setup_logging():
    """Journal détaillé dans la console ET dans data/moufloster.log (tourne tout seul: 1 Mo x 3 fichiers)"""
    fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in list(root.handlers):
        root.removeHandler(h)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError as e:
        root.warning(f"Journal fichier indisponible: {e}")
    logging.getLogger("werkzeug").setLevel(logging.WARNING)  # pas une ligne par requête


def _tail(path: Path, lines: int, max_bytes: int = 200_000) -> str:
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            f.seek(max(0, size - max_bytes))
            data = f.read().decode("utf-8", errors="replace")
        return "\n".join(data.splitlines()[-lines:])
    except OSError:
        return "(aucun journal pour le moment)"


def _mem():
    try:
        info = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, v = line.split(":", 1)
            info[k] = int(v.split()[0])
        return f"{info['MemAvailable'] // 1024} Mo libres sur {info['MemTotal'] // 1024} Mo"
    except Exception:
        return "inconnue"


def _disk(path):
    try:
        u = shutil.disk_usage(path)
        return f"{u.free / 1e9:.1f} Go libres sur {u.total / 1e9:.0f} Go"
    except OSError as e:
        return f"inaccessible ({e})"


def build_report(version, output_base, library_root, tmdb_ok, emby_ok):
    import lama

    ok, reason = lama.check(force=True)
    markers = {n: (DATA_DIR / n).exists() for n in (".lama-ok", ".lama-failed", ".lama-disabled")}
    tries = ""
    try:
        tries = (DATA_DIR / ".lama-tries").read_text().strip()
    except OSError:
        pass
    model_size = ""
    try:
        model_size = f"{lama.MODEL_PATH.stat().st_size / 1048576:.0f} Mo"
    except OSError:
        model_size = "absent"
    try:
        import torch
        torch_info = f"torch {torch.__version__}"
    except Exception as e:
        torch_info = f"torch NON utilisable → {type(e).__name__}: {e}"
    try:
        import cv2
        cv_info = f"opencv {cv2.__version__}"
    except Exception as e:
        cv_info = f"opencv NON utilisable → {type(e).__name__}: {e}"

    lines = [
        f"=== Rapport Moufloster · {datetime.now().strftime('%d/%m/%Y %H:%M:%S')} ===",
        f"Version : {version}",
        f"Python  : {sys.version.split()[0]} · {platform.platform()} · {os.cpu_count()} cœurs",
        f"Mémoire : {_mem()}",
        f"Disque  : appli {_disk(BASE_DIR)} · posters {_disk(output_base)}",
        f"Outils  : {cv_info} · {torch_info}",
        "",
        "--- Effacement (LaMa) ---",
        f"Moteur utilisé actuellement : {'LaMa' if ok else 'OpenCV (moteur léger)'}",
        f"État : {reason}",
        f"Modèle : {lama.MODEL_PATH} → {model_size}",
        f"Marqueurs d'installation : " + ", ".join(f"{k}={'oui' if v else 'non'}" for k, v in markers.items())
        + (f" · essais={tries}" if tries else ""),
        "",
        "--- Réglages ---",
        f"Clé TMDB : {'configurée' if tmdb_ok else 'MANQUANTE'} · Emby : {'configuré' if emby_ok else 'non configuré'}",
        f"Médiathèque : {library_root} → {'accessible' if Path(library_root).is_dir() else 'INTROUVABLE'}"
        + (f" · écriture {'OK' if os.access(library_root, os.W_OK) else 'IMPOSSIBLE'}" if Path(library_root).is_dir() else ""),
        "",
        "--- Installation de LaMa (data/lama-install.log, 60 dernières lignes) ---",
        _tail(LAMA_LOG, 60),
        "",
        "--- Journal de l'appli (150 dernières lignes) ---",
        _tail(LOG_FILE, 150),
    ]
    return redact("\n".join(lines))


def init_app(app, version, output_base, library_root, tmdb_ok_fn, emby_ok_fn):
    logger.info(f"Démarrage Moufloster {version} · Python {sys.version.split()[0]}")

    @app.route("/api/diagnostic")
    def api_diagnostic():
        report = build_report(version, output_base, library_root, tmdb_ok_fn(), emby_ok_fn())
        return jsonify({"report": report})

    @app.route("/api/lama/retry", methods=["POST"])
    def api_lama_retry():
        """Remet à zéro l'installation de LaMa: la mise à jour automatique (chaque minute) la relance"""
        removed = []
        for name in (".lama-ok", ".lama-failed", ".lama-disabled", ".lama-tries"):
            try:
                (DATA_DIR / name).unlink()
                removed.append(name)
            except FileNotFoundError:
                pass
            except OSError as e:
                logger.error(f"Impossible de supprimer {name}: {e}")
        logger.info(f"Relance de l'installation de LaMa demandée (marqueurs retirés: {removed})")
        return jsonify({"message": "Installation relancée : elle démarre dans la minute qui vient. Clique sur « Rafraîchir » ensuite pour suivre."})

    @app.route("/api/clientlog", methods=["POST"])
    def api_clientlog():
        """Erreurs JavaScript du navigateur, pour qu'elles apparaissent aussi dans le journal"""
        data = request.get_json(silent=True) or {}
        msg = str(data.get("message", ""))[:400]
        where = str(data.get("where", ""))[:200]
        logger.error(f"[navigateur] {msg} ({where})")
        return jsonify({"ok": True})

    @app.errorhandler(Exception)
    def on_error(exc):
        from werkzeug.exceptions import HTTPException
        if isinstance(exc, HTTPException):
            return exc
        logger.exception(f"Erreur non gérée sur {request.method} {request.path}")
        if request.path.startswith("/api/"):
            return jsonify({"error": f"Erreur interne: {type(exc).__name__}: {exc} (détails dans le Journal)"}), 500
        raise exc
