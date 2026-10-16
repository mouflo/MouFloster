"""🆕 Nouveautés sans poster et liste des épisodes : essais sur des dossiers et un Emby factices (rien de réel n'est touché)."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import library
import nouveautes


class _Rep:
    def __init__(self, data):
        self._d = data

    def json(self):
        return self._d


class NouveautesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.media = base / "media"
        for d in ("Manga/Blue Exorcist/Specials", "Manga/Blue Exorcist/s1", "Series/Below/s1", "Films 4k/Film (2026)"):
            (self.media / d).mkdir(parents=True)
        sp = self.media / "Manga/Blue Exorcist/Specials"
        (sp / "Blue Exorcist.S00E12.[JA+FR].mkv").write_bytes(b"x")
        (sp / "Blue Exorcist.S00E12.[JA+FR].nfo").write_text("<episodedetails><title>Blue Exorcist : Le film</title></episodedetails>", encoding="utf-8")
        (self.media / "Manga/Blue Exorcist/s1/Blue Exorcist.S01E01.mkv").write_bytes(b"x")
        (self.media / "Manga/Blue Exorcist/s1/Blue Exorcist.S01E01.nfo").write_text("<title>Blue Exorcist.S01E01</title>", encoding="utf-8")
        (self.media / "Series/Below/s1/x.mkv").write_bytes(b"x")
        (self.media / "Films 4k/Film (2026)/f.mkv").write_bytes(b"x")
        data = base / "data"
        data.mkdir()
        (data / "moufloster.log").write_text(
            "2026-10-11 20:08:42,755 - INFO - 📁 Médiathèque: Manga/Blue Exorcist/season01-poster.jpg (action=copy, sauvegarde=None)\n"
            "2026-10-11 20:09:42,755 - INFO - 📁 Médiathèque: Manga/Blue Exorcist/Specials/a-thumb.jpg (action=copy, sauvegarde=None)\n",
            encoding="utf-8")
        self.p = [mock.patch.object(library, "LIBRARY_ROOT", self.media), mock.patch.object(library, "MAP_FILE", data / "map.json"),
                  mock.patch.object(nouveautes, "FICHIER", data / "nouveautes.json")]
        for x in self.p:
            x.start()
        library.clear_cache()

    def tearDown(self):
        for x in self.p:
            x.stop()
        library.clear_cache()
        self.tmp.cleanup()

    def test_episodes_des_speciaux(self):
        eps = library.list_episodes("Manga/Blue Exorcist", 0)
        self.assertEqual([(e["episode"], e["titre"]) for e in eps], [(12, "Blue Exorcist : Le film")])
        self.assertEqual(library.list_episodes("Manga/Blue Exorcist", 1)[0]["titre"], "")   # nom de fichier : pas un titre
        self.assertEqual(library.list_episodes("../etc", 0), [])

    def test_liste_et_historique(self):
        m = str(self.media)
        items = {"Items": [
            {"Type": "Series", "Name": "Below", "Path": m + "/Series/Below", "ProviderIds": {"Tmdb": "1"}, "DateCreated": "2026-10-15"},
            {"Type": "Season", "IndexNumber": 1, "SeriesId": "s1", "SeriesName": "Below", "Path": m + "/Series/Below/s1", "DateCreated": "2026-10-15"},
            {"Type": "Season", "IndexNumber": 1, "SeriesId": "be", "SeriesName": "Blue Exorcist", "Path": m + "/Manga/Blue Exorcist/s1", "DateCreated": "2026-10-14"},
            {"Type": "Season", "IndexNumber": 0, "SeriesId": "be", "SeriesName": "Blue Exorcist", "Path": m + "/Manga/Blue Exorcist/Specials", "DateCreated": "2026-10-13"},
            {"Type": "Movie", "Name": "Film", "Path": m + "/Films 4k/Film (2026)/f.mkv", "ProviderIds": {"Tmdb": "9"}, "DateCreated": "2026-10-12"},
        ]}
        series = {"Items": [{"Id": "be", "Name": "Blue Exorcist", "ProviderIds": {"Tmdb": "38464"}, "ProductionYear": 2011}]}
        appels = []

        def faux(method, path, params=None, **k):
            appels.append(params)
            return _Rep(series if "Ids" in params else items)

        with mock.patch("emby._request", side_effect=faux):
            r = nouveautes.lister("30")
            par_rel = {e["rel"]: e for e in r["elements"]}
            self.assertEqual(par_rel["Manga/Blue Exorcist"]["saisons"], [0])        # saison 1 déjà faite (journal)
            self.assertEqual(par_rel["Manga/Blue Exorcist"]["tmdb"], "38464")      # série ancienne : TMDB relu
            self.assertTrue(par_rel["Series/Below"]["poster"])
            self.assertEqual(par_rel["Series/Below"]["saisons"], [1])
            self.assertIn("MinDateCreated", appels[0])
            nouveautes.marquer("Series/Below")
            nouveautes.marquer("Series/Below", 1)
            nouveautes.marquer("Films 4k/Film (2026)")
            r = nouveautes.lister("tout")
            self.assertEqual([e["rel"] for e in r["elements"]], ["Manga/Blue Exorcist"])
            self.assertNotIn("MinDateCreated", appels[-2])

    def test_reglage_par_defaut(self):
        self.assertEqual(nouveautes.reglages()["defaut"], "debut")
        nouveautes.regler_defaut("180")
        self.assertEqual(nouveautes.reglages()["defaut"], "180")
        with self.assertRaises(ValueError):
            nouveautes.regler_defaut("n'importe quoi")


if __name__ == "__main__":
    unittest.main()
