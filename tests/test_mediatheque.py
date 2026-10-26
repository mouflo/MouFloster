"""📁 Envoi d'un poster dans la médiathèque et restauration d'un ancien poster : essais dans un dossier factice
(rien de réel n'est touché). L'ancien poster est toujours sauvegardé avant d'être remplacé, jamais rien d'écrasé."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import library


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.media = base / "media"
        self.serie = self.media / "Series" / "Below (2024)"
        (self.serie / "Season 01").mkdir(parents=True)
        (self.media / "Films HD" / "Film (2026)").mkdir(parents=True)
        self.sauvegardes = base / "anciens"
        self.nouveau = base / "nouveau.jpg"
        self.nouveau.write_bytes(b"NOUVEAU" * 10)
        self._racine = library.LIBRARY_ROOT
        library.LIBRARY_ROOT = self.media

    def tearDown(self):
        library.LIBRARY_ROOT = self._racine
        self.tmp.cleanup()

    def envoyer(self, action, nom="poster.jpg", rel="Series/Below (2024)", sub="", source=None):
        return library.apply_poster(str(source or self.nouveau), rel, nom, action, str(self.sauvegardes), sub=sub)

    def restes(self, dossier):
        return [p.name for p in dossier.iterdir() if p.name.endswith(".tmp")]


class EnvoiTest(Base):
    def test_premier_poster(self):
        r = self.envoyer("copy")
        self.assertEqual((self.serie / "poster.jpg").read_bytes(), self.nouveau.read_bytes())
        self.assertEqual(r, {"written": "Series/Below (2024)/poster.jpg", "backup": None})
        self.assertEqual(self.restes(self.serie), [])

    def test_copie_refusee_si_un_poster_existe(self):
        (self.serie / "poster.jpg").write_bytes(b"ANCIEN")
        with self.assertRaises(ValueError):
            self.envoyer("copy")
        self.assertEqual((self.serie / "poster.jpg").read_bytes(), b"ANCIEN")

    def test_remplacer_sauvegarde_l_ancien(self):
        (self.serie / "poster.jpg").write_bytes(b"ANCIEN")
        r = self.envoyer("replace")
        self.assertEqual((self.serie / "poster.jpg").read_bytes(), self.nouveau.read_bytes())
        self.assertEqual(Path(r["backup"]).read_bytes(), b"ANCIEN")
        self.assertTrue(Path(r["backup"]).is_relative_to(self.sauvegardes / "Series" / "Below (2024)"))

    def test_deux_remplacements_dans_la_meme_seconde_gardent_l_original(self):
        (self.serie / "poster.jpg").write_bytes(b"ORIGINAL")
        with mock.patch.object(library, "datetime") as dt:
            dt.now.return_value.strftime.return_value = "20261026-120000"
            r1 = self.envoyer("replace")
            r2 = self.envoyer("replace")
        self.assertNotEqual(r1["backup"], r2["backup"])
        self.assertEqual(Path(r1["backup"]).read_bytes(), b"ORIGINAL")

    def test_garder_les_deux(self):
        (self.serie / "poster.jpg").write_bytes(b"ANCIEN")
        r1 = self.envoyer("both")
        r2 = self.envoyer("both")
        self.assertEqual((self.serie / "poster.jpg").read_bytes(), b"ANCIEN")
        self.assertEqual(r1["written"], "Series/Below (2024)/poster-new.jpg")
        self.assertNotEqual(r1["written"], r2["written"])
        self.assertTrue((self.serie / r2["written"].rsplit("/", 1)[1]).is_file())

    def test_poster_de_saison_dans_le_sous_dossier(self):
        r = self.envoyer("copy", nom="season01-poster.jpg", sub="Season 01")
        self.assertTrue((self.serie / "Season 01" / "season01-poster.jpg").is_file())
        self.assertEqual(r["written"], "Series/Below (2024)/Season 01/season01-poster.jpg")

    def test_noms_et_dossiers_interdits(self):
        for nom, rel, sub in (("../poster.jpg", "Series/Below (2024)", ""), ("cover.jpg", "Series/Below (2024)", ""),
                              ("poster.jpg", "Series/../../x", ""), ("poster.jpg", "Autre/Below (2024)", ""),
                              ("poster.jpg", "Series/Absent", ""), ("poster.jpg", "Series/Below (2024)", "../.."),
                              ("poster.jpg", "Series/@eaDir", "")):
            with self.subTest(nom=nom, rel=rel, sub=sub), self.assertRaises(ValueError):
                self.envoyer("copy", nom=nom, rel=rel, sub=sub)
        self.assertEqual(sorted(p.name for p in self.media.rglob("*.jpg")), [])

    def test_source_absente_ou_action_inconnue(self):
        with self.assertRaises(ValueError):
            self.envoyer("copy", source=self.media / "absent.jpg")
        with self.assertRaises(ValueError):
            self.envoyer("ecraser")

    def test_ecriture_ratee_rien_n_est_abime(self):
        (self.serie / "poster.jpg").write_bytes(b"ANCIEN")
        with mock.patch.object(library.os, "replace", side_effect=OSError("partage déconnecté")):
            with self.assertRaises(OSError):
                self.envoyer("replace")
        self.assertEqual((self.serie / "poster.jpg").read_bytes(), b"ANCIEN")
        self.assertEqual(self.restes(self.serie), [])                     # pas de fichier temporaire oublié
        self.assertEqual([p.read_bytes() for p in self.sauvegardes.rglob("*.jpg")], [b"ANCIEN"])


class RestaurationTest(Base):
    """Par l'adresse de l'appli : l'ancien poster revient, l'actuel est sauvegardé à son tour."""

    def setUp(self):
        super().setUp()
        from werkzeug.security import generate_password_hash
        self._env = {k: os.environ.get(k) for k in ("APP_USER", "APP_PASSWORD_HASH")}
        import app as A                       # avant les réglages d'essai : l'import relit data/secrets.env
        os.environ.update(APP_USER="admin", APP_PASSWORD_HASH=generate_password_hash("motdepasse-admin"))
        self.A = A
        self._backup = A.BACKUP_ROOT
        A.BACKUP_ROOT = str(self.sauvegardes)
        self.client = A.app.test_client()
        self.client.environ_base["REMOTE_ADDR"] = "192.168.1.50"
        self.assertEqual(self.client.post("/login", data={"username": "admin", "password": "motdepasse-admin"}).status_code, 302)

    def tearDown(self):
        self.A.BACKUP_ROOT = self._backup
        for k, v in self._env.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        super().tearDown()

    def test_restaurer(self):
        (self.serie / "poster.jpg").write_bytes(b"ORIGINAL")
        self.envoyer("replace")                                            # ORIGINAL part dans les anciens posters
        corps = {"rel": "Series/Below (2024)", "media_type": "tv", "target_type": "tv"}
        liste = self.client.post("/api/library/backups", json=corps).get_json()["backups"]
        self.assertEqual(len(liste), 1)
        time.sleep(1.1)                                                    # nouvelle seconde : nom de sauvegarde distinct
        r = self.client.post("/api/library/restore", json={**corps, "id": liste[0]["id"]}).get_json()
        self.assertTrue(r.get("success"), r)
        self.assertEqual((self.serie / "poster.jpg").read_bytes(), b"ORIGINAL")
        self.assertIn(self.nouveau.read_bytes(), [p.read_bytes() for p in self.sauvegardes.rglob("*.jpg")])

    def test_ancien_poster_d_un_autre_titre_refuse(self):
        autre = self.sauvegardes / "Films HD" / "Film (2026)"
        autre.mkdir(parents=True)
        (autre / "poster.20261020-100000.jpg").write_bytes(b"AUTRE")
        r = self.client.post("/api/library/restore", json={"rel": "Series/Below (2024)", "media_type": "tv", "target_type": "tv",
                                                           "id": "Films HD/Film (2026)/poster.20261020-100000.jpg"})
        self.assertEqual(r.status_code, 400)
        self.assertFalse((self.serie / "poster.jpg").exists())


class ApercuTest(unittest.TestCase):
    """Le dégradé calculé une fois donne exactement la même image qu'avant (ligne par ligne)."""

    def test_degrade_identique(self):
        import numpy as np
        from PIL import Image, ImageDraw
        import app as A
        for bw in (0, 22, 45):
            iw, ih = 1000 - 2 * bw, 1500 - 2 * bw
            ref = Image.new("RGBA", (iw, ih), (0, 0, 0, 0))
            d = ImageDraw.Draw(ref)
            xs, ys = [p[0] for p in A.GRADIENT_CURVE], [p[1] for p in A.GRADIENT_CURVE]
            for y in range(ih):
                a = int(round(255 * float(np.interp(y + bw, xs, ys))))
                if a:
                    d.line([(0, y), (iw, y)], fill=(0, 0, 0, a))
            self.assertTrue(np.array_equal(np.array(ref), np.array(A._degrade(bw))), bw)


if __name__ == "__main__":
    unittest.main()
