"""🔗 Applis voisines (voisins.py, fichier identique dans MouFloster et MouFlopening) : clé obligatoire, voisine du même
serveur trouvée toute seule, réglages communs repris sans jamais montrer les valeurs. Tout se passe dans un dossier factice."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from flask import Flask, jsonify

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import voisins  # noqa: E402

NOMS = ("MOUFL_CLE_VOISINES", "EMBY_URL", "EMBY_API_KEY", "TMDB_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
        "MOUFLOSTER_URL", "MOUFLOSTER_CLE", "MOUFLOPENING_URL", "MOUFLOPENING_CLE", "MOUFLOPENING_URL_EXTERNE")


def ecrire(fichier, nom, valeur):
    fichier.parent.mkdir(parents=True, exist_ok=True)
    lignes = [l for l in (fichier.read_text().splitlines() if fichier.exists() else []) if not l.startswith(nom + "=")]
    fichier.write_text("\n".join(lignes + [f'{nom}="{valeur}"']) + "\n")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.racine = Path(self.tmp.name)
        self.moi = self.racine / "moufloster"
        (self.moi / "data").mkdir(parents=True)
        self._env = {k: os.environ.get(k) for k in NOMS}
        for k in NOMS:
            os.environ.pop(k, None)
        self._etat = dict(voisins._etat)
        voisins._echecs.clear()
        self.app = Flask(__name__)
        voisins.init_app(self.app, self.moi, ecrire, lambda noms: self.repris.extend(noms))
        self.repris = []

        @voisins.route(self.app, "/api/voisin/essai")
        def essai():
            return jsonify({"ok": True})
        self.client = self.app.test_client()

    def tearDown(self):
        voisins._etat.clear()
        voisins._etat.update(self._etat)
        for k, v in self._env.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        self.tmp.cleanup()

    def voisine(self, nom, secrets=None, fichiers=None):
        d = self.racine / nom / "data"
        d.mkdir(parents=True)
        (d / "secrets.env").write_text("".join(f'{k}="{v}"\n' for k, v in (secrets or {}).items()))
        for chemin, contenu in (fichiers or {}).items():
            (self.racine / nom / chemin).write_text(contenu)
        return d.parent


class CleTest(Base):
    def test_cle_creee_au_lancement_seulement(self):
        self.assertNotIn("MOUFL_CLE_VOISINES", os.environ)          # pas à l'import ni pendant un essai
        voisins.preparer_cle()
        cle = os.environ["MOUFL_CLE_VOISINES"]
        self.assertIn(cle, (self.moi / "data" / "secrets.env").read_text())
        voisins.preparer_cle()
        self.assertEqual(os.environ["MOUFL_CLE_VOISINES"], cle)     # jamais remplacée toute seule

    def test_sans_cle_ou_mauvaise_cle_refuse(self):
        os.environ["MOUFL_CLE_VOISINES"] = "bonne-cle"
        self.assertEqual(self.client.get("/api/voisin/essai").status_code, 403)
        self.assertEqual(self.client.get("/api/voisin/essai", headers={"X-MouFl-Cle": "mauvaise"}).status_code, 403)
        self.assertEqual(self.client.get("/api/voisin/essai", headers={"X-MouFl-Cle": "bonne-cle"}).get_json(), {"ok": True})

    def test_aucune_cle_reglee_tout_est_refuse(self):
        self.assertEqual(self.client.get("/api/voisin/essai", headers={"X-MouFl-Cle": ""}).status_code, 403)

    def test_trop_d_essais_bloque(self):
        os.environ["MOUFL_CLE_VOISINES"] = "bonne-cle"
        for _ in range(10):
            self.client.get("/api/voisin/essai", headers={"X-MouFl-Cle": "x"})
        self.assertEqual(self.client.get("/api/voisin/essai", headers={"X-MouFl-Cle": "bonne-cle"}).status_code, 403)


class JoindreTest(Base):
    def test_voisine_du_meme_serveur(self):
        self.voisine("mouflopening", {"MOUFL_CLE_VOISINES": "cle-opening"})
        self.assertEqual(voisins.joindre("mouflopening"), ("http://127.0.0.1:8001", "cle-opening"))
        self.assertIsNone(voisins.joindre("moufloster"))             # soi-même : jamais

    def test_voisine_sur_une_autre_machine(self):
        self.assertIsNone(voisins.joindre("mouflopening"))
        os.environ.update(MOUFLOPENING_URL="http://10.0.0.5:8001/", MOUFLOPENING_CLE="cle-loin")
        self.assertEqual(voisins.joindre("mouflopening"), ("http://10.0.0.5:8001", "cle-loin"))

    def test_appeler_envoie_la_cle_et_lit_les_erreurs(self):
        self.voisine("mouflopening", {"MOUFL_CLE_VOISINES": "cle-opening"})
        rep = mock.Mock(status_code=200, json=lambda: {"ok": True})
        with mock.patch.object(voisins.requests, "request", return_value=rep) as req:
            self.assertEqual(voisins.appeler("mouflopening", "GET", "/api/voisin/x"), {"ok": True})
        self.assertEqual(req.call_args.kwargs["headers"], {"X-MouFl-Cle": "cle-opening"})
        refus = mock.Mock(status_code=403, json=lambda: {"error": "Clé refusée"})
        with mock.patch.object(voisins.requests, "request", return_value=refus), self.assertRaises(voisins.ErreurVoisin) as e:
            voisins.appeler("mouflopening", "GET", "/api/voisin/x")
        self.assertIn("Clé refusée", str(e.exception))
        with mock.patch.object(voisins.requests, "request", side_effect=voisins.requests.exceptions.ConnectionError()), \
                self.assertRaises(voisins.ErreurVoisin):
            voisins.appeler("mouflopening", "GET", "/api/voisin/x")


class ReglagesTest(Base):
    def test_reglages_des_voisines_avec_leurs_fichiers_propres(self):
        self.voisine("mouflopening", {"TELEGRAM_BOT_TOKEN": "111:jeton-opening"},
                     {"config.json": json.dumps({"emby": {"host": "http://emby:8096", "api_key": "cle-emby-1234"}})})
        self.voisine("mouflanimexer", {}, {"telegram_config.json": json.dumps({"bot_token": "222:jeton", "chat_id": "-100777"})})
        self.assertEqual(voisins.reglages_de("mouflopening"),
                         {"EMBY_URL": "http://emby:8096", "EMBY_API_KEY": "cle-emby-1234", "TELEGRAM_BOT_TOKEN": "111:jeton-opening"})
        self.assertEqual(voisins.reglages_de("mouflanimexer"), {"TELEGRAM_BOT_TOKEN": "222:jeton", "TELEGRAM_CHAT_ID": "-100777"})

    def test_etat_ne_montre_jamais_les_valeurs(self):
        self.voisine("mouflopening", {"EMBY_API_KEY": "secret-emby-abcd", "TMDB_API_KEY": "secret-tmdb-wxyz", "EMBY_URL": "http://emby:8096"})
        texte = json.dumps(self.client.get("/api/settings/voisines").get_json(), ensure_ascii=False)
        self.assertNotIn("secret-emby", texte)
        self.assertNotIn("secret-tmdb", texte)
        self.assertIn("…abcd", texte)
        self.assertIn("http://emby:8096", texte)                       # une adresse n'est pas un secret

    def test_reprendre_seulement_ce_qui_est_coche(self):
        self.voisine("mouflopening", {"EMBY_API_KEY": "secret-emby-abcd", "TMDB_API_KEY": "secret-tmdb-wxyz"})
        r = self.client.post("/api/settings/voisines", json={"action": "reprendre", "app": "mouflopening", "noms": ["TMDB_API_KEY", "APP_PASSWORD_HASH"]})
        self.assertTrue(r.get_json()["ok"])
        self.assertEqual(os.environ["TMDB_API_KEY"], "secret-tmdb-wxyz")
        self.assertNotIn("EMBY_API_KEY", os.environ)
        self.assertNotIn("secret", r.get_data(as_text=True))
        self.assertEqual(self.repris, ["TMDB_API_KEY"])
        secrets = (self.moi / "data" / "secrets.env").read_text()
        self.assertIn("TMDB_API_KEY", secrets)
        self.assertNotIn("APP_PASSWORD_HASH", secrets)                # jamais l'identifiant d'une autre appli

    def test_valeur_dangereuse_refusee(self):
        self.voisine("mouflopening", {"TMDB_API_KEY": "abc$(rm -rf /)"})
        r = self.client.post("/api/settings/voisines", json={"action": "reprendre", "app": "mouflopening", "noms": ["TMDB_API_KEY"]})
        self.assertEqual(r.status_code, 400)
        self.assertNotIn("TMDB_API_KEY", os.environ)

    def test_nouvelle_cle_montree_une_fois_et_cle_distante(self):
        r = self.client.post("/api/settings/voisines", json={"action": "nouvelle_cle"}).get_json()
        self.assertEqual(os.environ["MOUFL_CLE_VOISINES"], r["cle"])
        etat = self.client.get("/api/settings/voisines").get_json()
        self.assertEqual(etat["cle_fin"], "…" + r["cle"][-4:])
        self.assertNotIn(r["cle"], json.dumps(etat))
        self.client.post("/api/settings/voisines", json={"action": "distante", "app": "mouflopening", "cle": "cle-loin"})
        self.client.post("/api/settings/voisines", json={"action": "adresses", "app": "mouflopening", "url": "http://10.0.0.5:8001", "url_externe": ""})
        self.assertEqual(voisins.joindre("mouflopening"), ("http://10.0.0.5:8001", "cle-loin"))
        self.assertEqual(self.client.post("/api/settings/voisines", json={"action": "adresses", "app": "mouflopening", "url": "ftp://x"}).status_code, 400)
        self.assertEqual(self.client.post("/api/settings/voisines", json={"action": "distante", "app": "moufloster", "cle": "x"}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
