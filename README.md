# トレーニングカルテ

セッション記録・カルテ生成（単体 `index.html`、データは端末の localStorage）。

## 手書きノートの読み取り

保存先は **Vault（GitHub の `obsidian-vault` リポジトリ）** です。DB のサービスは使いません。

```
スマホ ─写真─▶ Vercel (/api/jobs) ─▶ Vault リポジトリの使い捨てブランチ karte-inbox（写真＋処理状態）
                                         ▲ 取りに行く（GitHub API）
ノートPC ocr-worker ── claude -p（Claude のサブスク・API キー不要）で読み取り → 結果を書き戻す
スマホ ◀─「結果を取り込む」─ 下書き → 確認・修正 → 「カルテを生成する」
                                          └▶ Vault の main に カルテ・訂正ログ を保存
ノートPC ocr-worker ── 訂正から読み癖を学習 ─▶ Vault の main に 読み癖 を保存（次回の読み取りに必ず使う）
```

- **AI の処理はノートPCで、ログイン済みの Claude Code が行う。** Vercel では AI を呼ばず、API キーも使わない。
- ノートPCが閉じている間、写真は「読み取り待ち」のまま溜まるだけで、消えない。
- ノートPCに Vault を clone する必要はない（GitHub の API で読み書きする）。

### Vault に保存されるもの（他の用途からも読める）

| 場所（Vault の中） | 内容 | 形式 |
|---|---|---|
| `Karte/カルテ/<顧客名>/<日付>_<顧客名>.md` | 完成したカルテ。NotebookLM に渡している形式のまま | Markdown（先頭に `---json` のデータ） |
| `Karte/訂正ログ/<年-月>/<時刻>_<ジョブID>_<n>.json` | AI の読み取りと、直した内容の差分（消さない） | JSON |
| `Karte/読み癖/読み癖.md` | 学習した「書かれ方 → 正しい表記」の対応表と読み癖の規則 | Markdown（読む用） |
| `Karte/読み癖/memory.json`、`履歴/v0001.json …` | 同じ内容の最新版と、版ごとの履歴 | JSON |

- 他の AI や自動処理は、`Karte/` を読むだけで、カルテと、トレーナーの書き癖を使えます。
- **写真と処理状態は main に入れない。** 写真は `karte-inbox` ブランチに置く。ジョブがなくなってしばらくたつと、ワーカーがこのブランチを履歴ごと作り直す。Vault の main には写真の履歴が残らない。
- Vault の GitHub Actions は `Context Engine/*.md` と `AI-Queue/*.md` にしか反応しないので、`Karte/` への書き込みで動き出したり、Gemini や Drive に顧客情報が流れたりはしない。
- 訂正ログは1フォルダ1000件までしか一覧できない制限に当たらないよう、月ごとのフォルダに分けている。
- 各PCの Vault は、Syncthing か `git pull` で受け取る。PC から push するときに、先にこの書き込みを pull する必要がある。

### 学習（訂正から読み癖を覚える）
1. 下書きを直して「カルテを生成する」で保存すると、「AI の読み取り」と「直した内容」の差分が `Karte/訂正ログ/` に残る。
2. ノートPCの ocr-worker が、手が空いたときに写真・読み取り・訂正を見比べて、読み癖を要約する。要約は「規則」と「対応表（例: ワイドSQ→ワイドスクワット）」の2つ。
3. 毎回の読み取りプロンプトに「過去の訂正から学んだこと（最優先で必ず守る）」として入る。まだ要約していない直近の訂正もそのまま入る。
4. アプリ側でも、対応表どおりに名前を直してから下書きにする。

**忘れない仕組み**
- 訂正ログは消さない。読み癖はいつでも作り直せる。
- 読み癖は上書きせず、版を重ねて追記する（`履歴/`）。
- 要約し直すときは、既存の規則を残すよう指示している。規則が3割以上減ったり対応表が消えたりする要約は採用しない。
- 学習した内容は「設定・データ移行」→「学習した読み癖」でも見られる。

**写真の扱い**
- 訂正が無かった写真は、取り込みが済んだらすぐ消える。
- 訂正があった写真は、学習が済んだら消える。
- 取り込んだまま14日間保存されなかった写真も消える。

### 書くときのルール
- **WU（ウォームアップ）**：「プリセット名＋差分」で書く。
  - アプリのプリセット名は、紙に書く名前と同じにしておく。
  - プリセット名を書かなかったページでは、その顧客が前回使ったプリセットを使う。
- **体調**：セッション詳細の「体調」ブロックに入る。
- **読めなかった箇所**：下書きの上に黄色で表示される。

## セットアップ

### 1. GitHub のトークンを作る（Vault 用）
1. GitHub → Settings → Developer settings → Personal access tokens → **Fine-grained tokens** → Generate new token。
2. Repository access は **Only select repositories** で `obsidian-vault` だけを選ぶ。
3. Permissions → Repository permissions → **Contents: Read and write**（他は不要）。
4. 有効期限を決めて作成し、値を控える（**秘密。このリポジトリには絶対に入れない**）。Vercel 用とノートPC用に、別々に作ってもよい。

### 2. Vercel
1. このリポジトリを Import する。Framework は Other、Build Command は空。
2. Environment Variables を設定する。

   | 変数 | 値 |
   |---|---|
   | `APP_TOKEN` | 自分で決めた長いランダム文字列（例: `openssl rand -hex 24`） |
   | `GITHUB_TOKEN` | 1 で作ったトークン |
   | `GITHUB_REPO` | （任意）既定は `accees7106-lab/obsidian-vault` |
   | `VAULT_BRANCH` | （任意）既定は `main` |
   | `INBOX_BRANCH` | （任意）写真置き場のブランチ名。既定は `karte-inbox`。Vault の main と同じにはできない |
   | `INBOX_REPO` | （任意）写真置き場を別のリポジトリにしたいときだけ。既定は Vault と同じ |
   | `ALLOWED_ORIGINS` | （任意）別ドメインのページから API を呼ぶ場合だけ。例: `https://accees7106-lab.github.io` |

3. Deploy したら、スマホで `https://<プロジェクト>.vercel.app/` を開く。
4. 「📷 手書きノートから読み込み」→「設定・データ移行」に `APP_TOKEN` と同じ値を入れて保存する。
5. 「⬆ これまでのカルテを Vault へ送る」で、端末にあるカルテを Vault に保存する（以降は保存のたびに自動で送られる）。

> **データの引っ越し：** localStorage は URL（ドメイン）ごとに別です。GitHub Pages 版で使っていたデータは、Vercel 版には自動では移りません。
> 1. 旧 URL で「⬇ 全データを書き出し」を押す。
> 2. 新 URL で「⬆ 読み込み」を押して、書き出したファイルを選ぶ。

### 3. ノートPC（Windows）

**セットアップ用のスクリプトで進める。** PowerShell を開き、`ocr-worker` フォルダで次を実行する（何度実行してもよい）。

```
powershell -ExecutionPolicy Bypass -File .\setup.ps1
```

1. Python 3.10 以上があるか確認する（無ければ winget で入れるか尋ねる）。
2. Claude Code が入っているか確認する（自動では入れない）。
3. `ANTHROPIC_API_KEY` があっても、ワーカーのプロセスでだけ外す（API 課金にならない）。
4. `.env` を作り、`GITHUB_TOKEN` を貼ってもらう（入力は画面に出ない。保存先はあなただけが読めるファイル）。
5. `python worker.py --check` で、トークン・Vault への接続・`claude -p` の呼び出し（サブスクのログイン）を確認する。問題があれば理由を表示して止まる。
6. `python worker.py --once` を1回動かす（すでに溜まっている写真があれば、このとき読み取る）。
7. ログオン時に自動で起動するタスク `KarteOcrWorker` を登録するか尋ねる。ログは `ocr-worker.log`。

- 前もって必要なのは、Claude Code にログインしておくこと（`claude` を起動して `/login`）と、GitHub のトークン（手順 1）だけ。
- **今使っているカルテの画面は変更しない。** このスクリプトは、この PC でワーカーを動かす準備だけを行う。
- 手で進める場合は、`.env.example` を `.env` にコピーして `GITHUB_TOKEN` を入れ、`python worker.py --check` → `python worker.py --once` の順に実行する。
- オプション：`-SkipSmokeTest`（手順 6 を省く）、`-SkipTask`（手順 7 を省く）。
- `.env` の値は、同じ名前の環境変数より優先される（他のツールが `GITHUB_TOKEN` を設定していても影響しない）。
- 環境変数に `ANTHROPIC_API_KEY` があると API 課金になってしまうため、手で `python worker.py` を実行するときは、先にその変数を外す（タスクとして動かすときは自動で外れる）。
- `CLAUDE_MODEL=opus` などでモデルを指定できる（空ならサブスクの既定）。
- ⚠ スクリプトは、PowerShell 7 での構文確認と、偽の GitHub・偽の `claude` を使った動作確認までしかしていない。**実際の Windows PowerShell 5.1 での実行は未確認。**

### 注意
- このリポジトリは公開（public）。Vault は非公開（private）。**顧客のデータは Vault 側にだけあり、このリポジトリには入らない。**
- `GITHUB_TOKEN` は Vault に書き込める。`APP_TOKEN` を知っている人は `Karte/` 配下にだけ書き込める（コードで他の場所には書けない）。漏れたら両方を作り直す。
- GitHub は内容を作る操作が短時間に続くと制限をかける。まとめて送るときは間隔をあけていて、制限に当たったら「少し待ってから」と表示し、未送信分は端末に残る。
- 写真は `karte-inbox` ブランチにあるので、Vault を clone した PC には、このブランチも取得される。ジョブが空になるたびに履歴ごと作り直すので小さいままだが、気になる場合は `INBOX_REPO` で別の非公開リポジトリに分ける。

## 開発

```
npm install
npm test            # API と ocr-worker の単体テスト
npm run test:e2e    # 画面の E2E（/api/jobs はモック。Chromium が必要）
```

- 並べ替え：各項目の左にある ⠿ をドラッグする。マウスとタッチの両方で動く（`vendor/Sortable.min.js`）。
