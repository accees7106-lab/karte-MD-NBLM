// api/jobs.js のテスト: node --test test/
const test = require('node:test');
const assert = require('node:assert');

process.env.APP_TOKEN = 'secret';
process.env.SUPABASE_URL = 'https://sb.example';
process.env.SUPABASE_SERVICE_ROLE_KEY = 'svc';
process.env.ALLOWED_ORIGINS = 'https://accees7106-lab.github.io';
const handler = require('../api/jobs.js');

let calls = [];
let responder = () => ({ status: 200, body: [] });
global.fetch = async (url, opts) => {
  calls.push({ url, ...opts });
  const r = responder(url, opts);
  return { ok: r.status < 300, status: r.status, text: async () => (r.body == null ? '' : JSON.stringify(r.body)) };
};

function call({ method = 'GET', token = 'secret', query = {}, body, origin } = {}) {
  const headers = {};
  if (token) headers.authorization = 'Bearer ' + token;
  if (origin) headers.origin = origin;
  const res = {
    statusCode: 0, headers: {}, out: '',
    setHeader(k, v) { this.headers[k.toLowerCase()] = v; },
    end(s) { this.out = s || ''; },
  };
  return handler({ method, headers, query, body }, res).then(() => ({
    status: res.statusCode, headers: res.headers, json: res.out ? JSON.parse(res.out) : null,
  }));
}

const JPEG = 'data:image/jpeg;base64,' + Buffer.from('fakejpeg').toString('base64');

test.beforeEach(() => { calls = []; responder = () => ({ status: 200, body: [] }); });

test('トークン無し・違うトークンは 401', async () => {
  assert.equal((await call({ token: null })).status, 401);
  assert.equal((await call({ token: 'nope' })).status, 401);
  assert.equal(calls.length, 0);
});

test('GET は一覧を返し、service key で Supabase を叩く', async () => {
  responder = () => ({ status: 200, body: [{ id: 'a', status: 'queued' }] });
  const r = await call();
  assert.equal(r.status, 200);
  assert.deepEqual(r.json.jobs, [{ id: 'a', status: 'queued' }]);
  assert.match(calls[0].url, /\/rest\/v1\/karte_ocr_jobs\?select=/);
  assert.equal(calls[0].headers.Authorization, 'Bearer svc');
});

test('POST は写真を保存してから queued の行を作る', async () => {
  const r = await call({ method: 'POST', body: { image: JPEG, filename: 'p1.jpg', taken_at: '2026-10-07T10:00:00Z', context: { clients: [] } } });
  assert.equal(r.status, 200);
  assert.equal(r.json.status, 'queued');
  assert.match(calls[0].url, new RegExp(`/storage/v1/object/karte-ocr/${r.json.id}\\.jpg$`));
  assert.equal(calls[0].headers['Content-Type'], 'image/jpeg');
  assert.equal(Buffer.from(calls[0].body).toString(), 'fakejpeg');
  const row = JSON.parse(calls[1].body);
  assert.equal(row.status, 'queued');
  assert.equal(row.image_path, `${r.json.id}.jpg`);
  assert.equal(row.filename, 'p1.jpg');
});

test('POST: 行の登録に失敗したら写真を消す', async () => {
  responder = (url, o) => (o.method === 'POST' && url.includes('/rest/') ? { status: 500, body: {} } : { status: 200, body: {} });
  const r = await call({ method: 'POST', body: { image: JPEG } });
  assert.equal(r.status, 502);
  assert.equal(calls[2].method, 'DELETE');
  assert.match(calls[2].url, /\/storage\/v1\/object\/karte-ocr\//);
});

test('POST: 画像でないものは 400', async () => {
  assert.equal((await call({ method: 'POST', body: { image: 'data:text/html;base64,PGI+' } })).status, 400);
  assert.equal((await call({ method: 'POST', body: {} })).status, 400);
});

test('PATCH はエラーのジョブだけを queued に戻す', async () => {
  const id = '11111111-1111-1111-1111-111111111111';
  responder = () => ({ status: 200, body: [{ id }] });
  const r = await call({ method: 'PATCH', query: { id }, body: { status: 'queued' } });
  assert.equal(r.status, 200);
  assert.match(calls[0].url, /id=eq\.1111.*&status=eq\.error/);
  responder = () => ({ status: 200, body: [] });
  assert.equal((await call({ method: 'PATCH', query: { id }, body: { status: 'queued' } })).status, 404);
  assert.equal((await call({ method: 'PATCH', query: { id }, body: { status: 'done' } })).status, 400);
});

test('DELETE は行と写真を消す。不正な id は 400', async () => {
  const id = '11111111-1111-1111-1111-111111111111';
  responder = (url, o) => (url.includes('/rest/') ? { status: 200, body: [{ id, image_path: `${id}.jpg` }] } : { status: 200, body: {} });
  const r = await call({ method: 'DELETE', query: { id } });
  assert.equal(r.json.deleted, 1);
  assert.match(calls[1].url, new RegExp(`/storage/v1/object/karte-ocr/${id}\\.jpg$`));
  assert.equal((await call({ method: 'DELETE', query: { id: '../x' } })).status, 400);
});

test('CORS は許可したオリジンにだけ返す', async () => {
  const ok = await call({ origin: 'https://accees7106-lab.github.io' });
  assert.equal(ok.headers['access-control-allow-origin'], 'https://accees7106-lab.github.io');
  const ng = await call({ origin: 'https://evil.example' });
  assert.equal(ng.headers['access-control-allow-origin'], undefined);
});
