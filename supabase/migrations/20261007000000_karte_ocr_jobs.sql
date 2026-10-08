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
                check (status in ('queued', 'processing', 'done', 'error')),
  context       jsonb not null default '{}'::jsonb,   -- 送信時点の顧客・プリセット・種目一覧
  result        jsonb,                                -- 読み取り結果 { karte: [...] }
  error         text,
  attempts      integer not null default 0,
  started_at    timestamptz,
  processed_at  timestamptz
);

create index if not exists karte_ocr_jobs_status_created_idx
  on public.karte_ocr_jobs (status, created_at);

alter table public.karte_ocr_jobs enable row level security;

-- 写真置き場（非公開）。読み取りが終わった写真は ocr-worker が削除する。
insert into storage.buckets (id, name, public)
values ('karte-ocr', 'karte-ocr', false)
on conflict (id) do nothing;
