// 手書きノート読み取りジョブの受付（Vercel Function）
//   POST   写真を受け取り Supabase Storage に保存し、ジョブ（status=queued）を作る
//   GET    画面に出すジョブ（読み取り待ち・読み取り中・完了・エラー）と読み取り結果
//   PATCH  ?id=  {status:'queued'} エラーを再読み取りに戻す／{status:'imported'} 下書きに取り込んだ印
//   DELETE ?id=  ジョブと写真を削除
// 読み取り本体はノートPCの ocr-worker が行う（ここでは AI を呼ばない）。
// 取り込み後の写真は、訂正の学習（api/feedback.js → ocr-worker）が済むまで残す。
const crypto = require('crypto');
const { BUCKET, fail, sb, validId, deleteJobRow, route } = require('./_lib');

const TABLE = 'karte_ocr_jobs';
// Vercel Function のリクエスト上限は 4.5MB（base64 で約1.33倍になる）
const MAX_IMAGE_BYTES = 3 * 1024 * 1024;
const LIST_COLUMNS = 'id,status,filename,taken_at,created_at,processed_at,result,error';
const VISIBLE = 'queued,processing,done,error';

function parseImage(dataUrl) {
  const m = /^data:(image\/(?:jpeg|png|webp));base64,([A-Za-z0-9+/=]+)$/.exec(String(dataUrl || ''));
  if (!m) throw fail('画像の形式が不正です', 400);
  const buf = Buffer.from(m[2], 'base64');
  if (!buf.length) throw fail('画像が空です', 400);
  if (buf.length > MAX_IMAGE_BYTES) throw fail('画像が大きすぎます（3MBまで）', 413);
  return { type: m[1], ext: m[1] === 'image/png' ? 'png' : m[1] === 'image/webp' ? 'webp' : 'jpg', buf };
}

async function listJobs() {
  const r = await sb(`/rest/v1/${TABLE}?select=${LIST_COLUMNS}&status=in.(${VISIBLE})&order=created_at.asc&limit=200`);
  if (!r.ok) throw fail('ジョブ一覧の取得に失敗しました', 502);
  return { jobs: r.data };
}

async function createJob({ body }) {
  const img = parseImage(body.image);
  const id = crypto.randomUUID();
  const imagePath = `${id}.${img.ext}`;
  const up = await sb(`/storage/v1/object/${BUCKET}/${imagePath}`, {
    method: 'POST', headers: { 'Content-Type': img.type, 'x-upsert': 'false' }, body: img.buf,
  });
  if (!up.ok) throw fail('写真の保存に失敗しました', 502);
  const row = {
    id,
    image_path: imagePath,
    status: 'queued',
    filename: String(body.filename || '').slice(0, 200) || null,
    taken_at: isNaN(Date.parse(body.taken_at)) ? null : new Date(body.taken_at).toISOString(),
    context: body.context && typeof body.context === 'object' ? body.context : {},
  };
  const ins = await sb(`/rest/v1/${TABLE}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Prefer: 'return=minimal' },
    body: JSON.stringify(row),
  });
  if (!ins.ok) {
    await sb(`/storage/v1/object/${BUCKET}/${imagePath}`, { method: 'DELETE' });
    throw fail('ジョブの登録に失敗しました', 502);
  }
  return { id, status: 'queued' };
}

// 遷移できるのは error→queued（再読み取り）と done→imported（取り込み済み）だけ
const TRANSITIONS = { queued: 'error', imported: 'done' };

async function updateJob({ query, body }) {
  const id = validId(query.id);
  const from = TRANSITIONS[body && body.status];
  if (!from) throw fail('status は queued か imported のみ指定できます', 400);
  const fields = body.status === 'queued' ? { status: 'queued', error: null } : { status: 'imported', imported_at: new Date().toISOString() };
  const r = await sb(`/rest/v1/${TABLE}?id=eq.${id}&status=eq.${from}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json', Prefer: 'return=representation' },
    body: JSON.stringify(fields),
  });
  if (!r.ok) throw fail('更新に失敗しました', 502);
  if (!Array.isArray(r.data) || !r.data.length) throw fail('対象のジョブが見つかりません', 404);
  return { id, status: body.status };
}

async function deleteJob({ query }) {
  const id = validId(query.id);
  return { id, deleted: await deleteJobRow(id) };
}

module.exports = route({ GET: listJobs, POST: createJob, PATCH: updateJob, DELETE: deleteJob });
