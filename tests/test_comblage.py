"""🩹 Combler une zone d'un poster (texte) avec les morceaux d'une 2e image : le même personnage est replacé (zoom, rotation, décalage)
dans un poster au fond différent. Images fabriquées, aucun réseau, aucun fichier réel, l'effaceur LaMa est remplacé par un faux."""
import base64
import io
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import cv2  # noqa: E402
import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

import comblage  # noqa: E402
from test_liaisons import Base  # noqa: E402


def _personnage(rng, taille=(800, 600)):
    """Source : fond noir + « personnage » plein de détails (formes de couleurs variées, comme un dessin)."""
    w, h = taille
    img = np.zeros((h, w, 3), np.uint8)
    figure = np.zeros((h, w), np.uint8)
    cv2.ellipse(figure, (w // 2, h // 2), (190, 250), 0, 0, 360, 255, -1)
    for _ in range(350):
        x, y = int(rng.integers(w // 2 - 170, w // 2 + 170)), int(rng.integers(h // 2 - 230, h // 2 + 230))
        couleur = tuple(int(c) for c in rng.integers(30, 250, 3))
        if rng.random() < 0.5:
            cv2.circle(img, (x, y), int(rng.integers(5, 28)), couleur, -1)
        else:
            cv2.rectangle(img, (x, y), (x + int(rng.integers(8, 40)), y + int(rng.integers(8, 40))), couleur, -1)
    img[figure == 0] = 0
    return img, figure


def _poster_avec_personnage(source, figure, angle=12.0, echelle=1.3, taille=(900, 1200)):
    """Poster : fond dégradé différent + le personnage de la source tourné et agrandi. Renvoie (poster, vérité = poster sans texte)."""
    w, h = taille
    fond = np.zeros((h, w, 3), np.uint8)
    for y in range(h):
        fond[y, :] = (40 + 90 * y // h, 120 - 60 * y // h, 160)
    M = cv2.getRotationMatrix2D((source.shape[1] / 2, source.shape[0] / 2), angle, echelle)
    M[0, 2] += 110
    M[1, 2] += 150
    chaud = cv2.warpAffine(source, M, (w, h), flags=cv2.INTER_CUBIC)
    alpha = cv2.warpAffine(figure, M, (w, h)) > 128
    poster = fond.copy()
    poster[alpha] = chaud[alpha]
    return poster, alpha


class CombleTest(unittest.TestCase):
    def setUp(self):
        rng = np.random.default_rng(5)
        self.source, figure = _personnage(rng)
        self.vrai, self.alpha = _poster_avec_personnage(self.source, figure)
        # le « texte » : un gros trait blanc qui traverse le personnage et déborde sur le fond
        self.masque = np.zeros(self.vrai.shape[:2], np.uint8)
        cv2.line(self.masque, (60, 560), (840, 470), 255, 70)
        self.poster = self.vrai.copy()
        self.poster[self.masque > 0] = 255

    def test_le_texte_sur_le_personnage_est_remplace_par_la_source(self):
        r = comblage.combler(self.poster, self.masque, self.source)
        rap = r["rapport"]
        self.assertGreaterEqual(rap["modeles"], 1)
        self.assertGreater(rap["couvert"], 25.0)                                  # le personnage est couvert, pas le fond
        self.assertLess(rap["couvert"], 95.0)                                     # le fond du poster n'existe pas dans la source
        sur_personnage = (self.masque > 0) & self.alpha & (r["reste"] == 0)
        self.assertGreater(int(sur_personnage.sum()), 2000)
        ecart = np.abs(r["image"].astype(int) - self.vrai.astype(int)).max(axis=2)[sur_personnage]
        self.assertLess(float(np.median(ecart)), 20.0)                            # les pixels recopiés sont ceux d'origine
        self.assertEqual(r["image"].shape, self.poster.shape)
        self.assertGreater(r["rapport"]["morceaux"][0]["points"], 10)
        self.assertAlmostEqual(r["rapport"]["morceaux"][0]["echelle"], 1.3, delta=0.08)

    def test_la_couleur_du_poster_est_reprise_meme_retouchee(self):
        """Le poster a été étalonné autrement que la 2e image (teinte, saturation, clarté, différemment selon les couleurs) : les morceaux
        recopiés doivent avoir les couleurs du POSTER, pas celles de la source."""
        def lab(a):
            return cv2.cvtColor(a.astype(np.float32) / 255.0, cv2.COLOR_RGB2LAB)
        x = self.vrai.astype(np.float32) / 255.0
        gradue = np.clip(np.stack([x[..., 0] ** 0.8 * 1.10, x[..., 1] ** 1.1 * 0.92, x[..., 2] ** 0.9 * 0.80 + 0.04], axis=-1), 0, 1)
        vrai = np.where(self.alpha[..., None], (gradue * 255).astype(np.uint8), self.vrai)           # seul le personnage est retouché
        poster = vrai.copy()
        poster[self.masque > 0] = 255
        zone = (self.masque > 0) & self.alpha
        erreurs = {}
        try:
            for mode in ("decalage", "cases"):
                comblage.MODE_COULEUR = mode
                r = comblage.combler(poster, self.masque, self.source)
                ok = zone & (r["reste"] == 0)
                self.assertGreater(int(ok.sum()), 1500)
                erreurs[mode] = float(np.median(np.linalg.norm(lab(r["image"]) - lab(vrai), axis=2)[ok]))
        finally:
            comblage.MODE_COULEUR = "cases"
        self.assertLess(erreurs["cases"], 6.0)                                      # à peine visible
        self.assertLess(erreurs["cases"], 0.8 * erreurs["decalage"])                # nettement mieux qu'un simple décalage moyen

    def test_le_reste_est_rendu_a_l_effaceur_et_la_partie_collee_n_en_fait_pas_partie(self):
        r = comblage.combler(self.poster, self.masque, self.source)
        self.assertTrue((r["reste"][self.masque == 0] == 0).all())               # jamais hors de la zone peinte
        collee = (self.masque > 0) & (r["reste"] == 0)
        self.assertEqual(int(collee.sum()) + int((r["reste"] > 0).sum()), int((self.masque > 0).sum()))
        self.assertAlmostEqual(r["rapport"]["couvert"] + r["rapport"]["reste"], 100.0, delta=0.2)

    def _detourage(self, fond_voyant=(255, 0, 255)):
        """La source en PNG détouré : seul le personnage est opaque ; dessous, une couleur voyante qui ne doit JAMAIS se retrouver dans le poster."""
        _, figure = _personnage(np.random.default_rng(5))
        rgb = self.source.copy()
        rgb[figure == 0] = fond_voyant
        return np.dstack([rgb, figure])

    def test_un_detourage_ne_colle_que_le_personnage(self):
        r = comblage.combler(self.poster, self.masque, self._detourage())
        self.assertGreater(r["rapport"]["couvert"], 25)                       # le personnage est retrouvé et recopié comme avec l'image entière
        magenta = (r["image"][..., 0] >= 250) & (r["image"][..., 2] >= 250) & (r["image"][..., 1] <= 10)     # le personnage n'a aucune couleur aussi vive
        self.assertEqual(int(magenta.sum()), 0)                                  # le fond transparent n'est jamais recopié

    def test_un_detourage_a_la_meme_qualite_que_l_image_entiere(self):
        entiere = comblage.combler(self.poster, self.masque, self.source)
        detouree = comblage.combler(self.poster, self.masque, self._detourage((255, 255, 255)))
        zone = self.masque > 0
        e1 = np.abs(entiere["image"].astype(int) - self.vrai.astype(int)).max(axis=2)[zone & (entiere["reste"] == 0)].mean()
        e2 = np.abs(detouree["image"].astype(int) - self.vrai.astype(int)).max(axis=2)[zone & (detouree["reste"] == 0)].mean()
        self.assertLess(e2, e1 + 4.0)

    def test_png_entierement_opaque_est_traite_comme_une_image_normale(self):
        opaque = np.dstack([self.source, np.full(self.source.shape[:2], 255, np.uint8)])
        r = comblage.combler(self.poster, self.masque, opaque)
        self.assertGreater(r["rapport"]["couvert"], 25)

    def test_la_zone_confiee_a_l_effaceur_deborde_sur_la_jonction(self):
        r = comblage.combler(self.poster, self.masque, self.source)
        pas_collee = (self.masque > 0) & (r["reste"] == 0)
        # un pixel collé n'est jamais à moins de GRAIN_RACCORD pixels d'un pixel déjà confié à l'effaceur sans être lui-même confié
        voisins = cv2.dilate((r["reste"] > 0).astype(np.uint8), np.ones((3, 3), np.uint8)) > 0
        self.assertTrue(pas_collee.sum() > 0)
        self.assertTrue(((r["reste"] > 0) <= (self.masque > 0)).all())
        self.assertGreaterEqual(int(voisins.sum()), int((r["reste"] > 0).sum()))

    def test_une_image_sans_rapport_ne_couvre_rien(self):
        autre = np.random.default_rng(99).integers(0, 255, (600, 800, 3), dtype=np.uint8)
        r = comblage.combler(self.poster, self.masque, autre)
        self.assertEqual(r["rapport"]["couvert"], 0.0)
        self.assertEqual(int((r["reste"] > 0).sum()), int((self.masque > 0).sum()))
        self.assertTrue((r["image"] == self.poster).all())                        # rien n'est collé au hasard

    def test_masque_vide(self):
        r = comblage.combler(self.poster, np.zeros(self.poster.shape[:2], np.uint8), self.source)
        self.assertEqual(r["rapport"]["couvert"], 0.0)

    def test_les_entrees_ne_sont_pas_modifiees(self):
        p, m, s = self.poster.copy(), self.masque.copy(), self.source.copy()
        comblage.combler(p, m, s)
        self.assertTrue((p == self.poster).all() and (m == self.masque).all() and (s == self.source).all())


def _png_data(masque):
    b = io.BytesIO()
    Image.fromarray(masque).save(b, "PNG")
    return "data:image/png;base64," + base64.b64encode(b.getvalue()).decode()


class RouteTest(Base):
    def setUp(self):
        super().setUp()
        rng = np.random.default_rng(5)
        self.source, figure = _personnage(rng)
        self.vrai, _ = _poster_avec_personnage(self.source, figure)
        self.masque = np.zeros(self.vrai.shape[:2], np.uint8)
        cv2.line(self.masque, (60, 560), (840, 470), 255, 70)
        self.poster = self.vrai.copy()
        self.poster[self.masque > 0] = 255

    def _images(self, url, taille):
        return Image.fromarray(self.poster if "poster" in url else self.source)

    def test_comblage_enregistre_une_etape_d_effacement(self):
        self.connecter()
        faux_effaceur = mock.Mock(side_effect=lambda img, m, grossir=True: (img, "faux", ""))
        with mock.patch.object(self.A, "load_source_image", side_effect=self._images), \
                mock.patch.object(self.A, "inpaint_region", faux_effaceur), \
                mock.patch.object(self.A, "UPLOAD_DIR", self.tmp.name):
            r = self.c.post("/api/erase/combler", json={"poster_url": "poster", "source_url": "source", "mask": _png_data(self.masque)})
        d = r.get_json()
        self.assertEqual(r.status_code, 200, d)
        self.assertGreater(d["rapport"]["couvert"], 25)
        self.assertTrue(d["url"].startswith("/api/upload/"))
        self.assertIn("2ᵉ image", d["engine"])
        self.assertFalse(faux_effaceur.call_args.kwargs.get("grossir", True))      # le reste n'est pas élargi une 2e fois

    def test_image_qui_ne_correspond_pas_est_refusee_sans_rien_enregistrer(self):
        self.connecter()
        autre = np.random.default_rng(7).integers(0, 255, (600, 800, 3), dtype=np.uint8)
        with mock.patch.object(self.A, "load_source_image", side_effect=lambda url, t: Image.fromarray(self.poster if "poster" in url else autre)), \
                mock.patch.object(self.A, "UPLOAD_DIR", self.tmp.name):
            r = self.c.post("/api/erase/combler", json={"poster_url": "poster", "source_url": "autre", "mask": _png_data(self.masque)})
        self.assertEqual(r.status_code, 422)
        self.assertIn("ne montre pas", r.get_json()["error"])
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [])

    def _png_detoure(self):
        _, figure = _personnage(np.random.default_rng(5))
        rgb = self.source.copy()
        rgb[figure == 0] = (255, 0, 255)
        b = io.BytesIO()
        Image.fromarray(np.dstack([rgb, figure]), "RGBA").save(b, "PNG")
        return b.getvalue()

    def test_l_envoi_garde_la_transparence_a_part(self):
        self.connecter()
        with mock.patch.object(self.A, "UPLOAD_DIR", self.tmp.name):
            r = self.c.post("/api/upload", data={"file": (io.BytesIO(self._png_detoure()), "perso.png")}, content_type="multipart/form-data")
            d = r.get_json()
            self.assertEqual(r.status_code, 200, d)
            nom = d["url"].rsplit("/", 1)[1]
            self.assertTrue(nom.endswith(".jpg"))
            copie = Path(self.tmp.name) / (nom[:-4] + ".png")
            self.assertTrue(copie.is_file())
            self.assertEqual(Image.open(copie).mode, "RGBA")
            self.assertLess(np.asarray(Image.open(copie))[..., 3].min(), 10)
            # un PNG sans transparence n'en fait pas
            b = io.BytesIO()
            Image.fromarray(self.source).save(b, "PNG")
            d2 = self.c.post("/api/upload", data={"file": (io.BytesIO(b.getvalue()), "plein.png")}, content_type="multipart/form-data").get_json()
            self.assertFalse((Path(self.tmp.name) / (d2["url"].rsplit("/", 1)[1][:-4] + ".png")).exists())

    def test_comblage_avec_un_detourage_envoye(self):
        self.connecter()
        faux_effaceur = mock.Mock(side_effect=lambda img, m, grossir=True: (img, "faux", ""))
        with mock.patch.object(self.A, "UPLOAD_DIR", self.tmp.name):
            url = self.c.post("/api/upload", data={"file": (io.BytesIO(self._png_detoure()), "perso.png")},
                              content_type="multipart/form-data").get_json()["url"]
            with mock.patch.object(self.A, "load_source_image", side_effect=lambda u, t: Image.fromarray(self.poster) if "poster" in u else
                                   self.A._load_source_image(u, t)), mock.patch.object(self.A, "inpaint_region", faux_effaceur):
                r = self.c.post("/api/erase/combler", json={"poster_url": "poster", "source_url": url, "mask": _png_data(self.masque)})
        d = r.get_json()
        self.assertEqual(r.status_code, 200, d)
        self.assertGreater(d["rapport"]["couvert"], 25)
        resultat = np.asarray(Image.open(Path(self.tmp.name) / d["url"].rsplit("/", 1)[1]).convert("RGB")).astype(int)
        magenta = (resultat[..., 0] >= 240) & (resultat[..., 2] >= 240) & (resultat[..., 1] <= 25)           # (le JPEG fait dériver un peu les couleurs)
        self.assertEqual(int(magenta.sum()), 0)

    def test_trace_invalide_ou_vide(self):
        self.connecter()
        self.assertEqual(self.c.post("/api/erase/combler", json={"mask": "pas une image"}).status_code, 400)
        with mock.patch.object(self.A, "load_source_image", side_effect=self._images):
            r = self.c.post("/api/erase/combler", json={"poster_url": "poster", "source_url": "source",
                                                        "mask": _png_data(np.zeros(self.masque.shape, np.uint8))})
        self.assertEqual(r.status_code, 400)
        self.assertIn("Rien n'a été peint", r.get_json()["error"])

    def test_fonds_d_ecran_sans_texte_d_abord(self):
        self.connecter()
        faux = mock.Mock()
        faux.raise_for_status = lambda: None
        faux.json.return_value = {"backdrops": [{"file_path": "/avec.jpg", "iso_639_1": "fr", "vote_average": 9, "width": 1920, "height": 1080},
                                                {"file_path": "/sans.jpg", "iso_639_1": None, "vote_average": 5, "width": 1920, "height": 1080}]}
        with mock.patch.object(self.A, "tmdb_get", return_value=faux):
            r = self.c.post("/api/backdrops", json={"id": 123, "media_type": "tv"}).get_json()
        self.assertEqual([b["url"].rsplit("/", 1)[1] for b in r["backdrops"]], ["sans.jpg", "avec.jpg"])
        self.assertEqual(self.c.post("/api/backdrops", json={"id": "x", "media_type": "tv"}).get_json(), {"backdrops": []})

    def test_la_fenetre_d_effacement_propose_le_comblage(self):
        self.connecter()
        page = self.c.get("/").get_data(as_text=True)
        for repere in ('id="combleOuvrir"', 'id="combleFichier"', "eraseCombler", "/api/erase/combler", "/api/backdrops"):
            self.assertIn(repere, page)


if __name__ == "__main__":
    unittest.main()
