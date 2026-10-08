// api/ の共通処理（"_" 始まりなので Vercel のルートにはならない）
const crypto = require('crypto');

function fail(msg, status) { return Object.assign(new Error(msg), { status }); }

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

function validId(id) {
  if (!/^[0-9a-f-]{36}$/i.test(String(id || ''))) throw fail('id が不正です', 400);
  return String(id).toLowerCase();
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

module.exports = { fail, validId, route };
