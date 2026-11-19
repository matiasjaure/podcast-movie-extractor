"""Smoke tests with the slow/paid steps stubbed out. Run: python -m unittest"""
import io
import os
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import tempfile  # noqa: E402

os.environ.update(ANTHROPIC_API_KEY="test", TMDB_API_KEY="test", DATA_DIR=tempfile.mkdtemp())

import app as web  # noqa: E402
import podcast_movies as pm  # noqa: E402

EXTRACT = pm.extract_movies  # the real one, before tests stub it


DETAILS = {
    "id": 129, "title": "El viaje de Chihiro", "original_title": "千と千尋の神隠し", "release_date": "2001-07-20",
    "runtime": 125, "genres": [{"name": "Animación"}], "overview": "Una niña…", "vote_average": 8.54, "vote_count": 100,
    "poster_path": "/p.jpg", "production_countries": [{"name": "Japan"}], "original_language": "ja",
    "external_ids": {"imdb_id": "tt0245429"},
    "credits": {"crew": [{"name": "Hayao Miyazaki", "job": "Director", "department": "Directing"},
                         {"name": "Hayao Miyazaki", "job": "Screenplay", "department": "Writing"},
                         {"name": "Hayao Miyazaki", "job": "Writer", "department": "Writing"}],
                "cast": [{"name": "Rumi Hiiragi"}]},
    "videos": {"results": [{"site": "YouTube", "type": "Teaser", "key": "t"},
                           {"site": "YouTube", "type": "Trailer", "key": "abc", "official": True}]},
}


class FakeTmdb:
    def __init__(self, key):
        pass

    def search(self, title, year):
        return {"id": 129}

    def details(self, tmdb_id):
        return DETAILS


class AppTest(unittest.TestCase):
    def setUp(self):
        pm.transcribe = lambda audio, model, progress=None: "hablamos de Chihiro"
        pm.extract_movies = lambda t, progress=None, api_key=None: [
            {"title_original": "Spirited Away", "title_mentioned": "Chihiro", "year": 2001, "context": "c"}]
        pm.Tmdb = FakeTmdb
        self.client = web.app.test_client()

    def test_parse_json_array(self):
        self.assertEqual(pm.parse_json_array('ok [{"a": 1}] done'), [{"a": 1}])
        self.assertEqual(pm.parse_json_array("no json"), [])

    def test_mp3_upload_end_to_end(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        r = self.client.post("/api/jobs", data={"file": (io.BytesIO(b"x"), "ep.mp3")})
        self.assertEqual(r.status_code, 200)
        job_id = r.get_json()["id"]
        for _ in range(50):
            s = self.client.get(f"/api/jobs/{job_id}").get_json()
            if s["state"] != "running":
                break
            time.sleep(0.1)
        self.assertEqual(s["state"], "done", s)
        m = s["movies"][0]
        self.assertEqual(m["imdb_url"], "https://www.imdb.com/title/tt0245429/")
        self.assertEqual(m["directors"], ["Hayao Miyazaki"])
        self.assertEqual(m["writers"], ["Hayao Miyazaki"])  # deduplicated
        self.assertEqual(m["rating"], 8.5)
        self.assertEqual(m["poster_url"], "https://image.tmdb.org/t/p/w342/p.jpg")
        self.assertEqual(m["trailer_url"], "https://www.youtube.com/watch?v=abc")  # official trailer wins
        self.assertEqual(m["mentioned_as"], "Chihiro")
        csv = self.client.get(f"/files/{job_id}/movies.csv").data.decode()
        self.assertIn("tt0245429", csv)

    def test_lists_crud(self):
        movie = pm.movie_from_tmdb(DETAILS, "Chihiro").to_dict()
        r = self.client.post("/api/lists", json={"name": "  Episodio 1 ", "source": "ep1", "movies": [movie]})
        self.assertEqual(r.status_code, 201)
        list_id = r.get_json()["id"]

        lists = self.client.get("/api/lists").get_json()["lists"]
        self.assertEqual([(l["id"], l["name"], l["count"]) for l in lists], [(list_id, "Episodio 1", 1)])
        self.assertEqual(lists[0]["posters"], [movie["poster_url"]])

        full = self.client.get(f"/api/lists/{list_id}").get_json()
        self.assertEqual(full["movies"][0]["directors"], ["Hayao Miyazaki"])
        self.assertEqual(full["source"], "ep1")

        self.assertEqual(self.client.put(f"/api/lists/{list_id}", json={"name": "Renombrada", "movies": []}).status_code, 200)
        full = self.client.get(f"/api/lists/{list_id}").get_json()
        self.assertEqual((full["name"], full["movies"]), ("Renombrada", []))

        self.assertEqual(self.client.delete(f"/api/lists/{list_id}").status_code, 200)
        self.assertEqual(self.client.get(f"/api/lists/{list_id}").status_code, 404)

    def test_lists_validation(self):
        self.assertEqual(self.client.post("/api/lists", json={"name": "", "movies": []}).status_code, 400)
        self.assertEqual(self.client.post("/api/lists", json={"name": "x", "movies": "nope"}).status_code, 400)
        self.assertEqual(self.client.post("/api/lists", json={"name": "x" * 121, "movies": []}).status_code, 400)
        self.assertEqual(self.client.put("/api/lists/missing", json={"name": "x"}).status_code, 404)

    def test_extract_uses_haiku_and_reports_truncation(self):
        import anthropic
        from types import SimpleNamespace as NS
        calls = []

        class FakeClient:
            def __init__(self, api_key=None):
                self.messages = self

            def create(self, **kw):
                calls.append(kw)
                text = '[{"title_original": "Coco", "title_mentioned": "Coco", "year": 2017, "context": "c"}]'
                return NS(stop_reason=self.stop, content=[NS(type="text", text=text)])

        real_extract, real_client = EXTRACT, anthropic.Anthropic
        anthropic.Anthropic = FakeClient
        try:
            FakeClient.stop = "end_turn"
            self.assertEqual(real_extract("hablamos de Coco")[0]["title_original"], "Coco")
            self.assertEqual(calls[0]["model"], "claude-haiku-5-5")
            self.assertGreaterEqual(calls[0]["max_tokens"], 16000)
            FakeClient.stop = "max_tokens"
            with self.assertRaisesRegex(RuntimeError, "se cortó"):
                real_extract("hablamos de Coco")
        finally:
            anthropic.Anthropic = real_client

    def test_config(self):
        self.assertEqual(self.client.get("/api/config").get_json(), {"keys_on_server": True})

    def test_rejects_unknown_extension(self):
        r = self.client.post("/api/jobs", data={"file": (io.BytesIO(b"x"), "evil.exe")})
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
