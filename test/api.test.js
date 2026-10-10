// api/ のテスト: node --test test/api.test.js （偽 GitHub サーバーに対して実際に HTTP で読み書きする）
const test = require('node:test');
const assert = require('node:assert');
const { start } = require('./fake-github');

process.env.APP_TOKEN = 'secret';
process.env.GITHUB_TOKEN = 'tok';
process.env.GITHUB_REPO = 'me/vault';
process.env.ALLOWED_ORIGINS = 'https://accees7106-lab.github.io';
process.env.KARTE_RETRY_MS = '0';

const jobs = require('../api/jobs.js');
const feedback = require('../api/feedback.js');
const records = require('../api/records.js');

let fake;
const ID = '11111111-1111-1111-1111-111111111111';
const JPEG = 'data:image/jpeg;base64,' + Buffer.from('fakejpeg').toString('base64');

test.before(async () => { fake = await start(); });
test.after(() => fake.close());
test.beforeEach(() => {
  fake.state.repos = {}; fake.state.failPuts = 0; fake.state.calls = [];
  fake.state.seed('me/vault', 'main');
  process.env.GITHUB_API_URL = `http://127.0.0.1:${fake.port}`;
  delete process.env.INBOX_BRANCH; delete process.env.INBOX_REPO;
});

function call(fn, { method = 'GET', token = 'secret', query = {}, body, origin } = {}) {
  const headers = {};
  if (token) headers.authorization = 'Bearer ' + token;
  if (origin) headers.origin = origin;
  const res = { statusCode: 0, headers: {}, out: '', setHeader(k, v) { this.headers[k.toLowerCase()] = v; }, end(s) { this.out = s || ''; } };
  return fn({ method, headers, query, body }, res).then(() => ({ status: res.statusCode, headers: res.headers, json: res.out ? JSON.parse(res.out) : null }));
}
const tree = (branch = 'main') => fake.state.repos['me/vault'].branches[branch] ? [...fake.state.repos['me/vault'].branches[branch].files.keys()].sort() : null;
const file = (path, branch = 'main') => JSON.parse(fake.state.repos['me/vault'].branches[branch].files.get(path).buf.toString());
const seedJob = (over = {}) => {
  const b = fake.state.repos['me/vault'].branches['karte-inbox'] || (fake.state.repos['me/vault'].branches['karte-inbox'] = { files: new Map(), commits: [] });
  const job = Object.assign({ id: ID, status: 'done', image_path: `jobs/${ID}.jpg`, created_at: '2026-10-08T00:00:00Z', result: { karte: [] } }, over);
  b.files.set(`jobs/${job.id}.json`, { buf: Buffer.from(JSON.stringify(job)), sha: 'j' + Math.random() });
  b.files.set(`jobs/${job.id}.jpg`, { buf: Buffer.from('img'), sha: 'p' + Math.random() });
  return job;
};
const seedMain = (path, obj) => fake.state.repos['me/vault'].branches.main.files.set(path, { buf: Buffer.from(typeof obj === 'string' ? obj : JSON.stringify(obj)), sha: 'm' + Math.random() });

test('トークン無し・違うトークンは 401（GitHub には触らない）', async () => {
  assert.equal((await call(jobs, { token: null })).status, 401);
  assert.equal((await call(records, { method: 'POST', token: 'nope', body: {} })).status, 401);
  assert.equal(fake.state.calls.length, 0);
});

test('POST は inbox ブランチを作り、写真→ジョブの順に保存する。main には触らない', async () => {
  const r = await call(jobs, { method: 'POST', body: { image: JPEG, filename: 'p1.jpg', taken_at: '2026-10-07T10:00:00Z', context: { clients: [] } } });
  assert.equal(r.status, 200);
  assert.equal(r.json.status, 'queued');
  const id = r.json.id;
  assert.deepEqual(tree('karte-inbox'), ['README.md', `jobs/${id}.jpg`, `jobs/${id}.json`]);
  assert.equal(fake.state.repos['me/vault'].branches['karte-inbox'].commits[0].parents.length, 0); // 履歴の無い（orphan）ブランチ
  assert.equal(fake.state.repos['me/vault'].branches['karte-inbox'].files.get(`jobs/${id}.jpg`).buf.toString(), 'fakejpeg');
  const job = file(`jobs/${id}.json`, 'karte-inbox');
  assert.equal(job.status, 'queued');
  assert.equal(job.filename, 'p1.jpg');
  assert.equal(job.image_path, `jobs/${id}.jpg`);
  assert.deepEqual(tree('main'), []);
});

test('POST: 画像でないもの・大きすぎるものは 400/413', async () => {
  assert.equal((await call(jobs, { method: 'POST', body: { image: 'data:text/html;base64,PGI+' } })).status, 400);
  assert.equal((await call(jobs, { method: 'POST', body: {} })).status, 400);
  const big = 'data:image/jpeg;base64,' + Buffer.alloc(3 * 1024 * 1024 + 1).toString('base64');
  assert.equal((await call(jobs, { method: 'POST', body: { image: big } })).status, 413);
});

test('同時書き込みの競合（409）は自動でやり直す', async () => {
  await fake.state.seed('me/vault');
  fake.state.failPuts = 2;
  const r = await call(jobs, { method: 'POST', body: { image: JPEG } });
  assert.equal(r.status, 200);
  assert.equal(tree('karte-inbox').filter(p => p.startsWith('jobs/')).length, 2);
});

test('GET は画面に出す状態のジョブだけを返す（context は返さない・取り込み済みは出さない）', async () => {
  seedJob({ id: ID, status: 'done', context: { secret: 1 } });
  seedJob({ id: '22222222-2222-2222-2222-222222222222', status: 'imported', created_at: '2026-10-08T01:00:00Z' });
  seedJob({ id: '33333333-3333-3333-3333-333333333333', status: 'queued', created_at: '2026-10-08T02:00:00Z' });
  const r = await call(jobs);
  assert.deepEqual(r.json.jobs.map(j => j.status), ['done', 'queued']);
  assert.equal(r.json.jobs[0].context, undefined);
  assert.deepEqual(r.json.jobs[0].result, { karte: [] });
});

test('GET: inbox ブランチがまだ無ければ空', async () => {
  assert.deepEqual((await call(jobs)).json, { jobs: [], worker: null });
});

test('GET はノートPC の状態（worker/heartbeat.json）も返す', async () => {
  seedJob({ status: 'queued' });
  fake.state.repos['me/vault'].branches['karte-inbox'].files.set('worker/heartbeat.json',
    { buf: Buffer.from(JSON.stringify({ state: 'idle', updated_at: '2026-10-10T05:00:00Z' })), sha: 'h1' });
  const r = await call(jobs);
  assert.deepEqual(r.json.worker, { state: 'idle', updated_at: '2026-10-10T05:00:00Z' });
  assert.equal(r.json.jobs.length, 1);   // 状態報告はジョブに混ざらない
});

test('PATCH imported は完了したジョブだけ。queued はエラーだけ', async () => {
  seedJob({ status: 'done' });
  assert.equal((await call(jobs, { method: 'PATCH', query: { id: ID }, body: { status: 'imported' } })).status, 200);
  assert.equal(file(`jobs/${ID}.json`, 'karte-inbox').status, 'imported');
  assert.ok(file(`jobs/${ID}.json`, 'karte-inbox').imported_at);
  assert.equal((await call(jobs, { method: 'PATCH', query: { id: ID }, body: { status: 'imported' } })).status, 404); // もう done ではない
  seedJob({ status: 'error', error: 'x' });
  assert.equal((await call(jobs, { method: 'PATCH', query: { id: ID }, body: { status: 'queued' } })).status, 200);
  assert.deepEqual([file(`jobs/${ID}.json`, 'karte-inbox').status, file(`jobs/${ID}.json`, 'karte-inbox').error], ['queued', null]);
  assert.equal((await call(jobs, { method: 'PATCH', query: { id: ID }, body: { status: 'done' } })).status, 400);
});

test('DELETE はジョブと写真を消す。不正な id は 400', async () => {
  seedJob();
  const r = await call(jobs, { method: 'DELETE', query: { id: ID } });
  assert.equal(r.json.deleted, 1);
  assert.deepEqual(tree('karte-inbox'), []);
  assert.equal((await call(jobs, { method: 'DELETE', query: { id: '../x' } })).status, 400);
});

test('CORS は許可したオリジンにだけ返す', async () => {
  assert.equal((await call(jobs, { origin: 'https://accees7106-lab.github.io' })).headers['access-control-allow-origin'], 'https://accees7106-lab.github.io');
  assert.equal((await call(jobs, { origin: 'https://evil.example' })).headers['access-control-allow-origin'], undefined);
});

test('INBOX_BRANCH を Vault と同じにすると動かさない（作り直しで Vault を消さないため）', async () => {
  process.env.INBOX_BRANCH = 'main';
  assert.equal((await call(jobs, { method: 'POST', body: { image: JPEG } })).status, 500);
});

// ─── 訂正ログ ───
test('feedback: 訂正を Vault に記録し、学習待ちがあれば写真を残す（status=feedback）', async () => {
  seedJob({ status: 'imported' });
  const r = await call(feedback, { method: 'POST', body: { job_id: ID, draft_index: 1, draft: { a: 1 }, final: { b: 2 }, diffs: [{ field: 'client_name', ai: '出ロ', final: '出口' }], last: true } });
  assert.deepEqual(r.json, { recorded: 1, job: 'feedback' });
  const logs = tree('main').filter(p => p.startsWith('Karte/訂正ログ/'));
  assert.equal(logs.length, 1);
  assert.match(logs[0], new RegExp(`^Karte/訂正ログ/\\d{4}-\\d{2}/\\d{8}T\\d{6}Z_${ID}_1\\.json$`));
  assert.equal(file(logs[0]).diffs[0].final, '出口');
  assert.equal(file(`jobs/${ID}.json`, 'karte-inbox').status, 'feedback');
  assert.ok(tree('karte-inbox').includes(`jobs/${ID}.jpg`));
});

test('feedback: 訂正が無い（または学習済みの）写真は最後の下書きで削除する', async () => {
  seedJob({ status: 'imported' });
  assert.deepEqual((await call(feedback, { method: 'POST', body: { job_id: ID, diffs: [], last: true } })).json, { recorded: 0, job: 'deleted' });
  assert.deepEqual(tree('karte-inbox'), []);
  // 訂正はあるが、読み癖がもう取り込み済み
  seedJob({ status: 'imported' });
  seedMain(`Karte/訂正ログ/2026-10/20261008T000000Z_${ID}_0.json`, {});
  seedMain('Karte/読み癖/memory.json', { version: 1, last_feedback: `20261008T000000Z_${ID}_0.json` });
  assert.equal((await call(feedback, { method: 'POST', body: { job_id: ID, diffs: [], last: true } })).json.job, 'deleted');
});

test('feedback: last でなければ写真には触らない', async () => {
  seedJob({ status: 'imported' });
  assert.deepEqual((await call(feedback, { method: 'POST', body: { job_id: ID, diffs: [] } })).json, { recorded: 0, job: 'kept' });
  assert.ok(tree('karte-inbox').includes(`jobs/${ID}.json`));
});

test('feedback GET は最新の読み癖と学習待ち件数を返す', async () => {
  assert.deepEqual((await call(feedback)).json, { memory: null, pending: 0 });
  seedMain('Karte/読み癖/memory.json', { version: 3, rules: ['r'], aliases: [], last_feedback: '20261008T000100Z_x_0.json' });
  seedMain('Karte/訂正ログ/2026-10/20261008T000000Z_x_0.json', {});
  seedMain('Karte/訂正ログ/2026-10/20261008T000200Z_x_0.json', {});
  seedMain('Karte/訂正ログ/2026-10/20261008T000300Z_x_0.json', {});
  const r = await call(feedback);
  assert.equal(r.json.memory.version, 3);
  assert.equal(r.json.pending, 2);
  assert.equal((await call(feedback, { token: 'x' })).status, 401);
});

// ─── カルテ ───
test('records: カルテを Vault の Karte/カルテ/<顧客>/ に保存し、同じ id は上書きする', async () => {
  const body = { id: '20261006_玉谷', client_name: '玉谷', date: '2026-10-06', content: '---json\n{"a":1}\n---\n' };
  const r = await call(records, { method: 'POST', body });
  assert.equal(r.json.path, 'Karte/カルテ/玉谷/20261006_玉谷.md');
  assert.equal(fake.state.repos['me/vault'].branches.main.files.get(r.json.path).buf.toString(), body.content);
  await call(records, { method: 'POST', body: { ...body, content: body.content + '追記\n' } });
  assert.match(fake.state.repos['me/vault'].branches.main.files.get(r.json.path).buf.toString(), /追記/);
  assert.equal(tree('main').length, 1);
});

test('records: 顧客名・id にパス区切りや .. が入ってもフォルダの外に出ない', async () => {
  const r = await call(records, { method: 'POST', body: { id: '../../Context Engine/MyContext', client_name: '../../Context Engine', date: '2026-10-06', content: 'x' } });
  assert.equal(r.status, 200);
  assert.ok(r.json.path.startsWith('Karte/カルテ/'));
  assert.ok(!r.json.path.slice('Karte/カルテ/'.length).includes('..'));
  assert.equal(r.json.path.split('/').length, 4);
});

test('feedback: 取り込み済みの月のフォルダは見に行かない（1フォルダ1000件の制限対策）', async () => {
  seedMain('Karte/訂正ログ/2026-08/20260801T000000Z_x_0.json', {});
  seedMain('Karte/訂正ログ/2026-09/20260901T000000Z_x_0.json', {});
  seedMain('Karte/訂正ログ/2026-10/20261001T000000Z_x_0.json', {});
  seedMain('Karte/読み癖/memory.json', { version: 1, last_feedback: '20260901T000000Z_x_0.json' });
  fake.state.calls = [];
  assert.equal((await call(feedback)).json.pending, 1);
  assert.ok(!fake.state.calls.some(c => c.includes('2026-08')));
  assert.ok(fake.state.calls.some(c => c.includes('2026-10')));
});

test('GitHub の書き込み制限（403 rate limit）は 429 で返す', async () => {
  const realFetch = global.fetch;
  global.fetch = async (url, o) => (o && o.method === 'PUT'
    ? { ok: false, status: 403, text: async () => JSON.stringify({ message: 'You have exceeded a secondary rate limit.' }) }
    : realFetch(url, o));
  try {
    const r = await call(records, { method: 'POST', body: { id: 'a', client_name: 'x', date: '2026-10-06', content: 'c' } });
    assert.equal(r.status, 429);
    assert.match(r.json.error, /書き込み制限/);
  } finally { global.fetch = realFetch; }
});

test('records: 不正な入力は 400', async () => {
  const ok = { id: 'a', client_name: 'x', date: '2026-10-06', content: 'c' };
  for (const bad of [{ id: '' }, { client_name: '' }, { date: '10/6' }, { content: ' ' }]) {
    assert.equal((await call(records, { method: 'POST', body: { ...ok, ...bad } })).status, 400);
  }
});
