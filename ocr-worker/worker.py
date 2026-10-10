"""手書きノート読み取りワーカー（ノートPCで動かす）

Vault リポジトリ（GitHub）の使い捨てブランチ karte-inbox から、status=queued のジョブ（jobs/<id>.json と写真）を取り出し、
ログイン済みの Claude Code CLI（`claude -p`、サブスクの枠内）で写真を読み取って結果を書き戻す。
API キーは使わない。Vault をローカルに clone する必要はない（GitHub の API で読み書きする）。

学習：トレーナーが下書きを直して保存すると、その差分が Vault の Karte/訂正ログ/ に残る。
手が空いたときに「写真＋AI の読み取り＋直した結果」を見比べて読み癖を要約し、
Karte/読み癖/ に新しい版として追記する（上書きしない＝忘れない）。毎回の読み取りプロンプトに必ず入れる。

  python worker.py          常駐（POLL_SEC 秒ごとに確認）
  python worker.py --once   溜まっている分だけ処理して終了
  python worker.py --check  環境の確認だけ（トークン・Vault への接続・Claude Code のログイン）。setup.ps1 から呼ばれる

設定は同じフォルダの .env（.env.example 参照）。依存は標準ライブラリのみ。
"""
import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_REPO = "accees7106-lab/obsidian-vault"
JOBS_DIR = "jobs"
DIR_FEEDBACK = "Karte/訂正ログ"     # 実際の置き場所は <年-月>/（Contents API は1フォルダ1000件までしか一覧できない）
DIR_MEMORY = "Karte/読み癖"
MEMORY_FILE = DIR_MEMORY + "/memory.json"
SETTLE_SEC = 120            # 訂正ログがこれより新しいうちは学習に回さない（同時刻の書き込みを取りこぼさないため）
LEARN_CHECK_SEC = 120       # 学習待ちを確認する間隔
INBOX_IDLE_SEC = 600        # inbox ブランチを作り直すまでの無操作時間
STALE_MINUTES = 15
MAX_ATTEMPTS = 2
CONSOLIDATE_BATCH = 10      # 1回の学習で見比べる訂正の数
RECENT_FEEDBACK = 30        # まだ学習に回っていない訂正をプロンプトに直接入れる上限
MAX_RULES = 80
IMPORTED_KEEP_DAYS = 14     # 取り込んだまま訂正の報告が来ない写真を残す日数
FEEDBACK_KEEP_DAYS = 60     # 学習できないまま残った写真の上限

# ─── 出力スキーマ（index.html の applyDraft が読む形）───
SET_SCHEMA = {
    "type": "object",
    "properties": {
        "kg": {"type": ["number", "null"]},
        "reps": {"type": ["number", "null"]},
        "sets": {"type": ["number", "null"]},
    },
    "required": ["kg", "reps", "sets"],
}
KARTE_SCHEMA = {
    "type": "object",
    "properties": {
        "client_name": {"type": ["string", "null"]},
        "client_confidence": {"type": "string", "enum": ["high", "low"]},
        "date": {"type": ["string", "null"], "description": "YYYY-MM-DD。ノートに無ければ null"},
        "condition": {"type": ["string", "null"]},
        "warmup": {
            "type": "object",
            "properties": {
                "preset": {"type": ["string", "null"]},
                "changes": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "op": {"type": "string", "enum": ["replace", "add", "remove", "modify"]},
                            "from": {"type": ["string", "null"]},
                            "to": {"type": ["string", "null"]},
                            "name": {"type": ["string", "null"]},
                            "after": {"type": ["string", "null"]},
                            "value": {"type": ["string", "null"]},
                        },
                        "required": ["op"],
                    },
                },
            },
            "required": ["preset", "changes"],
        },
        "training": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "sets": {"type": "array", "items": SET_SCHEMA},
                    "text": {"type": ["string", "null"]},
                },
                "required": ["name", "sets", "text"],
            },
        },
        "notes": {"type": ["string", "null"]},
        "uncertain": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["client_name", "client_confidence", "date", "condition", "warmup", "training", "notes", "uncertain"],
}
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {"karte": {"type": "array", "items": KARTE_SCHEMA}},
    "required": ["karte"],
}

PROMPT = """あなたはパーソナルジムのトレーナーの手書きノートを読み取るアシスタントです。
画像ファイル {image} を Read ツールで開き、書かれている内容を読み取って、指定の JSON スキーマで返してください。

## ノートの書き方
- 1人分ごとに「顧客名・体調・ウォームアップ（WU）・トレーニング内容」が書かれている。1枚に複数人いれば karte に複数件入れる。
- WU は「プリセット名＋差分」で書かれる（例:「WU-A　デッドバグ→バードドッグ」）。プリセット通りなら差分は無い。
  - warmup.preset にはプリセット名を入れる（下の一覧の綴りに合わせる。書かれていなければ null）。
  - warmup.changes には差分だけを入れる：
    - 入れ替え: {{"op":"replace","from":"元の種目","to":"新しい種目"}}
    - 追加: {{"op":"add","name":"種目名だけ","after":"直前の種目 または null","value":"秒数・回数など（無ければ null）"}}
      （例: 「プランク30秒」→ name は「プランク」、value は「30秒」。種目名に数値を混ぜない）
    - 削除: {{"op":"remove","name":"種目"}}
    - 回数や時間などの変更: {{"op":"modify","name":"種目","value":"書かれた内容"}}
  - プリセットの種目を全部書き出さないこと（展開はアプリ側で行う）。
- トレーニングは書かれた順に training に入れる。
  - 重量・回数・セット数が読めるものは sets に1行ずつ入れる（例: 40kg×10×3 → {{"kg":40,"reps":10,"sets":3}}。重量が無ければ kg は null）。text は null。
  - 数値で表せないもの（秒数・左右・メモ等）は sets を [] にして text に書かれた内容を入れる。
- 体調は condition、それ以外の書き込みは notes に入れる。

## 名前の合わせ方
- 顧客名・プリセット名・種目名は、下の登録済み一覧に近いものがあればその綴りに合わせる。一覧に無いものは書かれた通りに入れる。
- 顧客名に自信が無ければ client_confidence を "low" にする。

## 過去の訂正から学んだこと（最優先で必ず守る）
このノートを書くトレーナーは、これまでの読み取り結果を実際に訂正している。下の規則と対応表はその訂正から学んだもの。
- 一覧に当てはまる書き方を見つけたら、必ず対応表・規則のとおりに読む。同じ間違いを二度としない。
- 規則と登録済み一覧が食い違うときは、規則を優先する。
{memory}

## 推測しない
- 読めない・あいまいな箇所は推測で埋めず、uncertain に「どこが・なぜ」を短く日本語で書く（例:「RDLの2セット目の重量が判読できない」）。
- 日付がノートに無ければ date は null。年が書かれていなければ撮影日（{taken}）の年とし、uncertain には書かない。

## 登録済み一覧
顧客（いつものWUプリセット）:
{clients}

WUプリセット（種目の並び）:
{presets}

種目:
{assets}
"""


def load_env(path=None):
    """.env を読む。値のある行は、すでに設定されている環境変数より優先する
    （他のツールが GITHUB_TOKEN などをユーザー環境変数に入れていても、このワーカーの設定が黙って無視されないように）。
    値が空の行は何もしない（環境変数があればそれを使う）。"""
    path = path or os.path.join(HERE, ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if v:
                os.environ[k] = v


def log(msg):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        print(line.encode("ascii", "replace").decode("ascii"), flush=True)
    # ログファイルは Python が UTF-8 で直接書く（PowerShell の出力リダイレクト経由だと日本語が文字化けする）
    path = os.environ.get("KARTE_LOG")
    if path:
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


def now_iso():
    return datetime.now(timezone.utc).isoformat()


class Conflict(RuntimeError):
    """同じブランチへの同時書き込みで GitHub が返す競合（409 / sha 不一致の 422）"""


class GitHubStore:
    """Vault リポジトリの Contents / Git Data API。ブラウザ側 api/_github.js・api/_store.js と同じ取り決め。"""

    def __init__(self, token, vault, inbox, api="https://api.github.com", retry_sec=0.4):
        self.token, self.api, self.retry_sec = token, api.rstrip("/"), retry_sec
        self.vault, self.inbox = vault, inbox            # (repo, branch)
        self._jobs_cache = {}                            # ファイルの sha → 読み込んだジョブ
        self._fb_cache = {}                              # 訂正ログのファイル名 → 中身（書き換わらない）

    # ─── 低レベル ───
    def call(self, method, path, body=None, raw=False):
        headers = {"Authorization": f"Bearer {self.token}", "User-Agent": "karte-ocr-worker",
                   "Accept": "application/vnd.github.raw+json" if raw else "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.api + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=60) as res:
                status, content = res.status, res.read()
        except urllib.error.HTTPError as e:
            status, content = e.code, e.read()
        if raw and status == 200:
            return status, content
        try:
            return status, (json.loads(content) if content else None)
        except ValueError:
            return status, content.decode("utf-8", "replace")

    @staticmethod
    def _enc(p):
        return "/".join(urllib.parse.quote(x, safe="") for x in p.split("/"))

    def _contents(self, loc, path):
        return f"/repos/{loc[0]}/contents/{self._enc(path)}"

    @staticmethod
    def _conflict(status, data):
        return status == 409 or (status == 422 and "sha" in json.dumps(data, ensure_ascii=False).lower())

    def _check(self, status, data, what):
        if status >= 300:
            raise RuntimeError(f"GitHub: {what} に失敗しました（{status}）{str(data)[:200]}")

    def get_file(self, loc, path):
        """(sha, 中身 or None) / 無ければ None。1MB を超えるファイル（写真）は中身が付かない。"""
        status, data = self.call("GET", f"{self._contents(loc, path)}?ref={urllib.parse.quote(loc[1], safe='')}")
        if status == 404:
            return None
        self._check(status, data, f"{path} の読み取り")
        if isinstance(data, list) or data.get("type") != "file":
            raise RuntimeError(f"ファイルではありません: {path}")
        body = base64.b64decode(data["content"]) if data.get("encoding") == "base64" else None
        return data["sha"], body

    def list_dir(self, loc, path):
        status, data = self.call("GET", f"{self._contents(loc, path)}?ref={urllib.parse.quote(loc[1], safe='')}")
        if status == 404:
            return []
        self._check(status, data, f"{path} の一覧取得")
        return data if isinstance(data, list) else []

    def put(self, loc, path, data, message, sha=None):
        body = {"message": message, "branch": loc[1], "content": base64.b64encode(data).decode("ascii")}
        if sha:
            body["sha"] = sha
        return self.call("PUT", self._contents(loc, path), body)

    def write(self, loc, path, data, message):
        """あれば上書き、無ければ作成。競合したら読み直してやり直す。"""
        for i in range(6):
            cur = self.get_file(loc, path)
            status, res = self.put(loc, path, data, message, cur[0] if cur else None)
            if status < 300:
                return res["content"]["sha"]
            if not self._conflict(status, res):
                self._check(status, res, f"{path} の書き込み")
            time.sleep(self.retry_sec * (i + 1))
        raise Conflict(f"{path} の書き込みが競合しました")

    def delete(self, loc, path, message):
        for i in range(6):
            cur = self.get_file(loc, path)
            if not cur:
                return False
            status, res = self.call("DELETE", self._contents(loc, path),
                                    {"message": message, "branch": loc[1], "sha": cur[0]})
            if status < 300:
                return True
            if status == 404:
                return False
            if not self._conflict(status, res):
                self._check(status, res, f"{path} の削除")
            time.sleep(self.retry_sec * (i + 1))
        raise Conflict(f"{path} の削除が競合しました")

    def download(self, loc, path):
        status, data = self.call("GET", f"{self._contents(loc, path)}?ref={urllib.parse.quote(loc[1], safe='')}", raw=True)
        self._check(status, data, f"{path} のダウンロード")
        return data

    # ─── ジョブ（inbox ブランチ）───
    def list_jobs(self):
        entries = [e for e in self.list_dir(self.inbox, JOBS_DIR) if e.get("type") == "file" and e["name"].endswith(".json")]
        seen, jobs = {}, []
        for e in entries:
            job = self._jobs_cache.get(e["sha"])
            if job is None:
                f = self.get_file(self.inbox, e["path"])
                if not f or f[1] is None:
                    continue
                job = json.loads(f[1].decode("utf-8"))
            seen[e["sha"]] = job
            jobs.append(dict(job, _sha=e["sha"], _path=e["path"]))
        self._jobs_cache = seen
        return sorted(jobs, key=lambda j: j.get("created_at") or "")

    @staticmethod
    def _plain(job):
        return {k: v for k, v in job.items() if not k.startswith("_")}

    def claim_next(self):
        """queued のうち一番古いものを processing にして返す。sha を条件に書くので、取り合いになっても1台だけが取れる。"""
        for j in (j for j in self.list_jobs() if j.get("status") == "queued"):
            new = dict(self._plain(j), status="processing", started_at=now_iso())
            status, res = self.put(self.inbox, j["_path"], self._dump(new), f"karte-inbox: start {j['id']}", j["_sha"])
            if status < 300:
                return dict(new, _sha=res["content"]["sha"], _path=j["_path"])
            if not self._conflict(status, res):
                self._check(status, res, "ジョブの取得")
        return None

    @staticmethod
    def _dump(obj):
        return (json.dumps(obj, ensure_ascii=False, indent=2) + "\n").encode("utf-8")

    def update_job(self, job, fields):
        """ジョブのファイルを読み直して fields を重ね、書き戻す。更新後のジョブを返す。"""
        path = job.get("_path") or f"{JOBS_DIR}/{job['id']}.json"
        for i in range(6):
            cur = self.get_file(self.inbox, path)
            if not cur or cur[1] is None:
                raise RuntimeError(f"ジョブが見つかりません: {job['id']}")
            new = dict(json.loads(cur[1].decode("utf-8")), **fields)
            status, res = self.put(self.inbox, path, self._dump(new), f"karte-inbox: {job['id']} → {new.get('status')}", cur[0])
            if status < 300:
                return dict(new, _sha=res["content"]["sha"], _path=path)
            if not self._conflict(status, res):
                self._check(status, res, "ジョブの更新")
            time.sleep(self.retry_sec * (i + 1))
        raise Conflict(f"ジョブの更新が競合しました: {job['id']}")

    def release_stale(self):
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=STALE_MINUTES)
        n = 0
        for j in self.list_jobs():
            started = parse_iso(j.get("started_at"))
            if j.get("status") == "processing" and started and started < cutoff:
                self.update_job(j, {"status": "queued"})
                n += 1
        if n:
            log(f"途中で止まっていた {n} 件を読み取り待ちに戻しました")

    def job_image(self, job):
        return self.download(self.inbox, job["image_path"])

    def delete_job(self, job):
        if job.get("image_path"):
            self.delete(self.inbox, job["image_path"], f"karte-inbox: remove photo {job['id']}")
        self.delete(self.inbox, job.get("_path") or f"{JOBS_DIR}/{job['id']}.json", f"karte-inbox: remove job {job['id']}")

    def reset_inbox_if_idle(self):
        """ジョブが1件も無く、しばらく誰も書いていなければ、inbox ブランチを履歴ごと作り直す（写真の履歴を残さない）。"""
        if self.inbox == self.vault or "inbox" not in self.inbox[1]:
            return False  # 作り直しは使い捨てブランチだけ。Vault 本体は絶対に触らない
        status, ref = self.call("GET", f"/repos/{self.inbox[0]}/git/ref/heads/{self._enc(self.inbox[1])}")
        if status == 404:
            return False
        self._check(status, ref, "inbox ブランチの確認")
        if self.list_dir(self.inbox, JOBS_DIR):
            return False
        status, c = self.call("GET", f"/repos/{self.inbox[0]}/git/commits/{ref['object']['sha']}")
        self._check(status, c, "inbox ブランチの確認")
        if not c.get("parents"):
            return False  # もう作り直したばかり
        last = parse_iso((c.get("committer") or {}).get("date"))
        if not last or (datetime.now(timezone.utc) - last).total_seconds() < int(os.environ.get("INBOX_IDLE_SEC", INBOX_IDLE_SEC)):
            return False
        repo = self.inbox[0]
        st, blob = self.call("POST", f"/repos/{repo}/git/blobs", {"content": INBOX_README, "encoding": "utf-8"})
        self._check(st, blob, "inbox の作り直し")
        st, tree = self.call("POST", f"/repos/{repo}/git/trees",
                             {"tree": [{"path": "README.md", "mode": "100644", "type": "blob", "sha": blob["sha"]}]})
        self._check(st, tree, "inbox の作り直し")
        st, commit = self.call("POST", f"/repos/{repo}/git/commits",
                               {"message": "karte-inbox: reset (履歴を作り直し)", "tree": tree["sha"], "parents": []})
        self._check(st, commit, "inbox の作り直し")
        st, r = self.call("PATCH", f"/repos/{repo}/git/refs/heads/{self._enc(self.inbox[1])}",
                          {"sha": commit["sha"], "force": True})
        self._check(st, r, "inbox の作り直し")
        log("inbox ブランチを作り直しました（写真の履歴を破棄）")
        return True

    # ─── 訂正ログと読み癖（Vault の main）───
    def latest_memory(self):
        f = self.get_file(self.vault, MEMORY_FILE)
        return json.loads(f[1].decode("utf-8")) if f and f[1] else None

    @staticmethod
    def feedback_path(name):
        return f"{DIR_FEEDBACK}/{name[:4]}-{name[4:6]}/{name}"

    def feedback_names(self, upto=""):
        """upto（読み癖に取り込み済みの最後のファイル名）より後の訂正ログの名前。取り込み済みの月は見に行かない。"""
        first = f"{upto[:4]}-{upto[4:6]}" if upto else ""
        months = sorted(e["name"] for e in self.list_dir(self.vault, DIR_FEEDBACK) if e.get("type") == "dir" and e["name"] >= first)
        names = [e["name"] for m in months for e in self.list_dir(self.vault, f"{DIR_FEEDBACK}/{m}")
                 if e.get("type") == "file" and e["name"].endswith(".json") and e["name"] > upto]
        return sorted(names)

    def _feedback(self, name):
        if name not in self._fb_cache:
            f = self.get_file(self.vault, self.feedback_path(name))
            self._fb_cache[name] = json.loads(f[1].decode("utf-8")) if f and f[1] else {}
        return dict(self._fb_cache[name], _name=name)

    def pending_names(self, memory):
        return self.feedback_names((memory or {}).get("last_feedback") or "")

    def pending_feedback(self, memory, limit):
        """学習に回す訂正（古い順）。書かれたばかりのものは、同時刻の書き込みを取りこぼさないよう少し待つ。"""
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=SETTLE_SEC)
        names = [n for n in self.pending_names(memory) if name_time(n) and name_time(n) <= cutoff]
        return [self._feedback(n) for n in names[:limit]]

    def recent_feedback(self, memory, limit):
        return [self._feedback(n) for n in self.pending_names(memory)[-limit:]]

    def save_memory(self, memory):
        v = memory["version"]
        data = self._dump(memory)
        self.write(self.vault, f"{DIR_MEMORY}/履歴/v{v:04d}.json", data, f"karte: 読み癖 v{v}（履歴）")
        self.write(self.vault, MEMORY_FILE, data, f"karte: 読み癖 v{v}")
        self.write(self.vault, f"{DIR_MEMORY}/読み癖.md", render_memory_md(memory).encode("utf-8"), f"karte: 読み癖 v{v}（Markdown）")


INBOX_README = (
    "# karte-inbox（使い捨て）\n\n"
    "カルテの手書きノート読み取りで、写真と処理状態を一時的に置くブランチです。\n"
    "完成したカルテ・訂正ログ・読み癖は main の Karte/ にあります。\n"
    "履歴ごと作り直されるので、ここに大事なものを置かないでください。\n"
)


def parse_iso(v):
    if not v:
        return None
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def name_time(name):
    """訂正ログのファイル名 20261008T001122Z_<jobId>_<n>.json から時刻を取り出す。"""
    try:
        return datetime.strptime(name.split("_", 1)[0], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def render_memory_md(m):
    lines = [
        "# 手書きノート読み取りの読み癖（自動生成）", "",
        f"> v{m['version']} ／ 訂正 {m.get('feedback_count', 0)}件から学習 ／ 更新 {str(m.get('created_at', ''))[:10]}  ",
        "> トレーナーの手書きノートを読み取る AI が、訂正から学んだ書き癖です。ノートの文章を扱う他の用途",
        "> （返信文の作成、集計、別の読み取りなど）でも、同じ書き癖として参照できます。手で直さないでください。", "",
        "## 対応表（書かれ方 → 正しい表記）", "",
    ]
    aliases = m.get("aliases") or []
    if aliases:
        lines += ["| 書かれ方 | 正しい表記 | 種類 |", "|---|---|---|"]
        lines += [f"| {a['written']} | {a['correct']} | {a.get('kind', '')} |" for a in aliases]
    else:
        lines.append("（まだありません）")
    lines += ["", "## 読み癖の規則", ""]
    lines += [f"- {r}" for r in m.get("rules") or []] or ["（まだありません）"]
    return "\n".join(lines) + "\n"


def fmt_context(ctx):
    ctx = ctx or {}
    clients = "\n".join(
        f"- {c.get('name')}" + (f"（{c['wu_preset']}）" if c.get("wu_preset") else "")
        for c in ctx.get("clients") or []
    ) or "（なし）"
    presets = "\n".join(
        f"- {p.get('name')}: " + " → ".join(p.get("items") or []) for p in ctx.get("presets") or []
    ) or "（なし）"
    assets = "、".join(a.get("name", "") for a in ctx.get("train_assets") or []) or "（なし）"
    return clients, presets, assets


DIFF_LABELS = {
    "client_name": "顧客名", "date": "日付", "condition": "体調", "notes": "メモ",
    "train_value": "種目の内容", "train_removed": "削除された種目", "train_added": "追加された種目",
    "train_order": "種目の順番", "block": "セッション詳細", "train_renamed": "種目名",
}
KIND_LABELS = {"client": "顧客名", "exercise": "種目名", "preset": "WUプリセット名", "other": "その他"}


def fmt_diff(d):
    # トレーナーが黄色の枠で明示した訂正（書き方の対応・文章の指摘）は、AI の推測より確かな情報
    if d.get("field") == "alias":
        kind = KIND_LABELS.get(d.get("name"), "その他")
        return f"【トレーナーの指定】{kind}「{d.get('ai')}」と書かれていたら「{d.get('final')}」と読む"
    if d.get("field") == "hint":
        return f"【トレーナーの指摘】{d.get('name') or ''} → {d.get('final')}"
    label = DIFF_LABELS.get(d.get("field"), d.get("field") or "?")
    name = f"「{d['name']}」" if d.get("name") else ""

    def v(x):
        if isinstance(x, list):
            return " → ".join(map(str, x))
        return "（空）" if x in (None, "") else str(x)
    return f"{label}{name}: AI「{v(d.get('ai'))}」→ 正しくは「{v(d.get('final'))}」"


def fmt_memory(memory, recent):
    lines = []
    memory = memory or {}
    aliases = memory.get("aliases") or []
    if aliases:
        lines.append("対応表（書かれ方 → 正しい表記）:")
        lines += [f"- 「{a.get('written')}」→「{a.get('correct')}」" for a in aliases if a.get("written") and a.get("correct")]
    rules = (memory.get("rules") or [])[:MAX_RULES]
    if rules:
        lines.append("読み癖の規則:")
        lines += [f"- {r}" for r in rules]
    if recent:
        lines.append("直近の訂正（まだ規則にまとめていないもの。同じ書き方が出たら同じように直す）:")
        for f in recent:
            for d in (f.get("diffs") or [])[:10]:
                lines.append("- " + fmt_diff(d))
    return "\n".join(lines) if lines else "（まだ訂正はありません）"


def build_prompt(image_path, ctx, taken_at=None, memory=None, recent=None):
    clients, presets, assets = fmt_context(ctx)
    taken = (taken_at or now_iso())[:10]
    return PROMPT.format(image=image_path, clients=clients, presets=presets, assets=assets, taken=taken,
                         memory=fmt_memory(memory, recent))


def _num(v):
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return int(f) if f.is_integer() else f


def _str(v):
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def normalize_result(data):
    """claude の出力を検証し、アプリが読む形に整える。形が違えば ValueError。"""
    if isinstance(data, str):
        data = extract_json(data)
    if not isinstance(data, dict) or not isinstance(data.get("karte"), list):
        raise ValueError("karte 配列がありません")
    out = []
    for k in data["karte"]:
        if not isinstance(k, dict):
            raise ValueError("karte の要素がオブジェクトではありません")
        wu = k.get("warmup") if isinstance(k.get("warmup"), dict) else {}
        changes = []
        for c in wu.get("changes") or []:
            if isinstance(c, dict) and c.get("op") in ("replace", "add", "remove", "modify"):
                changes.append({key: _str(c.get(key)) for key in ("op", "from", "to", "name", "after", "value")})
        training = []
        for t in k.get("training") or []:
            if not isinstance(t, dict) or not _str(t.get("name")):
                continue
            sets = []
            for s in t.get("sets") or []:
                if isinstance(s, dict):
                    row = {"kg": _num(s.get("kg")), "reps": _num(s.get("reps")), "sets": _num(s.get("sets"))}
                    if any(v is not None for v in row.values()):
                        sets.append(row)
            training.append({"name": _str(t["name"]), "sets": sets, "text": _str(t.get("text"))})
        date = _str(k.get("date"))
        if date:
            try:
                date = datetime.strptime(date, "%Y-%m-%d").strftime("%Y-%m-%d")
            except ValueError:
                date = None
        out.append({
            "client_name": _str(k.get("client_name")),
            "client_confidence": "low" if k.get("client_confidence") == "low" else "high",
            "date": date,
            "condition": _str(k.get("condition")),
            "warmup": {"preset": _str(wu.get("preset")), "changes": changes},
            "training": training,
            "notes": _str(k.get("notes")),
            "uncertain": [s for s in (_str(u) for u in k.get("uncertain") or []) if s],
        })
    return {"karte": out}


def extract_json(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0]
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("JSON が見つかりません")
    return json.loads(text[start:end + 1])


class NotLoggedIn(RuntimeError):
    pass


def claude_cmd():
    # Windows では claude.exe（公式のインストーラ）か claude.cmd（npm）のどちらか。PATH から探す
    return os.environ.get("CLAUDE_CMD") or shutil.which("claude") or ("claude.cmd" if os.name == "nt" else "claude")


def call_claude(prompt, schema, workdir):
    """claude -p をサブスクのログインで実行し、スキーマどおりの JSON を返す。"""
    cmd = [
        claude_cmd(), "-p",
        "--output-format", "json",
        "--json-schema", json.dumps(schema, ensure_ascii=False),
        "--allowedTools", "Read",
        "--strict-mcp-config",
    ]
    if os.environ.get("CLAUDE_MODEL"):
        cmd += ["--model", os.environ["CLAUDE_MODEL"]]
    # プロンプトは標準入力で渡す（Windows のコマンドライン長・引用符の問題を避ける）
    proc = subprocess.run(
        cmd, input=prompt, capture_output=True, text=True,
        encoding="utf-8", cwd=workdir, timeout=int(os.environ.get("CLAUDE_TIMEOUT_SEC", "300")),
    )
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "").strip()[-400:]
        if "Not logged in" in msg or "login" in msg.lower():
            raise NotLoggedIn("Claude Code にログインしていません（claude を起動して /login してください）")
        raise RuntimeError(f"claude が失敗しました: {msg}")
    out = json.loads(proc.stdout)
    if out.get("is_error"):
        raise RuntimeError(f"claude がエラーを返しました: {str(out.get('result'))[:400]}")
    return out.get("structured_output") or out.get("result") or ""


def run_claude(image_path, ctx, workdir, taken_at=None, memory=None, recent=None):
    prompt = build_prompt(image_path, ctx, taken_at, memory, recent)
    return normalize_result(call_claude(prompt, OUTPUT_SCHEMA, workdir))


def process(sb, job):
    attempts = (job.get("attempts") or 0) + 1
    job = sb.update_job(job, {"attempts": attempts})
    log(f"読み取り開始 {job['id']}（{job.get('filename') or '写真'}）")
    workdir = tempfile.mkdtemp(prefix="karte-ocr-")
    try:
        ext = os.path.splitext(job["image_path"])[1] or ".jpg"
        image = os.path.join(workdir, "page" + ext)
        with open(image, "wb") as f:
            f.write(sb.job_image(job))
        last = None
        for _ in range(MAX_ATTEMPTS):
            try:
                result = run_claude(image, job.get("context"), workdir, job.get("taken_at"),
                                    *load_learning(sb))
                break
            except (ValueError, json.JSONDecodeError) as e:
                last = e  # 出力の形が崩れたときだけ再試行する
        else:
            raise RuntimeError(f"読み取り結果の形式が不正です: {last}")
        # 写真はまだ消さない：訂正があれば学習に使う（訂正が無ければ取り込み後に api/feedback が消す）
        sb.update_job(job, {"status": "done", "result": result, "error": None, "processed_at": now_iso()})
        log(f"完了 {job['id']}：{len(result['karte'])}件")
    except NotLoggedIn:
        sb.update_job(job, {"status": "queued", "attempts": attempts - 1})  # 写真の問題ではないので待ちに戻す
        raise
    except Exception as e:  # noqa: BLE001 — ジョブ単位で記録して次へ進む
        sb.update_job(job, {"status": "error", "error": str(e)[:500], "processed_at": now_iso()})
        log(f"エラー {job['id']}：{e}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def load_learning(sb):
    """読み取りプロンプトに入れる学習内容（最新版の読み癖＋まだまとめていない直近の訂正）。"""
    try:
        memory = sb.latest_memory()
        return memory, sb.recent_feedback(memory, RECENT_FEEDBACK)
    except (urllib.error.URLError, RuntimeError) as e:
        log(f"学習内容を読めませんでした（読み取りは続けます）：{e}")
        return None, []


MEMORY_SCHEMA = {
    "type": "object",
    "properties": {
        "rules": {"type": "array", "items": {"type": "string"}},
        "aliases": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "written": {"type": "string"},
                    "correct": {"type": "string"},
                    "kind": {"type": "string", "enum": ["client", "exercise", "preset", "other"]},
                },
                "required": ["written", "correct", "kind"],
            },
        },
    },
    "required": ["rules", "aliases"],
}

CONSOLIDATE_PROMPT = """あなたは、パーソナルジムのトレーナーの手書きノートを読み取る AI の「読み癖ノート」を管理する係です。
読み取り AI が出した下書きを、トレーナーが訂正しました。写真・AI の読み取り・訂正後の内容を見比べて、
次回から同じ間違いをしないための規則と対応表を更新してください。

## 手順
1. 下の各ケースについて、写真（Read ツールで開く）を見て、なぜ読み間違えたかを確かめる。
   字の形の癖（例:「7」と「1」が似ている、「バ」の濁点が薄い）、略記（例:「SQ」＝スクワット）、書く位置の決まりなどを見つける。
2. 書かれ方と正しい表記が1対1で決まるもの（略記・崩し字・綴り違い）は aliases に入れる。kind は client / exercise / preset / other。
3. それ以外の傾向は rules に、読み取り AI がそのまま守れる具体的な指示として短い日本語で書く。
   読み取り AI の出力項目は client_name / date / condition / warmup(preset, changes) / training(name, sets[kg, reps, sets], text) / notes / uncertain。規則で項目に触れるときはこの名前を使う。
4. 単なる内容の追記（AI の読み違いではなく、トレーナーが後から書き足しただけ）は学習しない。
5. 【トレーナーの指定】（書き方の対応）は必ず aliases に入れる。【トレーナーの指摘】（文章）は、写真と見比べて、次回から守れる一般的な規則に直して rules に入れる。どちらも推測より優先する。

## 忘れないためのルール（厳守）
- 既存の rules と aliases は、新しい訂正と矛盾しない限り**すべてそのまま残す**。言い換えて短くまとめるのはよいが、意味を落とさない。
- 新しい訂正と矛盾する古い規則だけ、新しい内容に置き換える。
- rules は最大 {max_rules} 件。似た規則は1つにまとめる。

## 既存の読み癖
rules:
{rules}
aliases:
{aliases}

## 今回の訂正
{cases}
"""


def fmt_case(i, fb, image):
    draft = json.dumps(fb.get("draft") or {}, ensure_ascii=False)
    final = json.dumps(fb.get("final") or {}, ensure_ascii=False)
    diffs = "\n".join("  - " + fmt_diff(d) for d in fb.get("diffs") or [])
    img = f"写真: {image}" if image else "写真: （残っていない。テキストだけで判断する）"
    return f"### ケース{i}\n{img}\nAI の読み取り: {draft}\n訂正後: {final}\n差分:\n{diffs}"


def explicit_aliases(feedback):
    """黄色の枠でトレーナーが指定した「書かれ方 → 正しい表記」。要約の結果に関係なく、必ず対応表に入れる。"""
    out = []
    for fb in feedback:
        for d in fb.get("diffs") or []:
            if d.get("field") == "alias" and _str(d.get("ai")) and _str(d.get("final")):
                kind = d.get("name") if d.get("name") in KIND_LABELS else "other"
                out.append({"written": _str(d["ai"]), "correct": _str(d["final"]), "kind": kind})
    return out


def merge_memory(old, new):
    """取りこぼし防止：対応表は和集合（同じ書かれ方は新しい方を採用）、規則は激減したら採用しない。"""
    old = old or {}
    old_rules = old.get("rules") or []
    rules = [r.strip() for r in new.get("rules") or [] if isinstance(r, str) and r.strip()][:MAX_RULES]
    if old_rules and len(rules) < max(1, int(len(old_rules) * 0.7)):
        raise ValueError(f"規則が {len(old_rules)} 件から {len(rules)} 件に減ったため、忘れている可能性があるので採用しません")
    aliases = {}
    for a in (old.get("aliases") or []) + (new.get("aliases") or []):
        if isinstance(a, dict) and _str(a.get("written")) and _str(a.get("correct")) and a["written"] != a["correct"]:
            kind = a.get("kind") if a.get("kind") in ("client", "exercise", "preset", "other") else "other"
            aliases[(kind, a["written"].strip())] = {"written": a["written"].strip(), "correct": a["correct"].strip(), "kind": kind}
    return {"rules": rules, "aliases": list(aliases.values())}


def consolidate(sb):
    """まだ学習に回っていない訂正を写真と見比べ、読み癖ノートの新しい版を作る。"""
    old = sb.latest_memory() or {"version": 0, "rules": [], "aliases": [], "feedback_count": 0}
    pending = sb.pending_feedback(old, CONSOLIDATE_BATCH)
    if not pending:
        return False
    jobs = {j["id"]: j for j in sb.list_jobs()}
    workdir = tempfile.mkdtemp(prefix="karte-learn-")
    try:
        cases = []
        for i, fb in enumerate(pending, 1):
            job, image = jobs.get(fb.get("job_id")), None
            if job and job.get("image_path"):
                try:
                    image = os.path.join(workdir, f"case{i}" + (os.path.splitext(job["image_path"])[1] or ".jpg"))
                    with open(image, "wb") as f:
                        f.write(sb.job_image(job))
                except (urllib.error.URLError, RuntimeError):
                    image = None
            cases.append(fmt_case(i, fb, image))
        prompt = CONSOLIDATE_PROMPT.format(
            max_rules=MAX_RULES,
            rules="\n".join(f"- {r}" for r in old.get("rules") or []) or "（なし）",
            aliases="\n".join(f"- 「{a['written']}」→「{a['correct']}」({a.get('kind')})" for a in old.get("aliases") or []) or "（なし）",
            cases="\n\n".join(cases),
        )
        log(f"学習開始：訂正 {len(pending)} 件")
        new = call_claude(prompt, MEMORY_SCHEMA, workdir)
        if isinstance(new, str):
            new = extract_json(new)
        merged = merge_memory(old, new)
        merged = merge_memory(merged, {"rules": merged["rules"], "aliases": explicit_aliases(pending)})
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    version = (old.get("version") or 0) + 1
    upto = pending[-1]["_name"]
    sb.save_memory({
        "version": version, "created_at": now_iso(), "rules": merged["rules"], "aliases": merged["aliases"],
        "feedback_count": (old.get("feedback_count") or 0) + len(pending), "last_feedback": upto,
    })
    # 学習に使い終わった写真を消す（その写真の訂正がすべて学習済みのものだけ）
    left = sb.feedback_names(upto)
    for job in jobs.values():
        if job.get("status") == "feedback" and not any(job["id"] in n for n in left):
            sb.delete_job(job)
    log(f"学習完了：読み癖 v{version}（規則 {len(merged['rules'])} 件・対応表 {len(merged['aliases'])} 件）")
    return True


def cleanup(sb):
    """訂正の報告が来ないまま残った写真を期限で消し、空になった inbox ブランチは履歴ごと作り直す。"""
    now = datetime.now(timezone.utc)
    stale = []
    for j in sb.list_jobs():
        if j.get("status") == "imported" and (parse_iso(j.get("imported_at")) or now) < now - timedelta(days=IMPORTED_KEEP_DAYS):
            stale.append(j)
        elif j.get("status") == "feedback" and (parse_iso(j.get("processed_at")) or now) < now - timedelta(days=FEEDBACK_KEEP_DAYS):
            stale.append(j)
    for job in stale:
        sb.delete_job(job)
    if stale:
        log(f"期限切れの写真 {len(stale)} 件を削除しました")
    sb.reset_inbox_if_idle()


def make_store():
    repo = os.environ.get("GITHUB_REPO") or DEFAULT_REPO
    vault = (repo, os.environ.get("VAULT_BRANCH") or "main")
    inbox = (os.environ.get("INBOX_REPO") or repo, os.environ.get("INBOX_BRANCH") or "karte-inbox")
    return GitHubStore(os.environ["GITHUB_TOKEN"], vault, inbox,
                       os.environ.get("GITHUB_API_URL") or "https://api.github.com",
                       float(os.environ.get("KARTE_RETRY_SEC", "0.4")))


def check_env():
    if os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY が設定されています。API 課金になるため起動しません。環境変数から削除してください。")
    if not os.environ.get("GITHUB_TOKEN"):
        sys.exit("GITHUB_TOKEN が .env にありません（.env.example を参照）")
    sb = make_store()
    if sb.inbox == sb.vault:
        sys.exit("INBOX_BRANCH が Vault のブランチと同じです。使い捨てブランチ（例: karte-inbox）を指定してください。")
    if not shutil.which(claude_cmd()):
        sys.exit(f"{claude_cmd()} が見つかりません。Claude Code をインストールしてください。")


CHECK_PROMPT = """これは動作確認です。写真はありません。karte に空の配列を入れた JSON だけを返してください。"""


def self_check():
    """環境の確認。1項目ずつ [OK]/[NG] を表示し、すべて OK なら True を返す。"""
    ok = True

    def line(good, msg):
        nonlocal ok
        ok = ok and good
        print(("[OK] " if good else "[NG] ") + msg, flush=True)

    api_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    line(not api_key, "ANTHROPIC_API_KEY は未設定（サブスクの枠で動きます）" if not api_key else
         "ANTHROPIC_API_KEY が設定されています。API 課金になるため使いません（環境変数から外してください）")

    cmd = shutil.which(claude_cmd())
    line(bool(cmd), f"Claude Code: {cmd}" if cmd else
         "claude が見つかりません。Claude Code をインストールしてください（見つかる場所に PATH を通す）")

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        line(False, "GITHUB_TOKEN が .env にありません（.env.example を参照）")
        return False
    sb = make_store()
    if sb.inbox == sb.vault:
        line(False, "INBOX_BRANCH が Vault のブランチと同じです。使い捨てブランチ（例: karte-inbox）を指定してください")
        return False
    repo, branch = sb.vault
    try:
        status, data = sb.call("GET", f"/repos/{repo}")
        if status == 200:
            perms = (data or {}).get("permissions")
            if perms is not None and not perms.get("push"):
                line(False, f"GitHub: {repo} は読めますが、書き込み権限がありません（トークンの Contents を Read and write にしてください）")
            else:
                line(True, f"GitHub: {repo} に接続できました" + ("" if perms is not None else "（書き込み権限は確認できませんでした）"))
        elif status in (401, 403):
            line(False, f"GitHub: トークンが使えません（{status}）。期限切れ・権限不足・値の貼り間違いのいずれかです")
        elif status == 404:
            line(False, f"GitHub: {repo} が見えません。トークンの対象リポジトリに含まれていないか、リポジトリ名が違います")
        else:
            line(False, f"GitHub: 予期しない応答です（{status}）")
        if status == 200:
            st, _ = sb.call("GET", f"/repos/{repo}/branches/{sb._enc(branch)}")
            line(st == 200, f"Vault のブランチ {branch}" + (" があります" if st == 200 else " が見つかりません（VAULT_BRANCH を確認）"))
    except urllib.error.URLError as e:
        line(False, f"GitHub に接続できません：{e}")
    if not cmd or api_key:
        return False
    workdir = tempfile.mkdtemp(prefix="karte-check-")
    try:
        result = normalize_result(call_claude(CHECK_PROMPT, OUTPUT_SCHEMA, workdir))
        line(result == {"karte": []}, "claude -p の呼び出し（サブスクのログインで動作）" if result == {"karte": []}
             else f"claude -p の返答が想定と違います：{result}")
    except NotLoggedIn as e:
        line(False, str(e))
    except subprocess.TimeoutExpired:
        line(False, "claude -p が時間内に終わりませんでした（ログイン画面で止まっていないか確認）")
    except (RuntimeError, ValueError, json.JSONDecodeError) as e:
        line(False, f"claude -p の呼び出しに失敗：{e}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="溜まっている分だけ処理して終了")
    ap.add_argument("--check", action="store_true", help="環境の確認だけ行って終了")
    args = ap.parse_args()
    load_env()
    if args.check:
        sys.exit(0 if self_check() else 1)
    check_env()
    sb = make_store()
    poll = int(os.environ.get("POLL_SEC", "30"))
    learn_retry_at = learn_check_at = 0
    log("ocr-worker 起動" + ("（--once）" if args.once else f"（{poll}秒ごとに確認）"))
    while True:
        try:
            sb.release_stale()
            worked = False
            while True:
                job = sb.claim_next()
                if not job:
                    break
                process(sb, job)
                worked = True
            # 読み取り待ちが無いときに学習する（読み取りを優先）。失敗したら30分は再挑戦しない
            if worked or args.once or time.time() >= learn_check_at:
                learn_check_at = time.time() + LEARN_CHECK_SEC
                while time.time() >= learn_retry_at:
                    try:
                        if not consolidate(sb):
                            break
                    except (ValueError, RuntimeError, json.JSONDecodeError) as e:
                        log(f"学習に失敗しました（訂正は残っているので30分後にやり直します）：{e}")
                        learn_retry_at = time.time() + 1800
                        break
                cleanup(sb)
        except NotLoggedIn as e:
            sys.exit(str(e))
        except urllib.error.URLError as e:
            log(f"GitHub に接続できません：{e}")
        except RuntimeError as e:  # GitHub の一時的なエラーや書き込みの競合：次の巡回でやり直す
            log(f"GitHub の処理に失敗しました（次の巡回でやり直します）：{e}")
        if args.once:
            log("処理待ちはありません。終了します")
            return
        time.sleep(poll)


if __name__ == "__main__":
    main()
