"""
Compression gzip des réponses texte (pages, listes JSON, scripts, styles) : 5 à 10 fois moins de données à faire passer,
ce qui compte au téléphone par le VPN (la liste de MouFlopening passe de 546 Ko à 49 Ko).
Fichier identique dans les quatre applis (MouFlanga, MouFlanimeXer, MouFloster, MouFlopening).

Jamais pour les images, sons, vidéos, archives (déjà compressés), les fichiers envoyés tels quels (send_file),
les réponses en flux ou partielles (lecture audio), ni quand le navigateur ne l'accepte pas.
"""
import gzip

from flask import request

TYPES = ("text/", "application/json", "application/javascript", "application/xml", "image/svg+xml",
         "application/manifest+json")
MINIMUM = 1024           # en dessous, la compression ne fait rien gagner


def _compresser(reponse):
    try:
        if (request.method == "HEAD" or reponse.status_code != 200 or reponse.direct_passthrough or reponse.is_streamed
                or "Content-Encoding" in reponse.headers or "gzip" not in (request.headers.get("Accept-Encoding") or "").lower()
                or not (reponse.mimetype or "").startswith(TYPES)):
            return reponse
        donnees = reponse.get_data()
        if len(donnees) < MINIMUM:
            return reponse
        reponse.set_data(gzip.compress(donnees, compresslevel=5))
        reponse.headers["Content-Encoding"] = "gzip"
        reponse.vary.add("Accept-Encoding")
        if reponse.headers.get("ETag"):           # l'empreinte du contenu non compressé ne vaut plus pour celui-ci
            reponse.headers["ETag"] = reponse.headers["ETag"].rstrip('"') + '-gz"'
    except Exception:                             # jamais bloquant : la réponse part telle quelle
        pass
    return reponse


def init_app(app):
    # Placée en premier dans la liste : Flask appelle ces fonctions dans l'ordre inverse, la compression passe donc en
    # dernier, après tout ce qui pourrait encore modifier la réponse.
    app.after_request_funcs.setdefault(None, []).insert(0, _compresser)
