"""
Actualisation ciblée d'Emby: après l'envoi d'un poster, Emby ne relit que CE film / CETTE série
(et la saison concernée), sans rescanner toute la médiathèque.

Config (data/secrets.env sur le serveur, jamais sur GitHub):
    EMBY_API_KEY="..."            (obligatoire)
    EMBY_URL="http://IP:8096"     (facultatif, valeur par défaut ci-dessous)
    EMBY_REFRESH_MODE="ValidationOnly"   (facultatif: ValidationOnly | Default | FullRefresh)
"""

import os
import re

import requests

from library import fold, parse_season, split_folder_name

DEFAULT_URL = "http://192.168.1.134:8096"
TIMEOUT = 8


class EmbyError(Exception):
    pass


def base_url():
    return os.getenv("EMBY_URL", DEFAULT_URL).strip().rstrip("/")


def api_key():
    return os.getenv("EMBY_API_KEY", "").strip()


def configured():
    return bool(api_key())


def _request(method, path, extra_headers=None, **kwargs):
    headers = {"X-Emby-Token": api_key(), "Accept": "application/json", **(extra_headers or {})}
    try:
        resp = requests.request(method, base_url() + path, headers=headers, timeout=TIMEOUT, **kwargs)
    except requests.exceptions.ConnectionError:
        raise EmbyError(f"Emby injoignable ({base_url()})")
    except requests.exceptions.Timeout:
        raise EmbyError("Emby ne répond pas (délai dépassé)")
    except requests.exceptions.RequestException as e:
        raise EmbyError(f"Erreur de connexion à Emby: {e}")
    if resp.status_code in (401, 403):
        raise EmbyError("Clé API Emby refusée (vérifie EMBY_API_KEY)")
    if resp.status_code >= 400:
        raise EmbyError(f"Emby a répondu {resp.status_code}")
    return resp


def _items(params):
    base = {"Recursive": "true", "Fields": "Path,ProviderIds", "Limit": 30}
    base.update(params)
    data = _request("GET", "/Items", params=base).json()
    return data.get("Items", []) if isinstance(data, dict) else []


def _tmdb_of(item):
    ids = item.get("ProviderIds") or {}
    for k, v in ids.items():
        if k.lower() == "tmdb":
            return str(v)
    return None


def _path_has_folder(item, folder_name):
    parts = re.split(r"[\\/]", item.get("Path") or "")
    return folder_name in parts


def find_item(media_type, tmdb_id, folder_name, titles):
    """Retrouve l'élément Emby: par identifiant TMDB (sûr), sinon par nom + dossier."""
    item_type = "Movie" if media_type == "movie" else "Series"

    # 1) Par identifiant TMDB (on vérifie le TMDB renvoyé: certains Emby ignorent le filtre)
    if tmdb_id:
        try:
            found = [i for i in _items({"IncludeItemTypes": item_type, "AnyProviderIdEquals": f"tmdb.{tmdb_id}", "Limit": 50})
                     if _tmdb_of(i) == str(tmdb_id)]
        except EmbyError:
            raise
        if found:
            for i in found:
                if _path_has_folder(i, folder_name):
                    return i
            return found[0]

    # 2) Par nom, en exigeant que le dossier corresponde
    terms, seen = [], set()
    clean = split_folder_name(folder_name)[0]
    for t in [re.sub(r"\(\d{4}\)\s*$", "", folder_name).strip(), clean, *titles]:
        if t and fold(t) not in seen:
            seen.add(fold(t))
            terms.append(t)
    for term in terms:
        for i in _items({"IncludeItemTypes": item_type, "SearchTerm": term}):
            if _path_has_folder(i, folder_name):
                return i
    return None


def _refresh(item_id):
    mode = os.getenv("EMBY_REFRESH_MODE", "ValidationOnly")
    if mode not in ("ValidationOnly", "Default", "FullRefresh"):
        mode = "ValidationOnly"
    _request(
        "POST",
        f"/Items/{item_id}/Refresh",
        params={
            "Recursive": "false",
            "ImageRefreshMode": mode,
            "MetadataRefreshMode": mode,
            "ReplaceAllImages": "false",
            "ReplaceAllMetadata": "false",
        },
    )


def refresh_for(media_type, tmdb_id, rel, titles, season_text=None, episode_text=None):
    """
    Lance l'actualisation ciblée. Ne lève jamais d'exception.
    Retourne {"ok": bool, "message": str}
    """
    if not configured():
        return {"ok": False, "message": "Emby non configuré (clé API manquante)"}
    folder_name = rel.split("/")[-1]
    try:
        item = find_item(media_type, tmdb_id, folder_name, [t for t in titles if t])
        if not item:
            return {"ok": False, "message": f"« {folder_name} » introuvable dans Emby (pas encore scanné ?). Poster copié quand même."}

        name = item.get("Name") or folder_name
        done = [f"« {name} »"]

        season_no = parse_season(season_text) if media_type == "tv" else None
        if season_no is not None:
            seasons = _request("GET", f"/Shows/{item['Id']}/Seasons", params={"Fields": "Path"}).json().get("Items", [])
            season = next((s for s in seasons if s.get("IndexNumber") == season_no), None)
            if season:
                _refresh(season["Id"])
                done.append("saison " + ("0 (spéciaux)" if season_no == 0 else str(season_no)))
            else:
                done.append(f"(saison {season_no} absente d'Emby)")

        ep_no = parse_season(episode_text) if media_type == "tv" else None
        if ep_no is not None and season_no is not None:
            eps = _request("GET", f"/Shows/{item['Id']}/Episodes", params={"Season": season_no}).json().get("Items", [])
            ep = next((e for e in eps if e.get("IndexNumber") == ep_no and e.get("ParentIndexNumber") in (None, season_no)), None)
            if not ep:  # certains Emby ignorent le filtre de saison pour les spéciaux: on filtre nous-mêmes
                eps = _request("GET", f"/Shows/{item['Id']}/Episodes").json().get("Items", [])
                ep = next((e for e in eps if e.get("IndexNumber") == ep_no and e.get("ParentIndexNumber") == season_no), None)
            if ep:
                _refresh(ep["Id"])
                done.append(f"épisode S{season_no:02d}E{ep_no:02d}")
            else:
                done.append(f"(épisode S{season_no:02d}E{ep_no:02d} absent d'Emby)")

        _refresh(item["Id"])  # le film / la série eux-mêmes (sans les épisodes)
        return {"ok": True, "message": "Emby actualise " + " · ".join(done)}
    except EmbyError as e:
        return {"ok": False, "message": str(e)}
    except Exception as e:  # filet de sécurité: ne jamais casser la copie du poster
        return {"ok": False, "message": f"Erreur Emby inattendue: {e}"}


# ---------------------------------------------------------------------------
# Sagas (collections Emby « BoxSet ») : leur affiche n'est pas dans un dossier de la médiathèque,
# Emby la garde lui-même -> on la lit et on la remplace par l'API d'Emby.
# ---------------------------------------------------------------------------

def find_boxsets(tmdb_collection_id=None, terms=()):
    """Collections Emby candidates : d'abord par identifiant TMDB de la saga, puis par nom. -> [{id, name, sure}]"""
    out, seen = [], set()
    if tmdb_collection_id:
        for i in _items({"IncludeItemTypes": "BoxSet", "AnyProviderIdEquals": f"tmdb.{tmdb_collection_id}", "Limit": 20}):
            if _tmdb_of(i) == str(tmdb_collection_id) and i["Id"] not in seen:
                seen.add(i["Id"])
                out.append({"id": i["Id"], "name": i.get("Name", "?"), "sure": True})
    for term in terms:
        if not term:
            continue
        for i in _items({"IncludeItemTypes": "BoxSet", "SearchTerm": term, "Limit": 20}):
            if i["Id"] not in seen:
                seen.add(i["Id"])
                out.append({"id": i["Id"], "name": i.get("Name", "?"), "sure": False})
    return out


def get_primary_image(item_id):
    """Affiche actuelle d'un élément Emby (octets JPEG/PNG) ou None s'il n'en a pas."""
    try:
        r = _request("GET", f"/Items/{item_id}/Images/Primary", params={"quality": 95})
    except EmbyError:
        return None
    return r.content or None


def set_primary_image(item_id, image_bytes, mime="image/jpeg"):
    """Remplace l'affiche d'un élément Emby (l'API attend l'image encodée en base64)."""
    import base64
    _request("POST", f"/Items/{item_id}/Images/Primary", extra_headers={"Content-Type": mime},
             data=base64.b64encode(image_bytes))
