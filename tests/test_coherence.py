"""🎯 Cohérence des posters d'une série : posters déjà présents dans le dossier (faits avec MouFloster ou non), classement des
posters proposés par ressemblance, texte probable. Images fabriquées, aucun réseau, aucune vraie médiathèque."""
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PIL import Image, ImageDraw  # noqa: E402

import coherence  # noqa: E402
import library  # noqa: E402
from test_liaisons import Base  # noqa: E402


def _poster(haut, bas, texte=False, taille=(200, 300)):
    """Poster fabriqué : dégradé vertical haut → bas, avec (ou sans) un titre blanc dans le bas."""
    img = Image.new("RGB", taille)
    d = ImageDraw.Draw(img)
    for y in range(taille[1]):
        t = y / (taille[1] - 1)
        d.line([(0, y), (taille[0], y)], fill=tuple(int(haut[i] * (1 - t) + bas[i] * t) for i in range(3)))
    if texte:
        for k in range(6):
            d.rectangle([20 + k * 28, 250, 36 + k * 28, 280], fill="white")
    return img


def _octets(img):
    b = io.BytesIO()
    img.save(b, "JPEG", quality=92)
    return b.getvalue()


class MediathequeFausse(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.racine = Path(self.tmp.name)
        self.serie = self.racine / "Series" / "Ma Serie"
        self.serie.mkdir(parents=True)
        self._ancienne = library.LIBRARY_ROOT
        library.LIBRARY_ROOT = self.racine
        coherence._cache_empreintes.clear()

    def tearDown(self):
        library.LIBRARY_ROOT = self._ancienne
        self.tmp.cleanup()

    def poser(self, nom, img):
        img.save(self.serie / nom, "JPEG", quality=92)


class PostersExistantsTest(MediathequeFausse):
    def test_tous_les_posters_du_dossier_serie_puis_saisons_dans_l_ordre(self):
        for nom in ("season02-poster.jpg", "poster.jpg", "season01-poster.jpg", "season-specials-poster.jpg"):
            self.poser(nom, _poster((10, 10, 10), (200, 200, 0)))
        (self.serie / "fanart.jpg").write_bytes(b"x")                     # pas un poster
        (self.serie / "season03-poster-new.jpg").write_bytes(b"x")        # copie de sauvegarde « -new » : pas un modèle
        noms = [p["name"] for p in coherence.posters_existants("Series/Ma Serie")]
        self.assertEqual(noms, ["poster.jpg", "season01-poster.jpg", "season02-poster.jpg", "season-specials-poster.jpg"])
        self.assertEqual([p["label"] for p in coherence.posters_existants("Series/Ma Serie")][:3], ["Série", "Saison 1", "Saison 2"])

    def test_le_poster_en_cours_de_refonte_ne_sert_pas_de_modele(self):
        for nom in ("poster.jpg", "season01-poster.jpg", "season03-poster.jpg"):
            self.poser(nom, _poster((10, 10, 10), (200, 200, 0)))
        noms = [p["name"] for p in coherence.posters_existants("Series/Ma Serie", exclure="season03-poster.jpg")]
        self.assertEqual(noms, ["poster.jpg", "season01-poster.jpg"])

    def test_dossier_inconnu_ou_dehors(self):
        self.assertEqual(coherence.posters_existants("Series/Absent"), [])
        self.assertEqual(coherence.posters_existants("../../etc"), [])


class ClassementTest(MediathequeFausse):
    def test_le_poster_de_meme_style_passe_devant(self):
        # la série : deux saisons bleu nuit
        self.poser("season01-poster.jpg", _poster((5, 10, 60), (20, 40, 120)))
        self.poser("season02-poster.jpg", _poster((8, 12, 70), (25, 45, 130)))
        images = {"/bleu_proche_1.jpg": _poster((6, 11, 65), (22, 42, 125)),
                  "/jaune_clair_2.jpg": _poster((250, 230, 60), (255, 250, 150)),
                  "/rouge_sombre_3.jpg": _poster((60, 5, 5), (140, 20, 20))}
        posters = [{"path": p, "lang": ""} for p in images]
        rec = coherence.classer("Series/Ma Serie", "season03-poster.jpg", posters,
                                lambda url: _octets(images["/" + url.rsplit("/", 1)[1]]))
        self.assertEqual([r["label"] for r in rec["refs"]], ["Saison 1", "Saison 2"])
        classes = sorted(rec["scores"], key=lambda p: -rec["scores"][p]["rang"])
        self.assertEqual(classes[0], "/bleu_proche_1.jpg")
        self.assertGreater(rec["scores"]["/bleu_proche_1.jpg"]["score"], rec["scores"]["/jaune_clair_2.jpg"]["score"] + 20)
        self.assertIn("bleus", rec["scores"]["/bleu_proche_1.jpg"]["raison"])
        self.assertIn(rec["scores"]["/bleu_proche_1.jpg"]["proche_de"], ("Saison 1", "Saison 2"))

    def test_un_poster_sans_texte_passe_devant_un_texte_difficile_de_meme_style(self):
        self.poser("poster.jpg", _poster((5, 10, 60), (20, 40, 120)))
        propre = _poster((6, 11, 65), (22, 42, 125))
        charge = _poster((6, 11, 65), (22, 42, 125), texte=True)
        images = {"/propre_1.jpg": propre, "/charge_2.jpg": charge}
        posters = [{"path": "/propre_1.jpg", "lang": ""}, {"path": "/charge_2.jpg", "lang": "fr"}]
        rec = coherence.classer("Series/Ma Serie", "season01-poster.jpg", posters,
                                lambda url: _octets(images["/" + url.rsplit("/", 1)[1]]))
        self.assertEqual(rec["scores"]["/propre_1.jpg"]["texte"], "sans")
        self.assertGreater(rec["scores"]["/propre_1.jpg"]["rang"], rec["scores"]["/charge_2.jpg"]["rang"])

    def test_texte_sur_fond_lisse_facile_sur_fond_charge_difficile(self):
        lisse = _poster((10, 10, 10), (30, 30, 30), texte=True)
        self.assertEqual(coherence.texte_probable(lisse, "fr"), "facile")
        bruit = Image.effect_noise((200, 300), 90).convert("RGB")           # fond très chargé
        d = ImageDraw.Draw(bruit)
        for k in range(6):
            d.rectangle([20 + k * 28, 250, 36 + k * 28, 280], fill="white")
        self.assertIn(coherence.texte_probable(bruit, "fr"), ("moyen", "difficile"))
        self.assertEqual(coherence.texte_probable(bruit, ""), "sans")        # TMDB dit « sans texte » : on le croit

    def test_image_illisible_ou_chemin_louche_ignores(self):
        self.poser("poster.jpg", _poster((5, 10, 60), (20, 40, 120)))
        posters = [{"path": "/ok_1234.jpg", "lang": ""}, {"path": "/../../etc/passwd", "lang": ""}, {"path": "https://autre.site/x.jpg", "lang": ""}]
        appels = []
        rec = coherence.classer("Series/Ma Serie", "season01-poster.jpg", posters, lambda url: appels.append(url) or b"pas une image")
        self.assertEqual(rec["scores"], {})
        self.assertEqual(appels, ["https://image.tmdb.org/t/p/w185/ok_1234.jpg"])        # seul le chemin propre est demandé

    def test_sans_modele_pas_de_note_mais_le_texte_est_dit(self):
        images = {"/seul_1234.jpg": _poster((5, 10, 60), (20, 40, 120))}
        rec = coherence.classer("Series/Ma Serie", "poster.jpg", [{"path": "/seul_1234.jpg", "lang": ""}],
                                lambda url: _octets(images["/seul_1234.jpg"]))
        self.assertEqual(rec["refs"], [])
        self.assertEqual(rec["scores"]["/seul_1234.jpg"], {"texte": "sans"})


class RouteTest(Base):
    def test_route_reservee_aux_comptes_connectes(self):
        r = self.c.post("/api/coherence", json={"media_type": "tv"})
        self.assertIn(r.status_code, (302, 401, 403))

    def test_film_ou_dossier_introuvable_ne_plante_pas(self):
        self.connecter()
        film = self.c.post("/api/coherence", json={"media_type": "movie", "tmdb_id": 1}).get_json()
        self.assertEqual((film["refs"], film["scores"]), ([], {}))
        with mock.patch.object(self.A, "_dossier_de_la_serie", return_value=None):
            r = self.c.post("/api/coherence", json={"media_type": "tv", "tmdb_id": 1, "title": "Rien", "posters": []})
        self.assertEqual(r.get_json()["rel"], None)
        self.assertIn("introuvable", r.get_json()["message"])

    def test_la_saison_choisie_est_exclue_des_modeles(self):
        self.connecter()
        vu = {}

        def faux(rel, cible, posters, telecharger):
            vu.update(rel=rel, cible=cible)
            return {"refs": [], "scores": {}}
        with mock.patch.object(self.A, "_dossier_de_la_serie", return_value="Series/X"), \
                mock.patch.object(self.A.library, "LIBRARY_ROOT", Path(self.tmp.name)), \
                mock.patch.object(self.A.coherence, "classer", side_effect=faux):
            self.c.post("/api/coherence", json={"media_type": "tv", "tmdb_id": 5, "season_text": "3", "posters": []})
        self.assertEqual(vu, {"rel": "Series/X", "cible": "season03-poster.jpg"})

    def test_la_page_s_affiche_avec_la_bande_et_le_bouton(self):
        self.connecter()
        page = self.c.get("/").get_data(as_text=True)
        for repere in ('id="cohBande"', 'id="cohTri"', "chargerCoherence", "cohDessiner"):
            self.assertIn(repere, page)


if __name__ == "__main__":
    unittest.main()
