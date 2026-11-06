"""✨ Inventer le décor qui manque : bandes ajoutées à l'image, remplies par un faux LaMa (aucun réseau, aucun vrai fichier)."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

from test_liaisons import Base  # noqa: E402


class FauxLama:
    @staticmethod
    def check():
        return True, ""

    @staticmethod
    def inpaint(img, masque):
        a = np.asarray(img).copy()
        a[masque > 0] = (10, 200, 30)
        return Image.fromarray(a)


class EtendreTest(Base):
    def _appel(self, image, **corps):
        self.connecter()
        base = {"poster_url": "x", "crop_x": 0.5, "crop_y": 0.5, "zoom": 1.0, "border_style": "standard"}
        base.update(corps)
        with mock.patch.object(self.A, "load_source_image", return_value=image), mock.patch.dict(sys.modules, {"lama": FauxLama}), \
                mock.patch.object(self.A, "UPLOAD_DIR", self.tmp.name):
            return self.c.post("/api/etendre", json=base)

    def test_image_trop_petite_en_hauteur_recoit_des_bandes_et_garde_son_cadrage(self):
        image = Image.new("RGB", (1000, 800), (200, 50, 50))                 # paysage : au zoom 60 % il manque de la hauteur
        r = self._appel(image, zoom=0.6)
        d = r.get_json()
        self.assertEqual(r.status_code, 200, d)
        self.assertFalse(d.get("rien"))
        self.assertGreater(d["h"], 800)
        self.assertEqual(d["w"], 1000)                                         # la largeur ne manquait pas
        res = Image.open(Path(self.tmp.name) / d["url"].rsplit("/", 1)[1])
        a = np.asarray(res)
        b = (res.height - 800) // 2
        self.assertLess(int(np.abs(a[b + 5:b + 795].astype(int) - (200, 50, 50)).max()), 6)       # l'image d'origine est intacte (JPEG près)
        self.assertLess(int(np.abs(a[0].astype(int) - (10, 200, 30)).max()), 12)                   # la bande est remplie
        # même échelle d'affichage qu'avant
        iw, ih = 956, 1456
        avant = max(iw / 1000, ih / 800) * 0.6
        apres = max(iw / d["w"], ih / d["h"]) * d["zoom"]
        self.assertAlmostEqual(avant, apres, delta=0.003)

    def test_rien_ne_manque(self):
        r = self._appel(Image.new("RGB", (600, 900), (1, 2, 3)), zoom=1.0)
        self.assertTrue(r.get_json()["rien"])

    def test_trop_grand_manque_refuse(self):
        r = self._appel(Image.new("RGB", (1000, 300), (1, 2, 3)), zoom=0.5)
        self.assertEqual(r.status_code, 422)

    def test_la_page_propose_le_bouton(self):
        self.connecter()
        page = self.c.get("/").get_data(as_text=True)
        self.assertIn('id="etendreBtn"', page)
        self.assertIn("/api/etendre", page)


if __name__ == "__main__":
    unittest.main()
