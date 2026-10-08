-- 手書きノート読み取りジョブ（karte-MD-NBLM）
-- 書き込みは Vercel Function / ノートPC の ocr-worker が service role key で行う。
-- RLS を有効にしてポリシーを作らない＝ anon / authenticated からは一切読めない。

create table if not exists public.karte_ocr_jobs (
  id            uuid primary key,
  created_at    timestamptz not null default now(),
  taken_at      timestamptz,
  filename      text,
  image_path    text not null,
  status        text not null default 'queued'
                check (status in ('queued', 'processing', 'done', 'error', 'imported', 'feedback')),
                -- imported: 下書きに取り込み済み／feedback: 訂正あり・学習待ち（写真を残す）
  context       jsonb not null default '{}'::jsonb,   -- 送信時点の顧客・プリセット・種目一覧
  result        jsonb,                                -- 読み取り結果 { karte: [...] }
  error         text,
  attempts      integer not null default 0,
  started_at    timestamptz,
  processed_at  timestamptz,
  imported_at   timestamptz
);

create index if not exists karte_ocr_jobs_status_created_idx
  on public.karte_ocr_jobs (status, created_at);

alter table public.karte_ocr_jobs enable row level security;

-- ─── 学習（訂正の記録と、そこから要約した読み癖）───
-- 訂正は消さずに残す（consolidated=true になるだけ）。読み癖はいつでも作り直せる。
create table if not exists public.karte_ocr_feedback (
  id            bigint generated always as identity primary key,
  created_at    timestamptz not null default now(),
  job_id        uuid not null,           -- ジョブ行は学習後に消えるので外部キーにしない
  draft_index   integer not null default 0,
  draft         jsonb not null,          -- AI が読み取った下書き（1人分）
  final         jsonb not null,          -- 人が直して保存した内容
  diffs         jsonb not null,          -- 差分の一覧
  consolidated  boolean not null default false,
  consolidated_at timestamptz,
  memory_version integer                 -- どの版の読み癖に取り込まれたか
);
create index if not exists karte_ocr_feedback_pending_idx
  on public.karte_ocr_feedback (consolidated, created_at);
alter table public.karte_ocr_feedback enable row level security;

-- 読み癖（版ごとに追記。上書きしないので過去の版に戻せる）
create table if not exists public.karte_ocr_memory (
  version        integer primary key,
  created_at     timestamptz not null default now(),
  rules          jsonb not null default '[]'::jsonb,   -- ["「デッドバク」と書いてあれば「デッドバグ」", ...]
  aliases        jsonb not null default '[]'::jsonb,   -- [{written, correct, kind}]
  feedback_count integer not null default 0             -- この版までに学習した訂正の累計
);
alter table public.karte_ocr_memory enable row level security;

-- 写真置き場（非公開）。訂正の無い写真は取り込み後すぐ、訂正のある写真は学習後に削除する。
insert into storage.buckets (id, name, public)
values ('karte-ocr', 'karte-ocr', false)
on conflict (id) do nothing;
