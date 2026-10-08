"""ocr-worker のテスト（標準ライブラリのみ）: python -m unittest discover -s test -p 'test_*.py'"""
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ocr-worker"))
import worker  # noqa: E402

SAMPLE = {
    "karte": [{
        "client_name": " 玉谷 ", "client_confidence": "high", "date": "2026-10-07",
        "condition": "腰に張り", "warmup": {"preset": "WU-A", "changes": [
            {"op": "replace", "from": "デッドバグ", "to": "バードドッグ"}, {"op": "bogus"}]},
        "training": [
            {"name": "RDL", "sets": [{"kg": "40", "reps": 10, "sets": 3}, {"kg": None, "reps": None, "sets": None}], "text": None},
            {"name": "プランク", "sets": [], "text": "60秒×2"},
            {"name": "", "sets": [], "text": "x"},
        ],
        "notes": "", "uncertain": ["2行目", ""],
    }]
}


class NormalizeTest(unittest.TestCase):
    def test_normalize(self):
        r = worker.normalize_result(SAMPLE)["karte"][0]
        self.assertEqual(r["client_name"], "玉谷")
        self.assertEqual(r["warmup"]["changes"], [{"op": "replace", "from": "デッドバグ", "to": "バードドッグ",
                                                   "name": None, "after": None, "value": None}])
        self.assertEqual(r["training"][0]["sets"], [{"kg": 40, "reps": 10, "sets": 3}])
        self.assertEqual(r["training"][1], {"name": "プランク", "sets": [], "text": "60秒×2"})
        self.assertEqual(len(r["training"]), 2)
        self.assertIsNone(r["notes"])
        self.assertEqual(r["uncertain"], ["2行目"])

    def test_bad_date_becomes_null(self):
        d = json.loads(json.dumps(SAMPLE)); d["karte"][0]["date"] = "10/7"
        self.assertIsNone(worker.normalize_result(d)["karte"][0]["date"])

    def test_text_with_fence(self):
        r = worker.normalize_result("```json\n" + json.dumps(SAMPLE, ensure_ascii=False) + "\n```")
        self.assertEqual(len(r["karte"]), 1)

    def test_invalid(self):
        with self.assertRaises(ValueError):
            worker.normalize_result({"foo": 1})
        with self.assertRaises(ValueError):
            worker.normalize_result("読めませんでした")

    def test_prompt_has_context(self):
        p = worker.build_prompt("/tmp/a.jpg", {
            "clients": [{"name": "玉谷", "wu_preset": "WU-A"}],
            "presets": [{"name": "WU-A", "items": ["チェストオープナー", "デッドバグ"]}],
            "train_assets": [{"name": "RDL", "type": "structured"}]}, "2026-10-06T10:00:00+00:00")
        self.assertIn("撮影日（2026-10-06）", p)
        self.assertIn("/tmp/a.jpg", p)
        self.assertIn("玉谷（WU-A）", p)
        self.assertIn("WU-A: チェストオープナー → デッドバグ", p)
        self.assertIn("RDL", p)


class FakeSupabase(BaseHTTPRequestHandler):
    rows = {}
    objects = {}

    def log_message(self, *a):
        pass

    def _send(self, code, body=b"", ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(body if isinstance(body, bytes) else json.dumps(body).encode())

    def _filters(self):
        from urllib.parse import urlparse, parse_qsl
        q = dict(parse_qsl(urlparse(self.path).query))
        return {k: v.split(".", 1)[1] for k, v in q.items() if k in ("id", "status")}

    def _match(self):
        f = self._filters()
        return [r for r in self.rows.values() if all(str(r.get(k)) == v for k, v in f.items())]

    def do_GET(self):
        if self.path.startswith("/storage/v1/object/karte-ocr/"):
            name = self.path.rsplit("/", 1)[1]
            return self._send(200, self.objects[name], "image/jpeg") if name in self.objects else self._send(404)
        self._send(200, [{"id": r["id"]} for r in self._match()][:1])

    def do_PATCH(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if "started_at=lt." in self.path:
            return self._send(200, [])
        hit = self._match()
        for r in hit:
            r.update(body)
        self._send(200, hit)

    def do_DELETE(self):
        self.objects.pop(self.path.rsplit("/", 1)[1], None)
        self._send(200, {})


class EndToEndTest(unittest.TestCase):
    def setUp(self):
        FakeSupabase.rows = {"11111111-1111-1111-1111-111111111111": {
            "id": "11111111-1111-1111-1111-111111111111", "status": "queued", "image_path": "a.jpg",
            "attempts": 0, "context": {"clients": [], "presets": [], "train_assets": []}}}
        FakeSupabase.objects = {"a.jpg": b"\xff\xd8fakejpeg"}
        self.srv = HTTPServer(("127.0.0.1", 0), FakeSupabase)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.tmp = tempfile.mkdtemp()
        fake = os.path.join(self.tmp, "claude")
        out_path = os.path.join(self.tmp, "out.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump({"type": "result", "is_error": False, "structured_output": SAMPLE}, f, ensure_ascii=False)
        with open(fake, "w") as f:
            # 標準入力のプロンプトから画像パスを取り出し、実在するか確かめてから結果を返す偽 claude
            f.write("#!/usr/bin/env python3\nimport sys,re,os,json\np=sys.stdin.read()\n"
                    "m=re.search(r'画像ファイル (\\S+) を',p)\n"
                    "assert m and os.path.exists(m.group(1)), 'image missing'\n"
                    "assert '--json-schema' in sys.argv and 'Read' in sys.argv\n"
                    f"print(open({out_path!r}, encoding='utf-8').read())\n")
        os.chmod(fake, 0o755)
        os.environ["CLAUDE_CMD"] = fake

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        os.environ.pop("CLAUDE_CMD", None)

    def test_job_done_and_image_removed(self):
        sb = worker.Supabase(f"http://127.0.0.1:{self.srv.server_port}", "k")
        job = sb.claim_next()
        self.assertEqual(job["status"], "processing")
        worker.process(sb, job)
        row = FakeSupabase.rows[job["id"]]
        self.assertEqual(row["status"], "done", row.get("error"))
        self.assertEqual(row["result"]["karte"][0]["client_name"], "玉谷")
        self.assertNotIn("a.jpg", FakeSupabase.objects)
        self.assertIsNone(sb.claim_next())

    def test_api_key_refused(self):
        os.environ["ANTHROPIC_API_KEY"] = "x"
        try:
            with self.assertRaises(SystemExit):
                worker.check_env()
        finally:
            del os.environ["ANTHROPIC_API_KEY"]


if __name__ == "__main__":
    unittest.main()
