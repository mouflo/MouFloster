"""Explorateur de dossiers du serveur pour la page ⚙️ Réglages (fichier identique dans les trois applis).
Ne montre que des noms de dossiers et ne modifie rien."""
from pathlib import Path

from flask import jsonify, request

HIDDEN_PREFIXES = (".", "@", "#")
HIDDEN_NAMES = {"lost+found", "$RECYCLE.BIN", "System Volume Information", "eaDir", "@eaDir"}


def init_app(app):
    @app.route("/api/fs/list")
    def fs_list():
        raw = request.args.get("path", "") or "/mnt"
        try:
            path = Path(raw).resolve()
        except (OSError, RuntimeError):
            return jsonify({"error": "Chemin invalide"}), 400
        if not path.is_absolute() or not path.is_dir():
            path = Path("/")
        dirs = []
        try:
            for sub in sorted(path.iterdir(), key=lambda f: f.name.lower()):
                if sub.name.startswith(HIDDEN_PREFIXES) or sub.name in HIDDEN_NAMES:
                    continue
                try:
                    if sub.is_dir():
                        dirs.append(sub.name)
                except OSError:
                    pass
        except PermissionError:
            return jsonify({"path": str(path), "parent": str(path.parent) if path != path.parent else "", "dirs": [], "note": "Accès refusé à ce dossier"})
        except OSError as e:
            return jsonify({"error": f"Dossier illisible : {e.__class__.__name__}"}), 400
        return jsonify({"path": str(path), "parent": str(path.parent) if path != path.parent else "", "dirs": dirs})
