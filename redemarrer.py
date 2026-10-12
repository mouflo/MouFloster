"""Bouton « 🔄 Redémarrer l'appli » de la page ⚙️ Réglages (fichier identique dans les quatre applis).

L'appli s'envoie un arrêt normal (SIGTERM) : le témoin des alertes « redémarrage inattendu » est effacé comme pour un
arrêt voulu, puis systemd la relance (Restart=always). Le serveur, lui, ne redémarre pas.
"""
import logging
import os
import signal
import threading
import time

from flask import jsonify, request

logger = logging.getLogger(__name__)


def init_app(app, occupe=None):
    """occupe() → liste des travaux longs en cours (vide si rien) : on prévient avant de les couper."""

    @app.route("/api/redemarrer", methods=["POST"])
    def api_redemarrer():
        body = request.get_json(silent=True) or {}
        raisons = []
        if occupe:
            try:
                raisons = list(occupe() or [])
            except Exception as e:
                logger.warning("Redémarrage : état des travaux inconnu (%s)", e)
        if raisons and not body.get("forcer"):
            return jsonify({"ok": False, "occupe": raisons,
                            "error": "Travail en cours : " + ", ".join(raisons) + ". Le redémarrage l'interromprait."}), 409
        logger.info("Redémarrage demandé depuis la page Réglages%s", " (travail en cours interrompu)" if raisons else "")

        def arreter():
            time.sleep(1.5)                     # la réponse part avant l'arrêt
            try:
                os.kill(os.getpid(), signal.SIGTERM)
            except OSError:
                pass
            time.sleep(10)
            os._exit(0)                         # si l'arrêt normal n'a pas suffi
        threading.Thread(target=arreter, daemon=True).start()
        return jsonify({"ok": True, "message": "L'appli redémarre… la page se recharge toute seule."})
