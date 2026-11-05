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

import numpy as np  # noqa: E402
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
        rec = coherence.classer(coherence.posters_existants("Series/Ma Serie", "season03-poster.jpg"), posters,
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
        rec = coherence.classer(coherence.posters_existants("Series/Ma Serie", "season01-poster.jpg"), posters,
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
        rec = coherence.classer(coherence.posters_existants("Series/Ma Serie", "season01-poster.jpg"), posters,
                                lambda url: appels.append(url) or b"pas une image")
        self.assertEqual(rec["scores"], {})
        self.assertEqual(appels, ["https://image.tmdb.org/t/p/w185/ok_1234.jpg"])        # seul le chemin propre est demandé

    def test_sans_modele_pas_de_note_mais_le_texte_est_dit(self):
        images = {"/seul_1234.jpg": _poster((5, 10, 60), (20, 40, 120))}
        rec = coherence.classer([], [{"path": "/seul_1234.jpg", "lang": ""}], lambda url: _octets(images["/seul_1234.jpg"]))
        self.assertEqual(rec["refs"], [])
        self.assertEqual(rec["scores"]["/seul_1234.jpg"], {"texte": "sans"})


def _scene(personnage=(0.35, 0.18, 0.65, 0.80), fond=(20, 60, 140), taille=(400, 600)):
    """Poster fabriqué : fond uni bruité + une silhouette claire (rectangle) : le « personnage » à retrouver."""
    rng = np.random.default_rng(3)
    arr = np.clip(np.array(fond, dtype=np.int16) + rng.integers(-12, 12, (taille[1], taille[0], 3)), 0, 255).astype(np.uint8)
    x0, y0, x1, y1 = (int(personnage[0] * taille[0]), int(personnage[1] * taille[1]), int(personnage[2] * taille[0]), int(personnage[3] * taille[1]))
    t = np.linspace(0, 1, y1 - y0)[:, None, None]                          # le personnage a des nuances (dégradé + grain), comme une photo
    degrade = np.array((60, 40, 30)) * (1 - t) + np.array((230, 200, 170)) * t
    arr[y0:y1, x0:x1] = np.clip(degrade + rng.integers(-25, 25, (y1 - y0, x1 - x0, 3)), 0, 255).astype(np.uint8)
    return Image.fromarray(arr)


class AlignementTest(unittest.TestCase):
    def test_le_personnage_est_retrouve(self):
        su = coherence.sujet(_scene((0.35, 0.18, 0.65, 0.80)))
        self.assertIsNotNone(su)
        for cle, attendu in (("x0", 0.35), ("x1", 0.65), ("y0", 0.18), ("y1", 0.80)):
            self.assertAlmostEqual(su[cle], attendu, delta=0.04, msg=cle)

    def test_cadrage_propose_met_le_haut_et_le_milieu_a_leur_place(self):
        cible = {"haut": 0.20, "centre": 0.50, "largeur": 0.30, "bas": 0.896, "touche_bas": True, "n": 1,
                 "ecarts": {"largeur": 0.0, "haut": 0.0, "hauteur": 0.0}}
        # petit personnage décalé à gauche, en haut : zoom avant, puis son milieu et sa tête rejoignent leur place
        img = _scene((0.15, 0.10, 0.45, 0.50), taille=(400, 600))
        props = {p["mode"]: p for p in coherence.proposer(img, cible, 22, conseille="largeur")}
        self.assertEqual(set(props), {"largeur", "hauteur", "tete"})
        self.assertEqual(sum(1 for p in props.values() if p["conseille"]), 1)
        for p in props.values():
            self.assertTrue(0.5 <= p["zoom"] <= 3.0 and 0.0 <= p["crop_x"] <= 1.0 and 0.0 <= p["crop_y"] <= 1.0)
        # même largeur : le personnage (30 % de l'image) doit faire 30 % du poster fini
        self.assertAlmostEqual(props["largeur"]["zoom"] * max(956 / 400, 1456 / 600) * 400 * 0.30 / 1000, 0.30, delta=0.05)
        # même hauteur : tête à 20 %, pieds en bas ; milieu et tête rejoignent leur place (zoom avant : il y a de la marge)
        self.assertGreater(props["hauteur"]["zoom"], 1.2)
        self.assertLess(abs(props["hauteur"]["ecart_centre"]), 30)
        self.assertLess(abs(props["hauteur"]["ecart_haut"]), 30)

    def test_modele_regulier_conseille_la_largeur_sinon_la_hauteur(self):
        reguliers = [{"haut": 0.2, "centre": 0.5, "largeur": w, "bas": 0.896, "touche_bas": True} for w in (0.5, 0.8, 0.95)]
        self.assertEqual(coherence.conseiller(coherence.resume_modeles(reguliers)), "hauteur")   # largeurs très différentes, tous touchent le bas
        variables = [{"haut": 0.2, "centre": 0.5, "largeur": 0.80, "bas": 0.5, "touche_bas": False},
                     {"haut": 0.2, "centre": 0.5, "largeur": 0.80, "bas": 0.8, "touche_bas": False},
                     {"haut": 0.2, "centre": 0.5, "largeur": 0.79, "bas": 0.9, "touche_bas": False}]
        self.assertEqual(coherence.conseiller(coherence.resume_modeles(variables)), "largeur")

    def test_un_logo_seul_n_est_pas_un_personnage(self):
        logo = Image.new("RGB", (400, 600), (0, 0, 0))
        ImageDraw.Draw(logo).rectangle([40, 250, 360, 350], fill=(250, 200, 0))
        self.assertFalse(coherence.sujet(logo)["riche"])
        self.assertTrue(coherence.sujet(_scene())["riche"])
        cible = {"haut": 0.2, "centre": 0.5, "largeur": 0.5, "bas": 0.9, "touche_bas": True, "n": 1, "ecarts": {}}
        props = coherence.proposer(logo, cible, 22, conseille="largeur")
        self.assertTrue(props and not any(p["sur"] or p["acceptable"] for p in props))      # jamais appliqué tout seul

    def test_image_sans_personnage_net_ne_plante_pas(self):
        uni = Image.new("RGB", (300, 450), (40, 40, 40))
        self.assertEqual(coherence.proposer(uni, {"haut": 0.2, "centre": 0.5, "largeur": 0.5, "bas": 0.9, "touche_bas": True,
                                                  "ecarts": {}, "n": 1}, 22), [])

    def test_modeles_mesures_depuis_le_dossier(self):
        with tempfile.TemporaryDirectory() as tmp:
            racine = Path(tmp)
            dossier = racine / "Series" / "S"
            dossier.mkdir(parents=True)
            fini = _scene((0.2, 0.1, 0.8, 0.9), taille=(300, 450))
            fini.save(dossier / "season01-poster.jpg")
            ancienne = library.LIBRARY_ROOT
            library.LIBRARY_ROOT = racine
            try:
                coherence._cache_sujets.clear()
                m = coherence.modeles_mesures([{"rel": "Series/S", "name": "season01-poster.jpg", "label": "Saison 1"}])
            finally:
                library.LIBRARY_ROOT = ancienne
        self.assertEqual(len(m), 1)
        self.assertAlmostEqual(m[0]["centre"], 0.5, delta=0.05)


class RouteTest(Base):
    def test_routes_reservees_aux_comptes_connectes(self):
        for url in ("/api/coherence", "/api/coherence/aligner"):
            r = self.c.post(url, json={"media_type": "tv"})
            self.assertIn(r.status_code, (302, 401, 403))

    def test_titre_inconnu_ou_sans_modele_ne_plante_pas(self):
        self.connecter()
        vide = self.c.post("/api/coherence", json={"media_type": "libre", "tmdb_id": 1}).get_json()
        self.assertEqual((vide["refs"], vide["scores"]), ([], {}))
        with mock.patch.object(self.A, "_dossier_de_la_serie", return_value=None), \
                mock.patch.object(self.A.library, "LIBRARY_ROOT", Path(tempfile.gettempdir())):
            self.A._CACHE_REFS.clear()
            r = self.c.post("/api/coherence", json={"media_type": "tv", "tmdb_id": 1, "title": "Rien", "posters": []}).get_json()
        self.assertEqual(r["refs"], [])
        self.assertIn("introuvable", r["message"])

    def test_serie_la_saison_choisie_est_exclue_des_modeles(self):
        self.A._CACHE_REFS.clear()
        vu = {}
        with mock.patch.object(self.A, "_dossier_de_la_serie", return_value="Series/X"), \
                mock.patch.object(self.A.coherence, "posters_existants", side_effect=lambda rel, cible: vu.update(rel=rel, cible=cible) or []):
            refs, message, cible = self.A._references_de("tv", 5, ["X"], 2020, "3")
        self.assertEqual((vu, cible), ({"rel": "Series/X", "cible": "season03-poster.jpg"}, "season03-poster.jpg"))
        self.assertIn("Aucun poster", message)

    def test_film_d_une_saga_les_autres_films_servent_de_modeles(self):
        self.A._CACHE_REFS.clear()
        with tempfile.TemporaryDirectory() as tmp:
            racine = Path(tmp)
            for nom in ("Un (2019)", "Deux (2022)"):
                (racine / "Films HD" / nom).mkdir(parents=True)
                _scene().save(racine / "Films HD" / nom / "poster.jpg")
            (racine / "Films HD" / "Trois (2025)").mkdir(parents=True)                      # pas de poster : ignoré
            parts = [{"id": 1, "title": "Un", "original_title": "One", "year": 2019},
                     {"id": 2, "title": "Deux", "original_title": "Two", "year": 2022},
                     {"id": 3, "title": "Trois", "original_title": "Three", "year": 2025}]
            dossiers = {1: "Films HD/Un (2019)", 2: "Films HD/Deux (2022)", 3: "Films HD/Trois (2025)"}
            with mock.patch.object(self.A.library, "LIBRARY_ROOT", racine), \
                    mock.patch.object(self.A, "_parts_de_la_saga", return_value=parts), \
                    mock.patch.object(self.A, "_dossier_de_la_serie", side_effect=lambda t, i, ti, y: dossiers[i]):
                refs, message, cible = self.A._references_de("movie", 2, ["Deux"], 2022, "")
        self.assertEqual([r["label"] for r in refs], ["Un (2019)"])                       # lui-même (2) et le film sans poster (3) exclus
        self.assertEqual(cible, "poster.jpg")

    def test_film_hors_saga(self):
        self.A._CACHE_REFS.clear()
        with mock.patch.object(self.A, "_parts_de_la_saga", return_value=[]):
            refs, message, _ = self.A._references_de("movie", 9, ["Seul"], 2020, "")
        self.assertEqual(refs, [])
        self.assertIn("aucune saga", message)

    def test_aligner_renvoie_des_propositions(self):
        self.connecter()
        self.A._CACHE_REFS[("tv", 7, "poster.jpg")] = (__import__("time").monotonic(), [{"rel": "Series/X", "name": "poster.jpg", "label": "Série"}], "")
        cible = {"haut": 0.2, "centre": 0.5, "largeur": 0.3, "bas": 0.9, "touche_bas": True, "n": 1, "ecarts": {"largeur": 0.0, "haut": 0.0, "hauteur": 0.0}}
        with mock.patch.object(self.A.coherence, "modeles_mesures", return_value=[{}]), \
                mock.patch.object(self.A.coherence, "resume_modeles", return_value=cible), \
                mock.patch.object(self.A, "load_source_image", return_value=_scene((0.3, 0.1, 0.7, 0.9), taille=(500, 750))):
            r = self.c.post("/api/coherence/aligner", json={"media_type": "tv", "tmdb_id": 7, "poster_url": "https://image.tmdb.org/t/p/w500/x.jpg"}).get_json()
        self.assertEqual({p["mode"] for p in r["propositions"]}, {"largeur", "hauteur", "tete"})
        self.assertEqual(r["modeles"], 1)

    def test_la_page_s_affiche_avec_la_bande_la_barre_et_l_alignement(self):
        self.connecter()
        page = self.c.get("/").get_data(as_text=True)
        for repere in ('id="cohBande"', 'id="cohTri"', 'id="cohProgres"', 'id="cohAuto"', "chargerCoherence", "cohAligner", "cohProgres("):
            self.assertIn(repere, page)


if __name__ == "__main__":
    unittest.main()
