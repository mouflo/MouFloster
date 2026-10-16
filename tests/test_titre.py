"""Découpage automatique du titre : chaque cas reprend un poster fait à la main sous Photoshop."""
import sys
import unittest
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import titre  # noqa: E402

POLICE = str(Path(__file__).resolve().parent.parent / "fonts" / "LiberationSans-Bold.ttf")   # mêmes mesures qu'Arial Bold


@lru_cache(maxsize=None)
def police(taille):
    return ImageFont.truetype(POLICE, taille)


LARGEUR = titre.largeur_utile(22)


def lignes(nom, film=True):
    r = titre.decouper(nom, LARGEUR, police, film=film)
    return tuple(r[k] for k in ("l1", "l2", "l3", "l4", "l5", "saison"))


class DecoupageTest(unittest.TestCase):
    def test_sous_titre_apres_deux_points(self):
        self.assertEqual(lignes("Top Gun : Maverick"), ("", "Top Gun", "", "Maverick", "", ""))
        self.assertEqual(lignes("FBI : Most Wanted", film=False), ("", "FBI", "", "Most Wanted", "", ""))
        self.assertEqual(lignes("65 : La Terre d'avant"), ("", "65", "", "La Terre d'avant", "", ""))

    def test_et_le(self):
        self.assertEqual(lignes("Indiana Jones et le Temple maudit"), ("", "Indiana Jones", "", "et le Temple maudit", "", ""))
        self.assertEqual(lignes("Raya et le Dernier Dragon"), ("", "Raya", "", "et le Dernier Dragon", "", ""))

    def test_et_la_suivi_d_un_seul_mot_reste_sur_une_ligne(self):
        self.assertEqual(lignes("Ant-Man et la Guêpe")[1], "Ant-Man et la Guêpe")
        plan = titre.plan_ligne("Ant-Man et la Guêpe", 89, LARGEUR, police)
        self.assertEqual(plan["taille"], 89)
        self.assertIn(("pile", "ET", "LA"), plan["blocs"])

    def test_titre_qui_commence_par_un_article_n_est_pas_coupe(self):
        self.assertEqual(lignes("Le Bon, la Brute et le Truand")[1:3], ("Le Bon, la Brute", "et le Truand"))
        self.assertEqual(lignes("La Belle et la Bête")[1:4], ("La Belle", "et la Bête", ""))

    def test_virgule(self):
        self.assertEqual(lignes("Kuzco, l'empereur mégalo"), ("", "Kuzco", "", "l'empereur mégalo", "", ""))
        self.assertEqual(lignes("Vaiana, la légende du bout du monde")[1:4], ("Vaiana", "", "la légende du bout du monde"))

    def test_sous_titre_long_sur_deux_lignes(self):
        r = lignes("Hunger Games : La Ballade du serpent et de l'oiseau chanteur")
        self.assertEqual(r[1], "Hunger Games")
        self.assertTrue(r[3] and r[4])

    def test_titre_long_sur_deux_lignes_et_sous_titre_dessous(self):
        r = lignes("La Planète des singes : Suprématie")
        self.assertEqual(r[1:5], ("La Planète", "des singes", "", "Suprématie"))

    def test_article_seul_au_dessus(self):
        self.assertEqual(lignes("Les Indestructibles 2"), ("Les", "Indestructibles", "", "", "", "2"))

    def test_numero_de_suite_en_grand_chiffre(self):
        self.assertEqual(lignes("Conjuring 2 : Le Cas Enfield"), ("", "Conjuring", "", "Le Cas Enfield", "", "2"))
        self.assertEqual(lignes("Hunger Games : La Révolte : Partie 1"), ("", "Hunger Games", "", "La Révolte", "", "1"))
        self.assertEqual(lignes("Les Gardiens de la Galaxie Vol. 2")[5], "2")
        self.assertEqual(lignes("Blade Runner 2049")[5], "")
        self.assertEqual(lignes("Breaking Bad 2", film=False)[5], "")      # séries : la saison se règle à part

    def test_mission_impossible_garde_ses_deux_points(self):
        self.assertEqual(lignes("Mission : Impossible - Fallout")[1:5], ("Mission :", "Impossible", "", "Fallout"))

    def test_star_wars_episode_retire(self):
        self.assertEqual(lignes("Star Wars, épisode IV - Un nouvel espoir")[1:4], ("Star Wars", "", "Un nouvel espoir"))

    def test_liaison_quand_le_titre_est_trop_long(self):
        self.assertEqual(lignes("Harry Potter à l'école des sorciers")[1:4], ("Harry Potter", "", "à l'école des sorciers"))


class EcritureTest(unittest.TestCase):
    def test_ligne_qui_tient_reste_normale(self):
        self.assertEqual(titre.plan_ligne("Skyfall", 89, LARGEUR, police)["mode"], "normal")

    def test_rapetisse_seulement_en_dernier_recours(self):
        plan = titre.plan_ligne("The Woman in the House Across the Street", 89, LARGEUR, police)
        self.assertEqual(plan["mode"], "reduit")
        self.assertLess(plan["taille"], 89)

    def test_barre_force_l_empilement(self):
        plan = titre.plan_ligne("Raya et/le Dragon", 89, LARGEUR, police)
        self.assertEqual((plan["mode"], plan["taille"]), ("normal", 89))
        self.assertIn(("pile", "ET", "LE"), plan["blocs"])
        self.assertIn(("mot", "AC/DC"), titre.plan_ligne("AC/DC", 89, LARGEUR, police)["blocs"])

    def test_ligne_centree_avec_les_mots_empiles(self):
        img = Image.new("L", (1000, 200), 0)
        titre.ecrire_ligne(ImageDraw.Draw(img), "Ant-Man et la Guêpe", 89, 500, 150, LARGEUR, police, couleur=255)
        boite = img.getbbox()
        self.assertLessEqual(abs((boite[0] + boite[2]) / 2 - 500), 3)


if __name__ == "__main__":
    unittest.main()
