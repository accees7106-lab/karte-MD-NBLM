"""ocr-worker のテスト: python -m unittest discover -s test -p 'test_*.py'
偽の GitHub サーバー（test/fake-github.js、node が必要）に対して、実際に HTTP で Vault を読み書きする。"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

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


ROOT = os.path.join(os.path.dirname(__file__), "..")
FAKE = {}
REPO = "me/vault"
JOB_ID = "11111111-1111-1111-1111-111111111111"


def setUpModule():
    env = dict(os.environ, FAKE_SEED=REPO)
    FAKE["proc"] = subprocess.Popen(["node", os.path.join(ROOT, "test", "fake-github.js")], stdout=subprocess.PIPE, text=True, env=env)
    FAKE["port"] = int(FAKE["proc"].stdout.readline().strip().split("=")[1])


def tearDownModule():
    FAKE["proc"].terminate()
    FAKE["proc"].wait()
    FAKE["proc"].stdout.close()


def fake(path, method="GET", body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{FAKE['port']}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(req) as res:
        return res.read()


def ts(dt):
    return dt.strftime("%Y%m%dT%H%M%SZ")


class VaultTestCase(unittest.TestCase):
    def setUp(self):
        fake("/__reset", "POST", {})
        self.sb = worker.GitHubStore("tok", (REPO, "main"), (REPO, "karte-inbox"), f"http://127.0.0.1:{FAKE['port']}", retry_sec=0)
        self.tmp = tempfile.mkdtemp()
        self.prompt_log = os.path.join(self.tmp, "prompts.txt")
        self.set_claude_output({"type": "result", "is_error": False, "structured_output": SAMPLE})
        os.environ["CLAUDE_CMD"] = os.path.join(self.tmp, "claude")
        os.environ["INBOX_IDLE_SEC"] = "0"

    def tearDown(self):
        os.environ.pop("CLAUDE_CMD", None)
        os.environ.pop("INBOX_IDLE_SEC", None)

    # ─── 偽 claude：渡された画像が実在するか確かめ、プロンプトを記録して、決めた結果を返す ───
    def set_claude_output(self, out):
        out_path = os.path.join(self.tmp, "out.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False)
        fakecmd = os.path.join(self.tmp, "claude")
        with open(fakecmd, "w") as f:
            f.write("#!/usr/bin/env python3\nimport sys,re,os\np=sys.stdin.read()\n"
                    f"open({self.prompt_log!r},'a',encoding='utf-8').write(p+'\\n=====\\n')\n"
                    "for m in re.findall(r'(?:画像ファイル|写真:) (/\\S+?\\.jpg)',p):\n"
                    "    assert os.path.exists(m), 'image missing '+m\n"
                    "assert '--json-schema' in sys.argv and 'Read' in sys.argv\n"
                    f"print(open({out_path!r}, encoding='utf-8').read())\n")
        os.chmod(fakecmd, 0o755)

    def prompts(self):
        with open(self.prompt_log, encoding="utf-8") as f:
            return f.read().split("\n=====\n")[:-1]

    # ─── Vault の準備と確認 ───
    def create_inbox(self):
        sb = self.sb
        _, blob = sb.call("POST", f"/repos/{REPO}/git/blobs", {"content": worker.INBOX_README, "encoding": "utf-8"})
        _, tree = sb.call("POST", f"/repos/{REPO}/git/trees", {"tree": [{"path": "README.md", "mode": "100644", "type": "blob", "sha": blob["sha"]}]})
        _, commit = sb.call("POST", f"/repos/{REPO}/git/commits", {"message": "init", "tree": tree["sha"], "parents": []})
        sb.call("POST", f"/repos/{REPO}/git/refs", {"ref": "refs/heads/karte-inbox", "sha": commit["sha"]})

    def seed_job(self, job_id=JOB_ID, **over):
        if not self.tree("karte-inbox"):
            self.create_inbox()
        job = dict({"id": job_id, "status": "queued", "image_path": f"jobs/{job_id}.jpg", "created_at": "2026-10-08T00:00:00Z",
                    "attempts": 0, "context": {"clients": [], "presets": [], "train_assets": []}, "result": None}, **over)
        self.sb.write(self.sb.inbox, f"jobs/{job_id}.jpg", b"\xff\xd8fakejpeg", "seed photo")
        self.sb.write(self.sb.inbox, f"jobs/{job_id}.json", self.sb._dump(job), "seed job")
        return job

    def seed_main(self, path, obj):
        data = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode()
        self.sb.write(self.sb.vault, path, data, "seed")

    def tree(self, branch="main"):
        try:
            return json.loads(fake(f"/__tree?repo={REPO}&branch={branch}"))
        except urllib.error.HTTPError:
            return []

    def read(self, path, branch="main"):
        return fake(f"/__file?repo={REPO}&branch={branch}&path={urllib.parse.quote(path)}")

    def job(self, job_id=JOB_ID):
        try:
            return json.loads(self.read(f"jobs/{job_id}.json", "karte-inbox"))
        except urllib.error.HTTPError:
            return None


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


class VaultTest(VaultTestCase):
    def test_job_done_and_image_kept_for_learning(self):
        self.seed_job()
        job = self.sb.claim_next()
        self.assertEqual(job["status"], "processing")
        worker.process(self.sb, job)
        self.assertEqual(self.job()["status"], "done", self.job().get("error"))
        self.assertEqual(self.job()["result"]["karte"][0]["client_name"], "玉谷")
        self.assertIn(f"jobs/{JOB_ID}.jpg", self.tree("karte-inbox"))  # 訂正の学習に使うので残す
        self.assertIsNone(self.sb.claim_next())
        self.assertIn("まだ訂正はありません", self.prompts()[0])

    def test_claim_is_exclusive_even_with_a_stale_view(self):
        self.seed_job()
        other = worker.GitHubStore("tok", (REPO, "main"), (REPO, "karte-inbox"), self.sb.api, retry_sec=0)
        stale = other.list_jobs()                       # もう1台がこの時点の一覧を持っている
        self.assertIsNotNone(self.sb.claim_next())
        other.list_jobs = lambda: stale                 # 古い一覧のまま取りに行く → sha が合わず取れない
        self.assertIsNone(other.claim_next())
        self.assertEqual(self.job()["status"], "processing")

    def test_conflicts_are_retried(self):
        self.seed_job()
        fake("/__fail", "POST", {"n": 2})
        job = self.sb.update_job(self.sb.list_jobs()[0], {"attempts": 5})
        self.assertEqual(job["attempts"], 5)
        self.assertEqual(self.job()["attempts"], 5)

    def test_stale_processing_goes_back_to_queue(self):
        old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        self.seed_job(status="processing", started_at=old)
        self.sb.release_stale()
        self.assertEqual(self.job()["status"], "queued")

    def test_learning_loop(self):
        old = datetime.now(timezone.utc) - timedelta(hours=1)
        fb_name = f"{ts(old)}_{JOB_ID}_0.json"
        # 1) 訂正が届いている（写真は status=feedback で残っている）
        self.seed_job(status="feedback")
        self.seed_main(f"Karte/訂正ログ/{fb_name[:4]}-{fb_name[4:6]}/{fb_name}", {
            "job_id": JOB_ID, "draft": {"client_name": "出ロ"}, "final": {"client_name": "出口"},
            "diffs": [{"field": "client_name", "ai": "出ロ", "final": "出口"}]})
        self.seed_main("Karte/読み癖/memory.json", {"version": 1, "rules": ["既存の規則A"], "feedback_count": 3, "last_feedback": "",
                                                   "aliases": [{"written": "SQ", "correct": "スクワット", "kind": "exercise"}]})
        # 2) 学習：写真と差分を見比べて新しい版を作る
        self.set_claude_output({"type": "result", "is_error": False, "structured_output": {
            "rules": ["既存の規則A", "「口」を「ロ」と読まない"],
            "aliases": [{"written": "出ロ", "correct": "出口", "kind": "client"}]}})
        self.assertTrue(worker.consolidate(self.sb))
        p = self.prompts()[-1]
        self.assertIn("写真: /", p)
        self.assertIn("顧客名: AI「出ロ」→ 正しくは「出口」", p)
        self.assertIn("既存の rules と aliases は、新しい訂正と矛盾しない限り**すべてそのまま残す**", p)
        mem = json.loads(self.read("Karte/読み癖/memory.json"))
        self.assertEqual((mem["version"], mem["feedback_count"], mem["last_feedback"]), (2, 4, fb_name))
        self.assertEqual(len(mem["aliases"]), 2)  # 古い対応表（SQ）も消えていない
        self.assertEqual(json.loads(self.read("Karte/読み癖/履歴/v0002.json"))["version"], 2)
        md = self.read("Karte/読み癖/読み癖.md").decode()
        self.assertIn("| 出ロ | 出口 | client |", md)
        self.assertIn("- 「口」を「ロ」と読まない", md)
        self.assertIsNone(self.job())  # 学習が済んだ写真とジョブは消える
        self.assertNotIn(f"jobs/{JOB_ID}.jpg", self.tree("karte-inbox"))
        self.assertIn(f"Karte/訂正ログ/{fb_name[:4]}-{fb_name[4:6]}/{fb_name}", self.tree())  # 訂正の記録は消さない
        self.assertFalse(worker.consolidate(self.sb))
        # 3) 次の読み取りプロンプトに学習内容が入る
        job_id2 = "22222222-2222-2222-2222-222222222222"
        self.seed_job(job_id2)
        self.set_claude_output({"type": "result", "is_error": False, "structured_output": SAMPLE})
        worker.process(self.sb, self.sb.claim_next())
        p = self.prompts()[-1]
        self.assertIn("過去の訂正から学んだこと（最優先で必ず守る）", p)
        self.assertIn("「出ロ」→「出口」", p)
        self.assertIn("「口」を「ロ」と読まない", p)

    def test_fresh_feedback_waits_but_is_shown_to_the_reader(self):
        self.seed_job(status="feedback")
        name = f"{ts(datetime.now(timezone.utc))}_{JOB_ID}_0.json"   # たった今書かれた訂正
        self.seed_main(f"Karte/訂正ログ/{name[:4]}-{name[4:6]}/{name}", {"job_id": JOB_ID, "diffs": [{"field": "date", "ai": "10/5", "final": "10/6"}]})
        self.assertFalse(worker.consolidate(self.sb))               # 同時刻の書き込みを取りこぼさないよう、少し待つ
        self.seed_job("33333333-3333-3333-3333-333333333333")
        worker.process(self.sb, self.sb.claim_next())
        self.assertIn("日付: AI「10/5」→ 正しくは「10/6」", self.prompts()[-1])  # 直近の訂正はそのまま読み取りに使う

    def test_learning_rejects_forgetting(self):
        name = f"{ts(datetime.now(timezone.utc) - timedelta(hours=1))}_{JOB_ID}_0.json"
        self.seed_job(status="feedback")
        self.seed_main(f"Karte/訂正ログ/{name[:4]}-{name[4:6]}/{name}", {"job_id": JOB_ID, "diffs": [{"field": "date", "ai": "a", "final": "b"}]})
        self.seed_main("Karte/読み癖/memory.json", {"version": 1, "rules": [f"規則{i}" for i in range(10)], "aliases": [], "last_feedback": ""})
        self.set_claude_output({"type": "result", "is_error": False, "structured_output": {"rules": ["規則0"], "aliases": []}})
        with self.assertRaises(ValueError):
            worker.consolidate(self.sb)
        self.assertEqual(json.loads(self.read("Karte/読み癖/memory.json"))["version"], 1)
        self.assertIsNotNone(self.job())  # 学習できていないので写真は残す

    def test_old_unreported_photos_are_deleted(self):
        long_ago = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        self.seed_job(status="imported", imported_at=long_ago)
        self.seed_job("22222222-2222-2222-2222-222222222222", status="imported", imported_at=datetime.now(timezone.utc).isoformat())
        worker.cleanup(self.sb)
        self.assertIsNone(self.job())
        self.assertIsNotNone(self.job("22222222-2222-2222-2222-222222222222"))

    def test_inbox_is_rebuilt_without_history_only_when_idle(self):
        self.seed_job()
        self.assertFalse(self.sb.reset_inbox_if_idle())             # ジョブがあるうちは触らない
        self.sb.delete_job(self.sb.list_jobs()[0])
        before = json.loads(fake(f"/__commits?repo={REPO}&branch=karte-inbox"))
        self.assertGreater(len(before), 3)                          # 写真と JSON の履歴が積もっている
        self.assertTrue(self.sb.reset_inbox_if_idle())
        after = json.loads(fake(f"/__commits?repo={REPO}&branch=karte-inbox"))
        self.assertEqual(len(after), 1)                             # 履歴ごと作り直した
        self.assertEqual(self.tree("karte-inbox"), ["README.md"])
        self.assertFalse(self.sb.reset_inbox_if_idle())             # 作り直したばかりなら何もしない

    def test_inbox_is_not_rebuilt_while_recently_written(self):
        os.environ["INBOX_IDLE_SEC"] = "600"
        self.seed_job()
        self.sb.delete_job(self.sb.list_jobs()[0])
        self.assertFalse(self.sb.reset_inbox_if_idle())             # 直前に書き込みがあった（アップロード中かもしれない）

    def test_vault_itself_is_never_rebuilt(self):
        self.seed_main("Karte/カルテ/玉谷/a.md", b"x")
        for inbox in ((REPO, "main"), (REPO, "work")):
            sb = worker.GitHubStore("tok", (REPO, "main"), inbox, self.sb.api, retry_sec=0)
            self.assertFalse(sb.reset_inbox_if_idle())
        self.assertEqual(self.tree(), ["Karte/カルテ/玉谷/a.md"])

    # ─── --check（環境の確認）───
    def run_check(self, **env):
        keys = ("GITHUB_TOKEN", "GITHUB_REPO", "VAULT_BRANCH", "INBOX_BRANCH", "INBOX_REPO", "GITHUB_API_URL", "ANTHROPIC_API_KEY")
        saved = {k: os.environ.get(k) for k in keys}
        try:
            for k in keys:
                os.environ.pop(k, None)
            os.environ.update(GITHUB_TOKEN="tok", GITHUB_REPO=REPO, GITHUB_API_URL=self.sb.api, KARTE_RETRY_SEC="0")
            os.environ.update(env)
            import io, contextlib
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                ok = worker.self_check()
            return ok, buf.getvalue()
        finally:
            for k, v in saved.items():
                os.environ.pop(k, None)
                if v is not None:
                    os.environ[k] = v

    def test_check_passes_when_everything_is_ready(self):
        self.set_claude_output({"type": "result", "is_error": False, "structured_output": {"karte": []}})
        ok, out = self.run_check()
        self.assertTrue(ok, out)
        self.assertNotIn("[NG]", out)
        self.assertIn("GitHub: me/vault に接続できました", out)
        self.assertIn("claude -p の呼び出し", out)

    def test_check_explains_each_problem(self):
        self.set_claude_output({"type": "result", "is_error": False, "structured_output": {"karte": []}})
        ok, out = self.run_check(GITHUB_TOKEN="wrong")
        self.assertFalse(ok)
        self.assertIn("[NG] GitHub: トークンが使えません（401）", out)
        ok, out = self.run_check(GITHUB_REPO="me/other")
        self.assertIn("[NG] GitHub: me/other が見えません", out)
        ok, out = self.run_check(VAULT_BRANCH="nope")
        self.assertIn("[NG] Vault のブランチ nope が見つかりません", out)
        fake("/__readonly", "POST", {"on": True})
        ok, out = self.run_check()
        self.assertIn("書き込み権限がありません", out)
        ok, out = self.run_check(ANTHROPIC_API_KEY="x")
        self.assertIn("[NG] ANTHROPIC_API_KEY が設定されています", out)
        ok, out = self.run_check(INBOX_BRANCH="main")
        self.assertIn("[NG] INBOX_BRANCH が Vault のブランチと同じです", out)
        os.environ.pop("GITHUB_TOKEN", None)
        ok, out = self.run_check(GITHUB_TOKEN="")
        self.assertIn("[NG] GITHUB_TOKEN が .env にありません", out)

    def test_check_reports_a_claude_that_is_not_logged_in(self):
        self.set_claude_output({"type": "result", "is_error": True, "result": "Not logged in"})
        ok, out = self.run_check()
        self.assertFalse(ok)
        self.assertIn("[NG] claude -p の呼び出しに失敗", out)

    def test_dotenv_wins_over_a_stray_environment_variable(self):
        path = os.path.join(self.tmp, ".env")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\ufeff# コメント（BOM 付きでも読める）\nGITHUB_TOKEN=from-dotenv\nCLAUDE_MODEL=\nPOLL_SEC=\"45\"\n")
        saved = {k: os.environ.get(k) for k in ("GITHUB_TOKEN", "CLAUDE_MODEL", "POLL_SEC")}
        try:
            os.environ.update(GITHUB_TOKEN="from-another-tool", CLAUDE_MODEL="keep-me")
            os.environ.pop("POLL_SEC", None)
            worker.load_env(path)
            self.assertEqual(os.environ["GITHUB_TOKEN"], "from-dotenv")      # .env が優先
            self.assertEqual(os.environ["CLAUDE_MODEL"], "keep-me")          # 空の行は環境変数を消さない
            self.assertEqual(os.environ["POLL_SEC"], "45")
        finally:
            for k, v in saved.items():
                os.environ.pop(k, None)
                if v is not None:
                    os.environ[k] = v

    def test_check_env(self):
        env = dict(os.environ)
        try:
            os.environ["ANTHROPIC_API_KEY"] = "x"
            with self.assertRaises(SystemExit):
                worker.check_env()
            del os.environ["ANTHROPIC_API_KEY"]
            os.environ.pop("GITHUB_TOKEN", None)
            with self.assertRaises(SystemExit):
                worker.check_env()
            os.environ.update(GITHUB_TOKEN="t", GITHUB_REPO=REPO, VAULT_BRANCH="main", INBOX_REPO=REPO, INBOX_BRANCH="main")
            with self.assertRaises(SystemExit):
                worker.check_env()                                  # 使い捨てブランチが Vault と同じ → 起動しない
        finally:
            os.environ.clear()
            os.environ.update(env)


if __name__ == "__main__":
    unittest.main()
