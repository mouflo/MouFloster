"""Poster déjà fait dans MouFlanga : une série sans fiche TheMovieDB (manga sans anime) se retrouve par son nom et son poster se reprend."""
import io
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import mouflanga_link  # noqa: E402
from test_liaisons import Base  # noqa: E402

SERIES = [{"name": "+ Again", "cover": True}, {"name": "Again!!", "cover": True}, {"name": "Air Gear", "cover": True},
          {"name": "Air Gear Unlimited", "cover": False}, {"name": "Gantz", "cover": True}]


class CorrespondanceTest(unittest.TestCase):
    def test_noms_proches(self):
        self.assertTrue(mouflanga_link.correspond("+ Again", "again"))
        self.assertTrue(mouflanga_link.correspond("+ Again", "plus again"))
        self.assertTrue(mouflanga_link.correspond("Air Gear", "airgear"))
        self.assertTrue(mouflanga_link.correspond("Prisonnier Riku", "PRISONNIER"))
        self.assertFalse(mouflanga_link.correspond("Gantz", "again"))
        self.assertFalse(mouflanga_link.correspond("Gantz", "ga"))          # trop court : tout correspondrait

    def test_seulement_les_series_avec_poster_le_plus_proche_d_abord(self):
        self.assertEqual([x["name"] for x in mouflanga_link.chercher("again", SERIES)], ["+ Again", "Again!!"])
        self.assertEqual([x["name"] for x in mouflanga_link.chercher("air gear", SERIES)], ["Air Gear"])      # sans poster : absente
        self.assertEqual(mouflanga_link.chercher("zzz", SERIES), [])


class RouteTest(Base):
    def test_recherche_sans_liaison(self):
        self.connecter()
        with mock.patch.object(mouflanga_link, "mode", return_value=""):
            self.assertEqual(self.c.get("/api/mouflanga/recherche?q=again").get_json(), {"series": []})

    def test_recherche_et_reprise_du_poster(self):
        from PIL import Image
        self.connecter()
        jpeg = io.BytesIO()
        Image.new("RGB", (600, 900), "blue").save(jpeg, "JPEG")
        with mock.patch.object(mouflanga_link, "mode", return_value="reseau"), mock.patch.object(mouflanga_link, "series", return_value=SERIES), \
                mock.patch.object(mouflanga_link, "couverture", return_value=jpeg.getvalue()), mock.patch.object(self.A, "UPLOAD_DIR", self.tmp.name):
            r = self.c.get("/api/mouflanga/recherche?q=plus again").get_json()
            self.assertEqual([x["name"] for x in r["series"]], ["+ Again", "Again!!"])
            rep = self.c.post("/api/mouflanga/reprendre", json={"name": "+ Again"}).get_json()
        self.assertEqual((rep["w"], rep["h"]), (600, 900))
        self.assertTrue(os.path.isfile(os.path.join(self.tmp.name, rep["url"].rsplit("/", 1)[1])))

    def test_reprise_sans_poster(self):
        self.connecter()
        with mock.patch.object(mouflanga_link, "couverture", return_value=None):
            r = self.c.post("/api/mouflanga/reprendre", json={"name": "Gantz"})
        self.assertEqual(r.status_code, 404)

    def test_la_recherche_est_reservee_a_l_admin(self):
        self.assertIn(self.c.get("/api/mouflanga/recherche?q=again").status_code, (302, 401, 403))
