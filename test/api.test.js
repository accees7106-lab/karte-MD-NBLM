// api/jobs.js のテスト: node --test test/
const test = require('node:test');
const assert = require('node:assert');

process.env.APP_TOKEN = 'secret';
process.env.SUPABASE_URL = 'https://sb.example';
process.env.SUPABASE_SERVICE_ROLE_KEY = 'svc';
process.env.ALLOWED_ORIGINS = 'https://accees7106-lab.github.io';
const handler = require('../api/jobs.js');
const feedback = require('../api/feedback.js');

let calls = [];
let responder = () => ({ status: 200, body: [] });
global.fetch = async (url, opts) => {
  calls.push({ url, ...opts });
  const r = responder(url, opts);
  return { ok: r.status < 300, status: r.status, text: async () => (r.body == null ? '' : JSON.stringify(r.body)) };
};

function call({ method = 'GET', token = 'secret', query = {}, body, origin, fn = handler } = {}) {
  const headers = {};
  if (token) headers.authorization = 'Bearer ' + token;
  if (origin) headers.origin = origin;
  const res = {
    statusCode: 0, headers: {}, out: '',
    setHeader(k, v) { this.headers[k.toLowerCase()] = v; },
    end(s) { this.out = s || ''; },
  };
  return fn({ method, headers, query, body }, res).then(() => ({
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
  assert.match(calls[0].url, /\/rest\/v1\/karte_ocr_jobs\?select=.*&status=in\.\(queued,processing,done,error\)/);
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

test('PATCH imported は完了したジョブだけに付けられる', async () => {
  const id = '11111111-1111-1111-1111-111111111111';
  responder = () => ({ status: 200, body: [{ id }] });
  const r = await call({ method: 'PATCH', query: { id }, body: { status: 'imported' } });
  assert.equal(r.status, 200);
  assert.match(calls[0].url, /id=eq\.1111.*&status=eq\.done/);
  assert.equal(JSON.parse(calls[0].body).status, 'imported');
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

const JOB = '22222222-2222-2222-2222-222222222222';

test('feedback: 差分を記録し、最後の下書きで訂正があれば写真を残す（status=feedback）', async () => {
  responder = (url, o) => (o.method === 'GET' ? { status: 200, body: [{ id: 1 }] } : { status: 200, body: null });
  const r = await call({ fn: feedback, method: 'POST', body: { job_id: JOB, draft_index: 0, draft: { a: 1 }, final: { b: 2 }, diffs: [{ field: 'client_name', ai: '出ロ', final: '出口' }], last: true } });
  assert.equal(r.status, 200);
  assert.deepEqual(r.json, { recorded: 1, job: 'feedback' });
  const row = JSON.parse(calls[0].body);
  assert.equal(row.job_id, JOB);
  assert.equal(row.diffs[0].final, '出口');
  assert.match(calls[2].url, new RegExp(`karte_ocr_jobs\\?id=eq\\.${JOB}$`));
  assert.equal(JSON.parse(calls[2].body).status, 'feedback');
});

test('feedback: 訂正が1件も無い写真は最後の下書きで削除する', async () => {
  responder = (url, o) => (o.method === 'GET' ? { status: 200, body: [] }
    : url.includes('/rest/') ? { status: 200, body: [{ id: JOB, image_path: `${JOB}.jpg` }] } : { status: 200, body: {} });
  const r = await call({ fn: feedback, method: 'POST', body: { job_id: JOB, diffs: [], last: true } });
  assert.deepEqual(r.json, { recorded: 0, job: 'deleted' });
  assert.ok(calls.some(c => c.method === 'DELETE' && c.url.endsWith(`/storage/v1/object/karte-ocr/${JOB}.jpg`)));
});

test('feedback: last でなければ写真には触らない', async () => {
  const r = await call({ fn: feedback, method: 'POST', body: { job_id: JOB, diffs: [] } });
  assert.deepEqual(r.json, { recorded: 0, job: 'kept' });
  assert.equal(calls.length, 0);
});

test('feedback GET は最新版の読み癖と学習待ち件数を返す', async () => {
  responder = url => (url.includes('karte_ocr_memory') ? { status: 200, body: [{ version: 3, rules: ['r'], aliases: [] }] } : { status: 200, body: [{ id: 1 }, { id: 2 }] });
  const r = await call({ fn: feedback });
  assert.equal(r.json.memory.version, 3);
  assert.equal(r.json.pending, 2);
  assert.match(calls[0].url, /order=version\.desc&limit=1/);
  assert.equal((await call({ fn: feedback, token: 'x' })).status, 401);
});
