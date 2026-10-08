// api/ の共通処理（"_" 始まりなので Vercel のルートにはならない）
const crypto = require('crypto');

const BUCKET = 'karte-ocr';

function fail(msg, status) { return Object.assign(new Error(msg), { status }); }

function env(name) {
  const v = process.env[name];
  if (!v) throw fail(`サーバー設定 ${name} がありません`, 500);
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

function validId(id) {
  if (!/^[0-9a-f-]{36}$/i.test(String(id || ''))) throw fail('id が不正です', 400);
  return id;
}

// ジョブ行と写真をまとめて消す
async function deleteJobRow(id) {
  const r = await sb(`/rest/v1/karte_ocr_jobs?id=eq.${id}`, {
    method: 'DELETE', headers: { Prefer: 'return=representation' },
  });
  if (!r.ok) throw fail('削除に失敗しました', 502);
  const rows = Array.isArray(r.data) ? r.data : [];
  for (const row of rows) {
    if (row.image_path) await sb(`/storage/v1/object/${BUCKET}/${row.image_path}`, { method: 'DELETE' });
  }
  return rows.length;
}

// 認証・CORS・JSON 応答・エラー処理を共通化する
function route(methods) {
  return async function handler(req, res) {
    setCors(req, res);
    if (req.method === 'OPTIONS') { res.statusCode = 204; return res.end(); }
    res.setHeader('Content-Type', 'application/json; charset=utf-8');
    res.setHeader('Cache-Control', 'no-store');
    try {
      if (!authorized(req)) throw fail('アクセストークンが違います', 401);
      const fn = methods[req.method];
      if (!fn) throw fail('Method Not Allowed', 405);
      const body = typeof req.body === 'string' ? JSON.parse(req.body || '{}') : (req.body || {});
      const out = await fn({ query: req.query || {}, body });
      res.statusCode = 200;
      res.end(JSON.stringify(out));
    } catch (e) {
      res.statusCode = e.status || 500;
      res.end(JSON.stringify({ error: e.status ? e.message : 'サーバーエラー' }));
      if (!e.status) console.error(e);
    }
  };
}

module.exports = { BUCKET, fail, sb, validId, deleteJobRow, route };
