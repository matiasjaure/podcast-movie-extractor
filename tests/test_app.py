"""Smoke tests with the slow/paid steps stubbed out. Run: python -m unittest"""
import io
import os
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.update(ANTHROPIC_API_KEY="test", TMDB_API_KEY="test")

import app as web  # noqa: E402
import podcast_movies as pm  # noqa: E402


class FakeTmdb:
    def __init__(self, key):
        pass

    def search(self, title, year):
        return {"id": 129, "title": "El viaje de Chihiro", "original_title": "Sen to Chihiro", "release_date": "2001-07-20"}

    def imdb_id(self, tmdb_id):
        return "tt0245429"


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
        self.assertEqual(s["movies"][0]["imdb_url"], "https://www.imdb.com/title/tt0245429/")
        csv = self.client.get(f"/files/{job_id}/movies.csv").data.decode()
        self.assertIn("tt0245429", csv)

    def test_rejects_unknown_extension(self):
        r = self.client.post("/api/jobs", data={"file": (io.BytesIO(b"x"), "evil.exe")})
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
