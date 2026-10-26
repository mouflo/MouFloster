"""Compression gzip des réponses (compression.py, fichier identique dans les quatre applis) : seulement le texte assez gros,
jamais les images, les fichiers envoyés tels quels ni les réponses partielles, et seulement si le navigateur l'accepte."""
import gzip
import io
import sys
import unittest
from pathlib import Path

from flask import Flask, Response, jsonify, send_file

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import compression  # noqa: E402

GROS = {"liste": [{"nom": f"Titre {i}", "theme": i % 2 == 0} for i in range(400)]}


class CompressionTest(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__)
        compression.init_app(app)

        @app.after_request
        def apres(r):                                  # une autre fonction « après » : la compression passe après elle
            r.headers["X-Vu"] = "1"
            return r
        app.add_url_rule("/gros", "gros", lambda: jsonify(GROS))
        app.add_url_rule("/petit", "petit", lambda: jsonify({"ok": True}))
        app.add_url_rule("/image", "image", lambda: Response(b"\xff\xd8" + b"x" * 5000, mimetype="image/jpeg"))
        app.add_url_rule("/fichier", "fichier", lambda: send_file(io.BytesIO(b"a" * 5000), mimetype="text/plain"))
        app.add_url_rule("/flux", "flux", lambda: Response((b"ligne\n" * 50 for _ in range(10)), mimetype="text/plain"))
        app.add_url_rule("/partiel", "partiel", lambda: Response(b"a" * 5000, status=206, mimetype="text/plain"))
        self.c = app.test_client()

    def test_texte_compresse(self):
        r = self.c.get("/gros", headers={"Accept-Encoding": "gzip, deflate, br"})
        self.assertEqual(r.headers.get("Content-Encoding"), "gzip")
        self.assertIn("Accept-Encoding", r.headers.get("Vary", ""))
        brut = gzip.decompress(r.data)
        self.assertEqual(len(r.data) < len(brut) / 4, True)
        self.assertEqual(int(r.headers["Content-Length"]), len(r.data))
        self.assertEqual(r.headers.get("X-Vu"), "1")

    def test_rien_si_le_navigateur_ne_le_demande_pas(self):
        self.assertNotIn("Content-Encoding", self.c.get("/gros").headers)

    def test_jamais_petit_image_fichier_flux_partiel(self):
        for chemin in ("/petit", "/image", "/fichier", "/flux", "/partiel"):
            with self.subTest(chemin):
                self.assertNotIn("Content-Encoding", self.c.get(chemin, headers={"Accept-Encoding": "gzip"}).headers)


if __name__ == "__main__":
    unittest.main()
