"""LaMa : les morceaux de masque éloignés sont remplis chacun avec leur fenêtre, et un trait fin du masque ne disparaît pas quand la fenêtre
est réduite. Le modèle est remplacé par un faux (aucun calcul réel)."""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

import lama  # noqa: E402


class GroupesTest(unittest.TestCase):
    def test_deux_morceaux_loin_l_un_de_l_autre_sont_deux_groupes(self):
        m = np.zeros((1200, 1600), np.uint8)
        m[100:160, 100:300] = 255
        m[1000:1060, 1300:1500] = 255
        g = lama._groupes(m)
        self.assertEqual(len(g), 2)
        self.assertEqual(sum(int(x.sum()) for x in g), int((m > 0).sum()))

    def test_deux_morceaux_voisins_sont_un_seul_groupe(self):
        m = np.zeros((600, 800), np.uint8)
        m[100:140, 100:300] = 255
        m[170:210, 320:500] = 255
        self.assertEqual(len(lama._groupes(m)), 1)

    def test_trop_de_morceaux_sont_regroupes(self):
        m = np.zeros((2000, 2000), np.uint8)
        for i in range(12):
            m[40 + 160 * i:60 + 160 * i, 40:80] = 255
            m[40 + 160 * i:60 + 160 * i, 1900:1940] = 255
        self.assertLessEqual(len(lama._groupes(m)), lama.GROUPES_MAX)


class RemplissageTest(unittest.TestCase):
    def setUp(self):
        self.vus = []

        def faux(rgb, mask):
            self.vus.append((rgb.shape, int((mask > 127).sum()), mask.copy()))
            sortie = rgb.copy()
            sortie[mask > 127] = (0, 255, 0)
            return sortie
        self.patch = mock.patch.object(lama, "_run_isolated", side_effect=faux)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_chaque_groupe_a_sa_fenetre_et_seule_la_zone_masquee_change(self):
        img = Image.fromarray(np.full((1200, 1600, 3), 90, np.uint8))
        m = np.zeros((1200, 1600), np.uint8)
        m[100:160, 100:300] = 255
        m[1000:1060, 1300:1500] = 255
        with mock.patch.object(lama, "max_side", return_value=2000):
            out = np.asarray(lama.inpaint(img, m))
        self.assertEqual(len(self.vus), 2)
        for forme, _, _ in self.vus:
            self.assertLess(forme[0] * forme[1], 1200 * 1600 / 2)                 # une fenêtre bien plus petite que l'image entière
        import cv2
        coeur = cv2.erode((m > 0).astype(np.uint8), np.ones((7, 7), np.uint8)) > 0                 # sans le bord adouci
        self.assertLess(int(np.abs(out[coeur].astype(int) - (0, 255, 0)).max()), 25)           # (le bord est adouci sur 2 px : un reste de gris de quelques niveaux)
        loin = np.ones(m.shape, bool)
        loin[max(0, 90):170, 90:310] = False
        loin[990:1070, 1290:1510] = False
        self.assertTrue((out[loin] == 90).all())                                  # rien d'autre n'est touché

    def test_un_trait_fin_reste_masque_quand_la_fenetre_est_reduite(self):
        img = Image.fromarray(np.full((900, 900, 3), 90, np.uint8))
        m = np.zeros((900, 900), np.uint8)
        m[450, 100:800] = 255                                                     # un trait d'un pixel
        with mock.patch.object(lama, "max_side", return_value=200):               # réduction forte (÷ 3 et plus)
            lama.inpaint(img, m)
        self.assertEqual(len(self.vus), 1)
        reduit = self.vus[0][2]
        self.assertLess(max(reduit.shape), 300)
        self.assertGreater(int((reduit > 127).sum()), 50)                         # le trait existe encore dans le masque réduit
        self.assertTrue((reduit > 127).any(axis=1).sum() >= 1)

    def test_sans_groupes_une_seule_fenetre_avec_tout_le_contexte(self):
        img = Image.fromarray(np.full((1200, 1600, 3), 90, np.uint8))
        m = np.zeros((1200, 1600), np.uint8)
        m[100:160, 100:300] = 255
        m[1000:1060, 1300:1500] = 255
        with mock.patch.object(lama, "max_side", return_value=4000):
            lama.inpaint(img, m, grouper=False)
        self.assertEqual(len(self.vus), 1)
        self.assertEqual(self.vus[0][0][:2], (1200, 1600))                         # toute l'image comme contexte, comme avant

    def test_un_groupe_deja_rempli_n_est_pas_refait_par_le_suivant(self):
        img = Image.fromarray(np.full((1200, 1600, 3), 90, np.uint8))
        m = np.zeros((1200, 1600), np.uint8)
        m[100:160, 100:300] = 255
        m[1000:1060, 1300:1500] = 255
        with mock.patch.object(lama, "max_side", return_value=2000):
            lama.inpaint(img, m)
        # 2e fenêtre : le premier groupe (déjà fait) n'y est plus masqué
        self.assertEqual(self.vus[1][1], 200 * 60)


if __name__ == "__main__":
    unittest.main()
