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

    def test_merge_memory_keeps_aliases(self):
        m = worker.merge_memory({"rules": ["a", "b"], "aliases": [{"written": "SQ", "correct": "スクワット", "kind": "exercise"}]},
                                {"rules": ["a", "b2"], "aliases": [{"written": "RD", "correct": "RDL", "kind": "exercise"},
                                                                   {"written": "x", "correct": "x", "kind": "other"}]})
        self.assertEqual(m["rules"], ["a", "b2"])
        self.assertEqual({a["written"] for a in m["aliases"]}, {"SQ", "RD"})

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
    """PostgREST と Storage のごく一部（eq / in / order / limit）だけを真似る"""
    tables = {}
    objects = {}

    def log_message(self, *a):
        pass

    def _send(self, code, body=b"", ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(body if isinstance(body, bytes) else json.dumps(body).encode())

    def _parse(self):
        from urllib.parse import urlparse, parse_qsl
        u = urlparse(self.path)
        table = u.path.rsplit("/", 1)[1]
        q = dict(parse_qsl(u.query))
        rows = self.tables.setdefault(table, [])
        out = []
        for r in rows:
            ok = True
            for k, v in q.items():
                if k in ("select", "order", "limit"):
                    continue
                op, val = v.split(".", 1)
                if op == "eq" and str(r.get(k)).lower() != val.lower():
                    ok = False
                if op == "in" and str(r.get(k)) not in val.strip("()").split(","):
                    ok = False
                if op == "lt":
                    ok = False  # 期限切れの判定はテストしない
            if ok:
                out.append(r)
        if q.get("order", "").endswith(".desc"):
            out = sorted(out, key=lambda r: r.get(q["order"].split(".")[0]) or 0, reverse=True)
        if "limit" in q:
            out = out[: int(q["limit"])]
        return table, rows, out

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n)) if n else None

    def do_GET(self):
        if self.path.startswith("/storage/v1/object/karte-ocr/"):
            name = self.path.rsplit("/", 1)[1]
            return self._send(200, self.objects[name], "image/jpeg") if name in self.objects else self._send(404)
        self._send(200, self._parse()[2])

    def do_POST(self):
        table, rows, _ = self._parse()
        rows.append(self._body())
        self._send(201, [])

    def do_PATCH(self):
        body = self._body()
        _, _, hit = self._parse()
        for r in hit:
            r.update(body)
        self._send(200, hit)

    def do_DELETE(self):
        if self.path.startswith("/storage/"):
            self.objects.pop(self.path.rsplit("/", 1)[1], None)
            return self._send(200, {})
        table, rows, hit = self._parse()
        for r in hit:
            rows.remove(r)
        self._send(200, hit)


JOB_ID = "11111111-1111-1111-1111-111111111111"


class EndToEndTest(unittest.TestCase):
    def setUp(self):
        FakeSupabase.tables = {"karte_ocr_jobs": [{
            "id": JOB_ID, "status": "queued", "image_path": "a.jpg", "attempts": 0,
            "context": {"clients": [], "presets": [], "train_assets": []}}]}
        FakeSupabase.objects = {"a.jpg": b"\xff\xd8fakejpeg"}
        self.srv = HTTPServer(("127.0.0.1", 0), FakeSupabase)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.sb = worker.Supabase(f"http://127.0.0.1:{self.srv.server_port}", "k")
        self.tmp = tempfile.mkdtemp()
        self.prompt_log = os.path.join(self.tmp, "prompts.txt")
        self.set_claude_output({"type": "result", "is_error": False, "structured_output": SAMPLE})
        os.environ["CLAUDE_CMD"] = os.path.join(self.tmp, "claude")

    def set_claude_output(self, out):
        """偽 claude：渡された画像が実在するか確かめ、プロンプトを記録して、決めた結果を返す"""
        out_path = os.path.join(self.tmp, "out.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False)
        fake = os.path.join(self.tmp, "claude")
        with open(fake, "w") as f:
            f.write("#!/usr/bin/env python3\nimport sys,re,os\np=sys.stdin.read()\n"
                    f"open({self.prompt_log!r},'a',encoding='utf-8').write(p+'\\n=====\\n')\n"
                    "for m in re.findall(r'(?:画像ファイル|写真:) (/\\S+?\\.jpg)',p):\n"
                    "    assert os.path.exists(m), 'image missing '+m\n"
                    "assert '--json-schema' in sys.argv and 'Read' in sys.argv\n"
                    f"print(open({out_path!r}, encoding='utf-8').read())\n")
        os.chmod(fake, 0o755)

    def prompts(self):
        with open(self.prompt_log, encoding="utf-8") as f:
            return f.read().split("\n=====\n")[:-1]

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        os.environ.pop("CLAUDE_CMD", None)

    def job(self):
        rows = FakeSupabase.tables["karte_ocr_jobs"]
        return rows[0] if rows else None

    def test_job_done_and_image_kept_for_learning(self):
        job = self.sb.claim_next()
        self.assertEqual(job["status"], "processing")
        worker.process(self.sb, job)
        self.assertEqual(self.job()["status"], "done", self.job().get("error"))
        self.assertEqual(self.job()["result"]["karte"][0]["client_name"], "玉谷")
        self.assertIn("a.jpg", FakeSupabase.objects)  # 訂正の学習に使うので残す
        self.assertIsNone(self.sb.claim_next())
        self.assertIn("まだ訂正はありません", self.prompts()[0])

    def test_learning_loop(self):
        # 1) 訂正が届いている（写真は status=feedback で残っている）
        self.job().update({"status": "feedback"})
        FakeSupabase.tables["karte_ocr_feedback"] = [{
            "id": 1, "job_id": JOB_ID, "consolidated": False, "created_at": "2026-10-08T00:00:00Z",
            "draft": {"client_name": "出ロ"}, "final": {"client_name": "出口"},
            "diffs": [{"field": "client_name", "ai": "出ロ", "final": "出口"}]}]
        FakeSupabase.tables["karte_ocr_memory"] = [{"version": 1, "rules": ["既存の規則A"], "aliases": [
            {"written": "SQ", "correct": "スクワット", "kind": "exercise"}], "feedback_count": 3}]
        # 2) 学習：写真と差分を見比べて新しい版を作る
        self.set_claude_output({"type": "result", "is_error": False, "structured_output": {
            "rules": ["既存の規則A", "「口」を「ロ」と読まない"],
            "aliases": [{"written": "出ロ", "correct": "出口", "kind": "client"}]}})
        self.assertTrue(worker.consolidate(self.sb))
        p = self.prompts()[-1]
        self.assertIn("写真: /", p)
        self.assertIn("顧客名: AI「出ロ」→ 正しくは「出口」", p)
        self.assertIn("既存の rules と aliases は、新しい訂正と矛盾しない限り**すべてそのまま残す**", p)
        mem = self.sb.latest_memory()
        self.assertEqual(mem["version"], 2)
        self.assertEqual(mem["feedback_count"], 4)
        self.assertEqual(len(mem["aliases"]), 2)  # 古い対応表（SQ）も消えていない
        self.assertEqual(FakeSupabase.tables["karte_ocr_memory"][0]["version"], 1)  # 古い版は残る
        self.assertTrue(FakeSupabase.tables["karte_ocr_feedback"][0]["consolidated"])
        self.assertIsNone(self.job())  # 学習が済んだ写真とジョブは消える
        self.assertNotIn("a.jpg", FakeSupabase.objects)
        self.assertFalse(worker.consolidate(self.sb))
        # 3) 次の読み取りプロンプトに学習内容が入る
        FakeSupabase.tables["karte_ocr_jobs"] = [{"id": JOB_ID, "status": "queued", "image_path": "b.jpg", "attempts": 0, "context": {}}]
        FakeSupabase.objects["b.jpg"] = b"x"
        self.set_claude_output({"type": "result", "is_error": False, "structured_output": SAMPLE})
        worker.process(self.sb, self.sb.claim_next())
        p = self.prompts()[-1]
        self.assertIn("過去の訂正から学んだこと（最優先で必ず守る）", p)
        self.assertIn("「出ロ」→「出口」", p)
        self.assertIn("「口」を「ロ」と読まない", p)

    def test_learning_rejects_forgetting(self):
        FakeSupabase.tables["karte_ocr_feedback"] = [{"id": 1, "job_id": JOB_ID, "consolidated": False,
                                                      "diffs": [{"field": "date", "ai": "a", "final": "b"}]}]
        FakeSupabase.tables["karte_ocr_memory"] = [{"version": 1, "rules": [f"規則{i}" for i in range(10)], "aliases": []}]
        self.set_claude_output({"type": "result", "is_error": False, "structured_output": {"rules": ["規則0"], "aliases": []}})
        with self.assertRaises(ValueError):
            worker.consolidate(self.sb)
        self.assertEqual(len(FakeSupabase.tables["karte_ocr_memory"]), 1)
        self.assertFalse(FakeSupabase.tables["karte_ocr_feedback"][0]["consolidated"])

    def test_api_key_refused(self):
        os.environ["ANTHROPIC_API_KEY"] = "x"
        try:
            with self.assertRaises(SystemExit):
                worker.check_env()
        finally:
            del os.environ["ANTHROPIC_API_KEY"]


if __name__ == "__main__":
    unittest.main()
