"""
🆕 Nouveautés : films, séries et saisons ajoutés dans Emby qui n'ont pas encore de poster fait avec MouFloster.

- « Fait » : chaque envoi vers la médiathèque est noté (dossier, et numéro de saison pour un poster de saison).
  Au premier passage, les envois déjà faits sont relus dans le journal de l'appli ; les titres de data/library_map.json
  comptent aussi comme faits pour le poster principal.
- « Remonter à » : depuis la mise en route de la liste (date notée au premier passage), 1 mois, 3 mois, 6 mois, 1 an
  ou tout. Le choix par défaut se règle dans ⚙️ Réglages.
- « ✓ Déjà bon » : masque un élément sans faire de poster (même liste que les posters faits).
"""

import json
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import emby
import library

FICHIER = Path(__file__).parent / "data" / "nouveautes.json"
_verrou = threading.Lock()

CHOIX = {"debut": None, "30": 30, "90": 90, "180": 180, "365": 365, "tout": None}
LIBELLES = {"debut": "Depuis la mise en route", "30": "1 mois", "90": "3 mois", "180": "6 mois", "365": "1 an", "tout": "Tout"}


def _lire():
    try:
        with open(FICHIER, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _ecrire(data):
    FICHIER.parent.mkdir(parents=True, exist_ok=True)
    tmp = FICHIER.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, FICHIER)


_LIGNE_ENVOI = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d).*📁 Médiathèque: (.+?) \(action=")


def _historique():
    """Posters déjà envoyés avant la mise en route de la liste, relus dans le journal de l'appli (data/moufloster.log*)."""
    faits = {}
    for f in sorted(FICHIER.parent.glob("moufloster.log*")):
        try:
            lignes = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for ligne in lignes:
            m = _LIGNE_ENVOI.match(ligne)
            if not m:
                continue
            parties = m.group(2).split("/")
            if len(parties) != 3:                 # image d'épisode (dans un sous-dossier) : pas un poster de titre
                continue
            rel, nom = "/".join(parties[:2]), parties[2]
            if re.match(r"^poster(-new.*)?\.jpg$", nom):
                faits[cle(rel)] = m.group(1)
            elif nom.startswith("season-specials-poster"):
                faits[cle(rel, 0)] = m.group(1)
            else:
                s = re.match(r"^season(\d+)-poster", nom)
                if s:
                    faits[cle(rel, int(s.group(1)))] = m.group(1)
    return faits


def _etat():
    """État complet ; la date de mise en route est notée au premier appel (et l'historique des envois repris)."""
    with _verrou:
        data = _lire()
        if not data.get("debut"):
            data["debut"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            data["faits"] = {**_historique(), **(data.get("faits") or {})}
            try:
                _ecrire(data)
            except OSError:
                pass
        return data


def cle(rel, saison=None):
    return rel if saison is None else f"{rel}#s{int(saison)}"


def marquer(rel, saison=None):
    """Un poster a été posé (ou l'élément est déclaré « déjà bon ») : il ne sera plus proposé."""
    if not rel:
        return
    _etat()
    with _verrou:
        data = _lire()
        data.setdefault("faits", {})[cle(rel, saison)] = datetime.now().strftime("%Y-%m-%d %H:%M")
        try:
            _ecrire(data)
        except OSError:
            pass


def reglages():
    data = _etat()
    defaut = data.get("defaut") if data.get("defaut") in CHOIX else "debut"
    return {"defaut": defaut, "debut": data["debut"], "choix": [{"valeur": k, "libelle": v} for k, v in LIBELLES.items()]}


def regler_defaut(valeur):
    if valeur not in CHOIX:
        raise ValueError("Choix inconnu")
    _etat()
    with _verrou:
        data = _lire()
        data["defaut"] = valeur
        _ecrire(data)


def _date_min(choix, debut):
    if choix == "tout":
        return None
    if choix == "debut":
        return debut
    return (datetime.now(timezone.utc) - timedelta(days=CHOIX[choix])).strftime("%Y-%m-%dT%H:%M:%SZ")


def _annee(item):
    a = item.get("ProductionYear")
    return str(a) if a else "?"


def lister(choix=None, limite=60):
    """Éléments à faire, du plus récent au plus ancien : [{rel, titre, media_type, tmdb, annee, date, poster, saisons}]"""
    data = _etat()
    if choix not in CHOIX:
        choix = data.get("defaut") if data.get("defaut") in CHOIX else "debut"
    faits = set((data.get("faits") or {}).keys()) | set(library.load_map().values())
    params = {"Recursive": "true", "IncludeItemTypes": "Movie,Series,Season", "SortBy": "DateCreated",
              "SortOrder": "Descending", "Fields": "Path,ProviderIds,DateCreated,ProductionYear", "Limit": 400}
    mini = _date_min(choix, data["debut"])
    if mini:
        params["MinDateCreated"] = mini
    items = emby._request("GET", "/Items", params=params).json().get("Items", [])

    groupes, series_a_lire = {}, {}
    for it in items:
        genre = it.get("Type")
        if genre in ("Movie", "Series"):
            mt = "movie" if genre == "Movie" else "tv"
            rel = library.rel_depuis_emby(it.get("Path"), mt)
            if not rel:
                continue
            g = groupes.setdefault(rel, {"rel": rel, "saisons": [], "poster": False, "date": it.get("DateCreated", "")})
            g.update(titre=it.get("Name") or rel.partition("/")[2], media_type=mt, tmdb=emby._tmdb_of(it), annee=_annee(it))
            if rel not in faits:
                g["poster"] = True
        elif genre == "Season" and it.get("Path") and it.get("IndexNumber") is not None:
            rel = library.rel_depuis_emby(str(Path(it["Path"]).parent), "tv")
            n = int(it["IndexNumber"])
            if not rel or cle(rel, n) in faits:
                continue
            g = groupes.setdefault(rel, {"rel": rel, "saisons": [], "poster": False, "date": it.get("DateCreated", "")})
            if n not in g["saisons"]:
                g["saisons"].append(n)
            g.setdefault("titre", it.get("SeriesName") or rel.partition("/")[2])
            g.setdefault("media_type", "tv")
            if "tmdb" not in g and it.get("SeriesId"):
                series_a_lire.setdefault(it["SeriesId"], []).append(rel)

    if series_a_lire:                     # série ancienne qui reçoit une nouvelle saison : son identifiant TMDB
        ids = list(series_a_lire)
        for i in range(0, len(ids), 50):
            lot = emby._request("GET", "/Items", params={"Ids": ",".join(ids[i:i + 50]), "Fields": "ProviderIds,ProductionYear"}).json()
            for s in lot.get("Items", []):
                for rel in series_a_lire.get(s.get("Id"), []):
                    g = groupes[rel]
                    g.setdefault("tmdb", emby._tmdb_of(s))
                    g.setdefault("annee", _annee(s))
                    g["titre"] = s.get("Name") or g["titre"]

    resultat = []
    for g in groupes.values():
        if not g["poster"] and not g["saisons"]:
            continue
        g["saisons"].sort()
        g.setdefault("tmdb", None)
        g.setdefault("annee", "?")
        resultat.append(g)
    resultat.sort(key=lambda g: g["date"], reverse=True)
    return {"choix": choix, "debut": data["debut"], "elements": resultat[:limite], "total": len(resultat)}
