"""手書きノート読み取りワーカー（ノートPCで動かす）

Supabase の karte_ocr_jobs から status=queued のジョブを取り出し、
ログイン済みの Claude Code CLI（`claude -p`、サブスクの枠内）で写真を読み取って結果を書き戻す。
API キーは使わない。

  python worker.py          常駐（POLL_SEC 秒ごとに確認）
  python worker.py --once   溜まっている分だけ処理して終了

設定は同じフォルダの .env（.env.example 参照）。依存は標準ライブラリのみ。
"""
import argparse
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
TABLE = "karte_ocr_jobs"
BUCKET = "karte-ocr"
STALE_MINUTES = 15
MAX_ATTEMPTS = 2

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
    - 追加: {{"op":"add","name":"種目","after":"直前の種目 または null"}}
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


def load_env():
    path = os.path.join(HERE, ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


class Supabase:
    def __init__(self, url, key):
        self.url = url.rstrip("/")
        self.key = key

    def request(self, method, path, body=None, headers=None, raw=False):
        h = {"apikey": self.key, "Authorization": f"Bearer {self.key}"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        req = urllib.request.Request(self.url + path, data=data, headers=h, method=method)
        with urllib.request.urlopen(req, timeout=60) as res:
            content = res.read()
        if raw:
            return content
        return json.loads(content) if content else None

    def table(self, query):
        return f"/rest/v1/{TABLE}?{query}"

    def claim_next(self):
        rows = self.request("GET", self.table("select=id&status=eq.queued&order=created_at.asc&limit=1"))
        if not rows:
            return None
        # status=eq.queued を条件に更新し、取り合いになっても1台だけが取れるようにする
        got = self.request(
            "PATCH",
            self.table(f"id=eq.{rows[0]['id']}&status=eq.queued"),
            {"status": "processing", "started_at": now_iso()},
            {"Prefer": "return=representation"},
        )
        return got[0] if got else None

    def release_stale(self):
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=STALE_MINUTES)).isoformat()
        q = "status=eq.processing&started_at=lt." + urllib.parse.quote(cutoff)
        rows = self.request("PATCH", self.table(q), {"status": "queued"}, {"Prefer": "return=representation"})
        if rows:
            log(f"途中で止まっていた {len(rows)} 件を読み取り待ちに戻しました")

    def update(self, job_id, fields):
        self.request("PATCH", self.table(f"id=eq.{job_id}"), fields, {"Prefer": "return=minimal"})

    def download(self, path):
        return self.request("GET", f"/storage/v1/object/{BUCKET}/{path}", raw=True)

    def remove(self, path):
        try:
            self.request("DELETE", f"/storage/v1/object/{BUCKET}/{path}", raw=True)
        except urllib.error.HTTPError as e:
            if e.code not in (400, 404):
                raise


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


def build_prompt(image_path, ctx, taken_at=None):
    clients, presets, assets = fmt_context(ctx)
    taken = (taken_at or now_iso())[:10]
    return PROMPT.format(image=image_path, clients=clients, presets=presets, assets=assets, taken=taken)


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
    return os.environ.get("CLAUDE_CMD") or ("claude.cmd" if os.name == "nt" else "claude")


def run_claude(image_path, ctx, workdir, taken_at=None):
    cmd = [
        claude_cmd(), "-p",
        "--output-format", "json",
        "--json-schema", json.dumps(OUTPUT_SCHEMA, ensure_ascii=False),
        "--allowedTools", "Read",
        "--strict-mcp-config",
    ]
    if os.environ.get("CLAUDE_MODEL"):
        cmd += ["--model", os.environ["CLAUDE_MODEL"]]
    # プロンプトは標準入力で渡す（Windows のコマンドライン長・引用符の問題を避ける）
    proc = subprocess.run(
        cmd, input=build_prompt(image_path, ctx, taken_at), capture_output=True, text=True,
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
    return normalize_result(out.get("structured_output") or out.get("result") or "")


def process(sb, job):
    attempts = (job.get("attempts") or 0) + 1
    sb.update(job["id"], {"attempts": attempts})
    log(f"読み取り開始 {job['id']}（{job.get('filename') or '写真'}）")
    workdir = tempfile.mkdtemp(prefix="karte-ocr-")
    try:
        ext = os.path.splitext(job["image_path"])[1] or ".jpg"
        image = os.path.join(workdir, "page" + ext)
        with open(image, "wb") as f:
            f.write(sb.download(job["image_path"]))
        last = None
        for _ in range(MAX_ATTEMPTS):
            try:
                result = run_claude(image, job.get("context"), workdir, job.get("taken_at"))
                break
            except (ValueError, json.JSONDecodeError) as e:
                last = e  # 出力の形が崩れたときだけ再試行する
        else:
            raise RuntimeError(f"読み取り結果の形式が不正です: {last}")
        sb.update(job["id"], {"status": "done", "result": result, "error": None, "processed_at": now_iso()})
        sb.remove(job["image_path"])  # 読み取り済みの写真はすぐ消す
        log(f"完了 {job['id']}：{len(result['karte'])}件")
    except NotLoggedIn:
        sb.update(job["id"], {"status": "queued", "attempts": attempts - 1})  # 写真の問題ではないので待ちに戻す
        raise
    except Exception as e:  # noqa: BLE001 — ジョブ単位で記録して次へ進む
        sb.update(job["id"], {"status": "error", "error": str(e)[:500], "processed_at": now_iso()})
        log(f"エラー {job['id']}：{e}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def check_env():
    if os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY が設定されています。API 課金になるため起動しません。環境変数から削除してください。")
    for k in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY"):
        if not os.environ.get(k):
            sys.exit(f"{k} が .env にありません（.env.example を参照）")
    if not shutil.which(claude_cmd()):
        sys.exit(f"{claude_cmd()} が見つかりません。Claude Code をインストールしてください。")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="溜まっている分だけ処理して終了")
    args = ap.parse_args()
    load_env()
    check_env()
    sb = Supabase(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])
    poll = int(os.environ.get("POLL_SEC", "30"))
    log("ocr-worker 起動" + ("（--once）" if args.once else f"（{poll}秒ごとに確認）"))
    while True:
        try:
            sb.release_stale()
            while True:
                job = sb.claim_next()
                if not job:
                    break
                process(sb, job)
        except NotLoggedIn as e:
            sys.exit(str(e))
        except urllib.error.URLError as e:
            log(f"Supabase に接続できません：{e}")
        if args.once:
            log("処理待ちはありません。終了します")
            return
        time.sleep(poll)


if __name__ == "__main__":
    main()
