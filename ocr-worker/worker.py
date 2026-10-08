"""手書きノート読み取りワーカー（ノートPCで動かす）

Supabase の karte_ocr_jobs から status=queued のジョブを取り出し、
ログイン済みの Claude Code CLI（`claude -p`、サブスクの枠内）で写真を読み取って結果を書き戻す。
API キーは使わない。

学習：トレーナーが下書きを直して保存すると、その差分が karte_ocr_feedback に記録される。
手が空いたときに「写真＋AI の読み取り＋直した結果」を見比べて読み癖を要約し、
karte_ocr_memory に新しい版として追記する（上書きしない＝忘れない）。毎回の読み取りプロンプトに必ず入れる。

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

    def latest_memory(self):
        rows = self.request("GET", "/rest/v1/karte_ocr_memory?select=*&order=version.desc&limit=1")
        return rows[0] if rows else None

    def pending_feedback(self, limit):
        return self.request(
            "GET", f"/rest/v1/karte_ocr_feedback?select=*&consolidated=eq.false&order=created_at.asc&limit={limit}") or []

    def recent_feedback(self, limit):
        return self.request(
            "GET", f"/rest/v1/karte_ocr_feedback?select=diffs&consolidated=eq.false&order=created_at.desc&limit={limit}") or []

    def jobs_by_ids(self, ids):
        if not ids:
            return []
        return self.request("GET", self.table("select=id,image_path,status&id=in.(" + ",".join(ids) + ")")) or []

    def delete_job(self, job):
        self.request("DELETE", self.table(f"id=eq.{job['id']}"), headers={"Prefer": "return=minimal"})
        if job.get("image_path"):
            self.remove(job["image_path"])

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


DIFF_LABELS = {
    "client_name": "顧客名", "date": "日付", "condition": "体調", "notes": "メモ",
    "train_value": "種目の内容", "train_removed": "削除された種目", "train_added": "追加された種目",
    "train_order": "種目の順番", "block": "セッション詳細",
}


def fmt_diff(d):
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
    return os.environ.get("CLAUDE_CMD") or ("claude.cmd" if os.name == "nt" else "claude")


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
                result = run_claude(image, job.get("context"), workdir, job.get("taken_at"),
                                    *load_learning(sb))
                break
            except (ValueError, json.JSONDecodeError) as e:
                last = e  # 出力の形が崩れたときだけ再試行する
        else:
            raise RuntimeError(f"読み取り結果の形式が不正です: {last}")
        # 写真はまだ消さない：訂正があれば学習に使う（訂正が無ければ取り込み後に api/feedback が消す）
        sb.update(job["id"], {"status": "done", "result": result, "error": None, "processed_at": now_iso()})
        log(f"完了 {job['id']}：{len(result['karte'])}件")
    except NotLoggedIn:
        sb.update(job["id"], {"status": "queued", "attempts": attempts - 1})  # 写真の問題ではないので待ちに戻す
        raise
    except Exception as e:  # noqa: BLE001 — ジョブ単位で記録して次へ進む
        sb.update(job["id"], {"status": "error", "error": str(e)[:500], "processed_at": now_iso()})
        log(f"エラー {job['id']}：{e}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def load_learning(sb):
    """読み取りプロンプトに入れる学習内容（最新版の読み癖＋まだまとめていない直近の訂正）。"""
    try:
        return sb.latest_memory(), sb.recent_feedback(RECENT_FEEDBACK)
    except urllib.error.HTTPError as e:
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
    pending = sb.pending_feedback(CONSOLIDATE_BATCH)
    if not pending:
        return False
    old = sb.latest_memory() or {"version": 0, "rules": [], "aliases": [], "feedback_count": 0}
    jobs = {j["id"]: j for j in sb.jobs_by_ids(sorted({f["job_id"] for f in pending}))}
    workdir = tempfile.mkdtemp(prefix="karte-learn-")
    try:
        cases = []
        for i, fb in enumerate(pending, 1):
            job, image = jobs.get(fb["job_id"]), None
            if job and job.get("image_path"):
                try:
                    image = os.path.join(workdir, f"case{i}" + (os.path.splitext(job["image_path"])[1] or ".jpg"))
                    with open(image, "wb") as f:
                        f.write(sb.download(job["image_path"]))
                except urllib.error.HTTPError:
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
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    version = (old.get("version") or 0) + 1
    sb.request("POST", "/rest/v1/karte_ocr_memory", {
        "version": version, "rules": merged["rules"], "aliases": merged["aliases"],
        "feedback_count": (old.get("feedback_count") or 0) + len(pending),
    }, {"Prefer": "return=minimal"})
    ids = ",".join(str(f["id"]) for f in pending)
    sb.request("PATCH", f"/rest/v1/karte_ocr_feedback?id=in.({ids})",
               {"consolidated": True, "consolidated_at": now_iso(), "memory_version": version},
               {"Prefer": "return=minimal"})
    # 学習に使い終わった写真を消す（その写真の訂正がすべて学習済みのものだけ）
    for job in jobs.values():
        if job.get("status") != "feedback":
            continue
        left = sb.request("GET", f"/rest/v1/karte_ocr_feedback?select=id&job_id=eq.{job['id']}&consolidated=eq.false&limit=1")
        if not left:
            sb.delete_job(job)
    log(f"学習完了：読み癖 v{version}（規則 {len(merged['rules'])} 件・対応表 {len(merged['aliases'])} 件）")
    return True


def cleanup(sb):
    """訂正の報告が来ないまま残った写真を期限で消す。"""
    def older(days):
        return urllib.parse.quote((datetime.now(timezone.utc) - timedelta(days=days)).isoformat())
    stale = (sb.request("GET", sb.table(f"select=id,image_path,status&status=eq.imported&imported_at=lt.{older(IMPORTED_KEEP_DAYS)}")) or []) + \
            (sb.request("GET", sb.table(f"select=id,image_path,status&status=eq.feedback&processed_at=lt.{older(FEEDBACK_KEEP_DAYS)}")) or [])
    for job in stale:
        sb.delete_job(job)
    if stale:
        log(f"期限切れの写真 {len(stale)} 件を削除しました")


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
    learn_retry_at = 0
    log("ocr-worker 起動" + ("（--once）" if args.once else f"（{poll}秒ごとに確認）"))
    while True:
        try:
            sb.release_stale()
            while True:
                job = sb.claim_next()
                if not job:
                    break
                process(sb, job)
            # 読み取り待ちが無いときに学習する（読み取りを優先）。失敗したら30分は再挑戦しない
            while time.time() >= learn_retry_at and not sb.request("GET", sb.table("select=id&status=eq.queued&limit=1")):
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
            log(f"Supabase に接続できません：{e}")
        if args.once:
            log("処理待ちはありません。終了します")
            return
        time.sleep(poll)


if __name__ == "__main__":
    main()
