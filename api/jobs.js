// 手書きノート読み取りジョブの受付（Vercel Function）
//   POST   写真を受け取り Supabase Storage に保存し、ジョブ（status=queued）を作る
//   GET    未取り込みのジョブと読み取り結果の一覧
//   PATCH  ?id=  エラーになったジョブを再読み取りに戻す（body: {status:'queued'}）
//   DELETE ?id=  ジョブと写真を削除（結果を下書きに取り込んだ後に呼ばれる）
// 読み取り本体はノートPCの ocr-worker が行う（ここでは AI を呼ばない）
const crypto = require('crypto');

const TABLE = 'karte_ocr_jobs';
const BUCKET = 'karte-ocr';
// Vercel Function のリクエスト上限は 4.5MB（base64 で約1.33倍になる）
const MAX_IMAGE_BYTES = 3 * 1024 * 1024;
const LIST_COLUMNS = 'id,status,filename,taken_at,created_at,processed_at,result,error';

function env(name) {
  const v = process.env[name];
  if (!v) throw Object.assign(new Error(`サーバー設定 ${name} がありません`), { status: 500 });
  return v;
}

function authorized(req) {
  const expected = process.env.APP_TOKEN;
  if (!expected) return false;
  const got = String(req.headers.authorization || '').replace(/^Bearer\s+/i, '');
  const h = s => crypto.createHash('sha256').update(s).digest();
  return crypto.timingSafeEqual(h(got), h(expected));
}

function setCors(req, res) {
  const allowed = String(process.env.ALLOWED_ORIGINS || '').split(',').map(s => s.trim()).filter(Boolean);
  const origin = req.headers.origin;
  if (origin && allowed.includes(origin)) {
    res.setHeader('Access-Control-Allow-Origin', origin);
    res.setHeader('Vary', 'Origin');
    res.setHeader('Access-Control-Allow-Methods', 'GET,POST,PATCH,DELETE,OPTIONS');
    res.setHeader('Access-Control-Allow-Headers', 'Authorization,Content-Type');
  }
}

async function sb(path, { method = 'GET', headers = {}, body } = {}) {
  const key = env('SUPABASE_SERVICE_ROLE_KEY');
  const res = await fetch(env('SUPABASE_URL').replace(/\/+$/, '') + path, {
    method,
    headers: Object.assign({ apikey: key, Authorization: `Bearer ${key}` }, headers),
    body,
  });
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  return { ok: res.ok, status: res.status, data };
}

function fail(msg, status) { return Object.assign(new Error(msg), { status }); }

function parseImage(dataUrl) {
  const m = /^data:(image\/(?:jpeg|png|webp));base64,([A-Za-z0-9+/=]+)$/.exec(String(dataUrl || ''));
  if (!m) throw fail('画像の形式が不正です', 400);
  const buf = Buffer.from(m[2], 'base64');
  if (!buf.length) throw fail('画像が空です', 400);
  if (buf.length > MAX_IMAGE_BYTES) throw fail('画像が大きすぎます（3MBまで）', 413);
  return { type: m[1], ext: m[1] === 'image/png' ? 'png' : m[1] === 'image/webp' ? 'webp' : 'jpg', buf };
}

function validId(id) {
  if (!/^[0-9a-f-]{36}$/i.test(String(id || ''))) throw fail('id が不正です', 400);
  return id;
}

async function listJobs() {
  const r = await sb(`/rest/v1/${TABLE}?select=${LIST_COLUMNS}&order=created_at.asc&limit=200`);
  if (!r.ok) throw fail('ジョブ一覧の取得に失敗しました', 502);
  return { jobs: r.data };
}

async function createJob(body) {
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

async function requeueJob(id, body) {
  if (!body || body.status !== 'queued') throw fail('status は queued のみ指定できます', 400);
  const r = await sb(`/rest/v1/${TABLE}?id=eq.${id}&status=eq.error`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json', Prefer: 'return=representation' },
    body: JSON.stringify({ status: 'queued', error: null }),
  });
  if (!r.ok) throw fail('更新に失敗しました', 502);
  if (!Array.isArray(r.data) || !r.data.length) throw fail('エラー状態のジョブが見つかりません', 404);
  return { id, status: 'queued' };
}

async function deleteJob(id) {
  const r = await sb(`/rest/v1/${TABLE}?id=eq.${id}`, {
    method: 'DELETE', headers: { Prefer: 'return=representation' },
  });
  if (!r.ok) throw fail('削除に失敗しました', 502);
  for (const row of Array.isArray(r.data) ? r.data : []) {
    if (row.image_path) await sb(`/storage/v1/object/${BUCKET}/${row.image_path}`, { method: 'DELETE' });
  }
  return { id, deleted: Array.isArray(r.data) ? r.data.length : 0 };
}

async function handler(req, res) {
  setCors(req, res);
  if (req.method === 'OPTIONS') { res.statusCode = 204; return res.end(); }
  res.setHeader('Content-Type', 'application/json; charset=utf-8');
  res.setHeader('Cache-Control', 'no-store');
  try {
    if (!authorized(req)) throw fail('アクセストークンが違います', 401);
    const body = typeof req.body === 'string' ? JSON.parse(req.body || '{}') : (req.body || {});
    const id = req.query && req.query.id;
    let out;
    if (req.method === 'GET') out = await listJobs();
    else if (req.method === 'POST') out = await createJob(body);
    else if (req.method === 'PATCH') out = await requeueJob(validId(id), body);
    else if (req.method === 'DELETE') out = await deleteJob(validId(id));
    else throw fail('Method Not Allowed', 405);
    res.statusCode = 200;
    res.end(JSON.stringify(out));
  } catch (e) {
    res.statusCode = e.status || 500;
    res.end(JSON.stringify({ error: e.status ? e.message : 'サーバーエラー' }));
    if (!e.status) console.error(e);
  }
}

module.exports = handler;
