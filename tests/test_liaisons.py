"""Aperçu envoyé en image et liens avec MouFlopening (nouveautés, générique cherché tout seul après un poster) :
essais par les adresses de l'appli, sans réseau ni fichier réel (images et clés factices)."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class Base(unittest.TestCase):
    def setUp(self):
        from werkzeug.security import generate_password_hash
        self.tmp = tempfile.TemporaryDirectory()
        self._env = {k: os.environ.get(k) for k in ("APP_USER", "APP_PASSWORD_HASH", "MOUFL_CLE_VOISINES")}
        import app as A                       # avant les réglages d'essai : l'import relit data/secrets.env
        os.environ.update(APP_USER="admin", APP_PASSWORD_HASH=generate_password_hash("motdepasse-admin"), MOUFL_CLE_VOISINES="cle-essai")
        self.A = A
        self.c = A.app.test_client()
        self.c.environ_base["REMOTE_ADDR"] = "192.168.1.50"

    def connecter(self):
        self.assertEqual(self.c.post("/login", data={"username": "admin", "password": "motdepasse-admin"}).status_code, 302)

    def tearDown(self):
        for k, v in self._env.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        self.tmp.cleanup()


class ApercuTest(Base):
    def test_apercu_en_image_avec_debord_et_lignes(self):
        from PIL import Image
        self.connecter()
        Image.new("RGB", (800, 1000), "red").save(Path(self.tmp.name) / ("a" * 32 + ".jpg"))
        with mock.patch.object(self.A, "UPLOAD_DIR", self.tmp.name):
            r = self.c.post("/api/preview", json={"poster_url": "/api/upload/" + "a" * 32 + ".jpg", "text_2": "TITRE", "zoom": 1.2})
        self.assertEqual(r.mimetype, "image/jpeg")
        self.assertEqual(Image.open(__import__("io").BytesIO(r.data)).size, (1000, 1500))
        self.assertGreater(json.loads(r.headers["X-Apercu-Debord"])[1], 0)
        self.assertEqual(json.loads(r.headers["X-Apercu-Lignes"])[0]["ligne"], 2)

    def test_erreur_toujours_en_json(self):
        self.connecter()
        r = self.c.post("/api/preview", json={})
        self.assertEqual((r.status_code, r.mimetype), (400, "application/json"))


class MouflopeningTest(Base):
    def test_nouveautes_pour_mouflopening_cle_obligatoire(self):
        self.assertEqual(self.c.get("/api/voisin/nouveautes?depuis=2026-10-26T02:00:00Z").status_code, 403)
        h = {"X-MouFl-Cle": "cle-essai"}
        self.assertEqual(self.c.get("/api/voisin/nouveautes?depuis=hier", headers=h).status_code, 400)
        with mock.patch.object(self.A.emby, "configured", return_value=True), \
                mock.patch.object(self.A.nouveautes, "arrivees", return_value=[{"rel": "Series/Below"}]) as arr:
            r = self.c.get("/api/voisin/nouveautes?depuis=2026-10-26T02:00:00Z", headers=h).get_json()
        self.assertEqual(r["elements"], [{"rel": "Series/Below"}])
        arr.assert_called_once_with("2026-10-26T02:00:00Z")

    def test_generique_demande_a_mouflopening(self):
        self.connecter()
        with mock.patch.object(self.A.voisins, "appeler", return_value={"ok": True, "job": "abc123"}) as ap:
            r = self.c.post("/api/generique/auto", json={"dossier": "/media/Films HD/Wicked (2024)"}).get_json()
        self.assertEqual(r["job"], "abc123")
        self.assertEqual(ap.call_args.args[:3], ("mouflopening", "POST", "/api/voisin/generique/auto"))
        with mock.patch.object(self.A.voisins, "appeler", side_effect=self.A.voisins.ErreurVoisin("MouFlopening injoignable")):
            r = self.c.post("/api/generique/auto", json={"dossier": "/media/x"}).get_json()
        self.assertEqual((r["ok"], r["error"]), (False, "MouFlopening injoignable"))     # la page propose alors le lien
        self.assertEqual(self.c.get("/api/generique/job/../../etc").status_code, 404)

    def test_nouveautes_disent_si_le_generique_est_la(self):
        self.connecter()
        media = Path(self.tmp.name) / "media"
        (media / "Films HD" / "Avec (2024)").mkdir(parents=True)
        (media / "Films HD" / "Avec (2024)" / "theme.mp3").write_bytes(b"x" * 9000)
        (media / "Films HD" / "Sans (2024)").mkdir()
        els = [{"rel": "Films HD/Avec (2024)", "titre": "Avec"}, {"rel": "Films HD/Sans (2024)", "titre": "Sans"}]
        with mock.patch.object(self.A.library, "LIBRARY_ROOT", media), mock.patch.object(self.A.emby, "configured", return_value=True), \
                mock.patch.object(self.A.nouveautes, "lister", return_value={"elements": els, "total": 2}):
            r = self.c.get("/api/nouveautes").get_json()
        self.assertEqual([e["generique"] for e in r["elements"]], [True, False])
        self.assertTrue(r["elements"][1]["dossier"].endswith("Sans (2024)"))


if __name__ == "__main__":
    unittest.main()
