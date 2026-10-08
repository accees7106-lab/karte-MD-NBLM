# トレーニングカルテ

セッション記録・カルテ生成（単体 `index.html`、データは端末の localStorage）。

## 手書きノートの読み取り

```
スマホ ─写真─▶ Vercel (/api/jobs) ─▶ Supabase（写真＋ジョブ）
                                         ▲ 取りに行く
ノートPC ocr-worker ── claude -p（Claude のサブスク・API キー不要）で読み取り → 結果を書き戻す
スマホ ◀─「結果を取り込む」─ 下書き → 確認・修正 → 「カルテを生成する」
```

- **AI の処理はノートPCで、ログイン済みの Claude Code が行う。** Vercel では AI を呼ばず、API キーも使わない。
- ノートPCが閉じている間、写真は「読み取り待ち」のまま溜まるだけで、消えない。
- **訂正から学習して、次回以降に反映する。**
  1. 下書きを直して「カルテを生成する」で保存すると、「AI の読み取り」と「直した内容」の差分がサーバーに記録される。
  2. ノートPCの ocr-worker は、手が空いたときに写真・読み取り・訂正を見比べて、読み癖を要約する。要約は「規則」と「対応表（例: ワイドSQ→ワイドスクワット）」の2つ。
  3. 毎回の読み取りプロンプトに「過去の訂正から学んだこと（最優先で必ず守る）」として入る。まだ要約していない直近の訂正もそのまま入る。
  4. アプリ側でも、対応表どおりに名前を直してから下書きにする。
- **忘れない仕組み**
  - 訂正の記録は消さない（`karte_ocr_feedback`）。
  - 読み癖は上書きせず、版を重ねて追記する（`karte_ocr_memory`）。
  - 要約し直すときは、既存の規則を残すよう指示している。規則が3割以上減ったり対応表が消えたりする要約は採用しない。
  - 学習した内容は「設定・データ移行」→「学習した読み癖」で見られる。
- 写真の扱い
  - 訂正が無かった写真は、取り込みが済んだらすぐ消える。
  - 訂正があった写真は、学習が済んだら消える。
  - 取り込んだまま14日間保存されなかった写真も消える。
- 書き込むときのルール
  - **WU（ウォームアップ）**：「プリセット名＋差分」で書く。
    - アプリのプリセット名は、紙に書く名前と同じにしておく。
    - プリセット名を書かなかったページでは、その顧客が前回使ったプリセットを使う。
  - **体調**：セッション詳細の「体調」ブロックに入る。
  - **読めなかった箇所**：下書きの上に黄色で表示される。

## セットアップ

### 1. Supabase
1. SQL Editor で `supabase/migrations/20261007000000_karte_ocr_jobs.sql` を実行する。テーブル `karte_ocr_jobs` と非公開バケット `karte-ocr` ができる。
2. Project Settings → API で、次の2つを控える。
   - `Project URL`
   - `service_role` key（**秘密。ブラウザや git には絶対に入れない**）

### 2. Vercel
1. このリポジトリを Import する。Framework は Other、Build Command は空。
2. Environment Variables を設定する。

   | 変数 | 値 |
   |---|---|
   | `APP_TOKEN` | 自分で決めた長いランダム文字列（例: `openssl rand -hex 24`） |
   | `SUPABASE_URL` | 1 の Project URL |
   | `SUPABASE_SERVICE_ROLE_KEY` | 1 の service_role key |
   | `ALLOWED_ORIGINS` | （任意）別ドメインのページから API を呼ぶ場合だけ指定。例: `https://accees7106-lab.github.io` |

3. Deploy したら、スマホで `https://<プロジェクト>.vercel.app/` を開く。
4. 「📷 手書きノートから読み込み」→「設定・データ移行」に `APP_TOKEN` と同じ値を入れて保存する。

> **データの引っ越し：** localStorage は URL（ドメイン）ごとに別です。GitHub Pages 版で使っていたデータは、Vercel 版には自動では移りません。
> 1. 旧 URL で「⬇ 全データを書き出し」を押す。
> 2. 新 URL で「⬆ 読み込み」を押して、書き出したファイルを選ぶ。

### 3. ノートPC（Windows）
1. Python 3.10 以上と Claude Code を入れる。`claude` を一度起動して `/login` する（サブスクのアカウントで）。
2. このリポジトリの `ocr-worker` フォルダを置く（clone でも可）。
3. `ocr-worker/.env.example` を `.env` にコピーし、`SUPABASE_URL` と `SUPABASE_SERVICE_ROLE_KEY` を入れる。
4. 動作確認：`python worker.py --once`（溜まっている分を処理して終了する）
5. ログオン時に自動で起動させたい場合は、次を1回だけ実行する。ログは `ocr-worker.log`。

   ```
   powershell -ExecutionPolicy Bypass -File .\register_task.ps1
   ```

- 環境変数に `ANTHROPIC_API_KEY` があると API 課金になってしまうため、ワーカーは起動を拒否する。
- `CLAUDE_MODEL=opus` などでモデルを指定できる（空ならサブスクの既定）。

## 開発

```
npm install
npm test            # API と ocr-worker の単体テスト
npm run test:e2e    # 画面の E2E（/api/jobs はモック。Chromium が必要）
```

- 並べ替え：各項目の左にある ⠿ をドラッグする。マウスとタッチの両方で動く（`vendor/Sortable.min.js`）。
