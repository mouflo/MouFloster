"""🔒 Toutes les adresses de l'appli, essayées une par une : un copain n'atteint que sa liste (COPAIN_OK et
COPAIN_PREFIXES), une personne non connectée n'atteint que la page de connexion et les fichiers publics."""
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PUBLIQUES = ("/login", "/healthz", "/favicon.ico")


def _adresses(app):
    for regle in app.url_map.iter_rules():
        if regle.endpoint == "static":
            continue
        chemin = regle.rule
        for nom, conv in regle._converters.items():
            valeur = "1" if conv.__class__.__name__ in ("IntegerConverter", "FloatConverter") else "x"
            chemin = re.sub(r"<(?:\w+:)?%s>" % nom, valeur, chemin)
        for m in sorted((regle.methods or set()) - {"HEAD", "OPTIONS"}):
            yield m, chemin, regle.rule


class SecuriteTest(unittest.TestCase):
    def setUp(self):
        from werkzeug.security import generate_password_hash
        self.tmp = tempfile.TemporaryDirectory()
        self._env = {k: os.environ.get(k) for k in ("APP_USER", "APP_PASSWORD_HASH")}
        import auth
        import app as A
        os.environ.update(APP_USER="admin", APP_PASSWORD_HASH=generate_password_hash("motdepasse-admin"))
        self.A, self.auth = A, auth
        self._users = auth.USERS_FILE
        auth.USERS_FILE = Path(self.tmp.name) / "utilisateurs.json"
        auth.ecrire_utilisateurs({"lea": {"actif": True, "hash": generate_password_hash("copain-lea")}})

    def tearDown(self):
        self.auth.USERS_FILE = self._users
        for k, v in self._env.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        self.tmp.cleanup()

    def _client(self):
        c = self.A.app.test_client()
        c.environ_base["REMOTE_ADDR"] = "192.168.1.50"           # pas le serveur lui-même
        return c

    @staticmethod
    def _publique(regle):
        return regle in PUBLIQUES or regle.startswith(("/icons/", "/ui/"))

    def test_copain_n_atteint_que_sa_liste(self):
        c = self._client()
        self.assertEqual(c.post("/login", data={"username": "lea", "password": "copain-lea"}).status_code, 302)
        essais = 0
        for m, chemin, regle in _adresses(self.A.app):
            permis = (m, regle) in self.auth.COPAIN_OK or any(m == a and chemin.startswith(b) for a, b in self.auth.COPAIN_PREFIXES)
            if permis or self._publique(regle):
                continue                                  # permis : pas appelé ici (effets réels)
            r = c.open(chemin, method=m, json={})
            essais += 1
            if regle.startswith("/api/"):
                self.assertEqual(r.status_code, 403, f"{m} {regle} accessible à un copain")
            else:
                self.assertEqual(r.status_code, 302, f"{m} {regle} accessible à un copain")
                self.assertTrue(r.headers["Location"].endswith("/"), f"{m} {regle}")
        self.assertGreater(essais, 20)

    def test_personne_non_connectee(self):
        c = self._client()
        for m, chemin, regle in _adresses(self.A.app):
            if self._publique(regle):
                continue
            r = c.open(chemin, method=m, json={})
            if regle.startswith("/api/"):
                self.assertIn(r.status_code, (401, 403), f"{m} {regle} accessible sans connexion")
            else:
                self.assertEqual(r.status_code, 302, f"{m} {regle} accessible sans connexion")
                self.assertIn("/login", r.headers["Location"], f"{m} {regle}")

    def test_identifiants_reserves_refuses(self):
        import comptes
        for uid in ("..", ".x", "_copains", "_x"):
            self.assertIsNone(comptes._ID.fullmatch(uid), uid)
        self.assertIsNotNone(comptes._ID.fullmatch("lea.martin"))


if __name__ == "__main__":
    unittest.main()
