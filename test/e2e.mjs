// カルテ画面の E2E（/api/jobs はモック）: npm i && npm run test:e2e
// Chromium は PLAYWRIGHT_BROWSERS_PATH、または CHROMIUM_PATH で指定
import { chromium } from 'playwright-core';
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';

const ROOT = path.join(path.dirname(fileURLToPath(import.meta.url)), '..');
const TYPES = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.json': 'application/json', '.png': 'image/png', '.svg': 'image/svg+xml' };
const server = http.createServer((req, res) => {
  let p = decodeURIComponent(new URL(req.url, 'http://x').pathname);
  if (p.endsWith('/')) p += 'index.html';
  const f = path.join(ROOT, p);
  if (!f.startsWith(ROOT) || !fs.existsSync(f)) { res.statusCode = 404; return res.end(); }
  res.setHeader('Content-Type', TYPES[path.extname(f)] || 'application/octet-stream');
  fs.createReadStream(f).pipe(res);
});
await new Promise(r => server.listen(0, '127.0.0.1', r));
const BASE = `http://localhost:${server.address().port}/`;

const SEED = {
  clients: { 玉谷: { sessions: 3, lastDate: '2026-10-01', wuPreset: 'WU-A' }, 出口: { sessions: 1, lastDate: '2026-10-01', wuPreset: 'WU-B' } },
  checkAssets: [],
  trainAssets: [{ name: 'チェストオープナー', type: 'free' }, { name: 'デッドバグ', type: 'free' }, { name: 'ローリング', type: 'free' }, { name: 'RDL', type: 'structured' }],
  trainPresets: [
    { name: 'WU-A', items: [{ name: 'チェストオープナー', type: 'free' }, { name: 'デッドバグ', type: 'free' }, { name: 'ローリング', type: 'free' }] },
    { name: 'WU-B', items: [{ name: 'チェストオープナー', type: 'free' }, { name: 'ローリング', type: 'free' }] },
  ],
  records: [], drafts: [], importedJobs: [],
};
const DONE = {
  id: 'aaaaaaaa-0000-0000-0000-000000000001', status: 'done', created_at: '2026-10-07T01:00:00Z', taken_at: '2026-10-06T10:00:00Z', processed_at: '2026-10-07T01:02:00Z',
  result: { karte: [
    { client_name: '玉谷', client_confidence: 'high', date: '2026-10-06', condition: '腰に張り。睡眠5h',
      warmup: { preset: 'WU-A', changes: [{ op: 'replace', from: 'デッドバグ', to: 'バードドッグ' }, { op: 'add', name: 'プランク', after: 'ローリング', value: '30秒' }] },
      training: [{ name: 'RDL', sets: [{ kg: 40, reps: 10, sets: 2 }, { kg: 45, reps: 8, sets: 1 }], text: null }, { name: 'ワイドSQ', sets: [], text: '自重15回' }],
      notes: null, uncertain: ['RDL 2行目の重量がにじんでいる'] },
    { client_name: '出口', client_confidence: 'low', date: null, condition: null,
      warmup: { preset: null, changes: [] }, training: [], notes: 'ストレッチ中心', uncertain: [] },
  ] },
};
const QUEUED = { id: 'aaaaaaaa-0000-0000-0000-000000000002', status: 'queued', created_at: '2026-10-07T01:00:00Z' };
const ERRJOB = { id: 'aaaaaaaa-0000-0000-0000-000000000003', status: 'error', filename: 'IMG_9.jpg', error: '読み取り結果の形式が不正です', created_at: '2026-10-07T01:00:00Z' };

const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});

async function setup(ctxOpts = {}) {
  const ctx = await browser.newContext(ctxOpts);
  await ctx.addInitScript(([seed]) => {
    if (!localStorage.getItem('training_karte_v2')) {
      localStorage.setItem('training_karte_v2', seed);
      localStorage.setItem('karte_ocr_cfg', JSON.stringify({ token: 't', api: '' }));
      localStorage.setItem('karte_last_export', new Date().toISOString());
    }
  }, [JSON.stringify(SEED)]);
  const api = { posts: [], deletes: [], patches: [], feedback: [], jobs: [DONE, QUEUED, ERRJOB] };
  await ctx.route('**/api/feedback**', async route => {
    const req = route.request();
    assert.equal(req.headers()['authorization'], 'Bearer t');
    if (req.method() === 'POST') { api.feedback.push(req.postDataJSON()); return route.fulfill({ json: { recorded: 1 } }); }
    return route.fulfill({ json: { pending: 1, memory: { version: 2, feedback_count: 5, rules: ['「口」を「ロ」と読まない'],
      aliases: [{ written: 'ワイドSQ', correct: 'ワイドスクワット', kind: 'exercise' }] } } });
  });
  await ctx.route('**/api/jobs**', async route => {
    const req = route.request();
    assert.equal(req.headers()['authorization'], 'Bearer t');
    const m = req.method();
    if (m === 'GET') return route.fulfill({ json: { jobs: api.jobs } });
    if (m === 'POST') { api.posts.push(req.postDataJSON()); return route.fulfill({ json: { id: 'x', status: 'queued' } }); }
    if (m === 'PATCH') { const id = new URL(req.url()).searchParams.get('id'); api.patches.push([id, req.postDataJSON().status]); if (req.postDataJSON().status === 'imported') api.jobs = api.jobs.filter(j => j.id !== id); return route.fulfill({ json: {} }); }
    if (m === 'DELETE') { const id = new URL(req.url()).searchParams.get('id'); api.deletes.push(id); api.jobs = api.jobs.filter(j => j.id !== id); return route.fulfill({ json: { deleted: 1 } }); }
    return route.fulfill({ json: {} });
  });
  const page = await ctx.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  page.on('dialog', d => d.accept());
  await page.goto(BASE);
  return { ctx, page, api, errors };
}
const trainNames = page => page.$$eval('#trainRows .tb2 .ktag', els => els.map(e => e.textContent));
const store = page => page.evaluate(() => JSON.parse(localStorage.getItem('training_karte_v2')));

let failures = 0;
async function step(name, fn) {
  try { await fn(); console.log('ok   ' + name); }
  catch (e) { failures++; console.log('FAIL ' + name + '\n     ' + e.message.split('\n').join('\n     ')); }
}

// ─── デスクトップ（マウス）───
{
  const { ctx, page, api, errors } = await setup();

  await step('起動時に結果を取り込み、完了ジョブに取り込み済みの印・状態と読み癖を表示', async () => {
    await page.waitForSelector('#draftList .ocr-row');
    assert.equal((await page.$$('#draftList .ocr-row')).length, 2);
    assert.deepEqual(api.patches, [[DONE.id, 'imported']]);
    assert.deepEqual(api.deletes, []);
    await page.waitForFunction(() => document.getElementById('memView').textContent.includes('v2'));
    const mv = await page.textContent('#memView');
    assert.match(mv, /訂正 5件から学習（学習待ちの訂正 1件）/);
    assert.match(mv, /ワイドSQ→ワイドスクワット/);
    const st = await page.textContent('#ocrStatus');
    assert.match(st, /読み取り待ち 1件/);
    assert.match(st, /エラー 1件/);
    assert.match(await page.textContent('#ocrJobs'), /IMG_9\.jpg/);
  });

  await step('写真3枚を縮小 JPEG で送信し、送信時点の一覧を添える', async () => {
    const png = fs.readFileSync(path.join(ROOT, 'icon-512.png'));
    await page.setInputFiles('#ocrFiles', [1, 2, 3].map(i => ({ name: `p${i}.png`, mimeType: 'image/png', buffer: png })));
    await page.click('#ocrUploadBtn');
    await page.waitForFunction(() => document.getElementById('toast').textContent.includes('3枚を送信'));
    assert.equal(api.posts.length, 3);
    assert.match(api.posts[0].image, /^data:image\/jpeg;base64,/);
    assert.deepEqual(api.posts[0].context.presets.find(p => p.name === 'WU-A').items, ['チェストオープナー', 'デッドバグ', 'ローリング']);
    assert.equal(api.posts[0].context.clients.find(c => c.name === '玉谷').wu_preset, 'WU-A');
  });

  await step('下書きを開くと WU プリセット＋差分が正しい順で展開される', async () => {
    await page.click('#draftList .ocr-row:nth-child(2) button');
    assert.deepEqual(await trainNames(page), ['チェストオープナー', 'バードドッグ', 'ローリング', 'プランク', 'RDL', 'ワイドスクワット']);
    assert.equal(await page.inputValue('#clientName'), '玉谷');
    assert.equal(await page.inputValue('#sessionDate'), '2026-10-06');
    assert.equal(await page.inputValue('#totalSessions'), '4');
    const vals = await page.$$eval('#trainRows .tb2 input', els => els.map(e => e.value));
    assert.deepEqual(vals, ['WU（WU-A）通り', 'WU（WU-A）通り', 'WU（WU-A）通り', '30秒', '40', '10', '2', '45', '8', '1', '自重15回']);
    assert.equal(await page.inputValue('#sessionBlocks .sbl input'), '体調');
    const bar = await page.textContent('#draftBar');
    assert.match(bar, /RDL 2行目の重量がにじんでいる/);
    assert.match(bar, /新しい種目（保存時に登録）：バードドッグ、プランク、ワイドスクワット/); // 学習した対応表で「ワイドSQ」を直している
  });

  await step('マウスのドラッグで RDL を先頭へ移動できる', async () => {
    await page.evaluate(() => document.querySelector('#trainRows .tb2').scrollIntoView({ block: 'start' }));
    const from = await page.locator('#trainRows .tb2').nth(4).locator('.dh').boundingBox();
    const to = await page.locator('#trainRows .tb2').nth(0).boundingBox();
    await page.mouse.move(from.x + from.width / 2, from.y + from.height / 2);
    await page.mouse.down();
    for (let i = 1; i <= 20; i++) await page.mouse.move(from.x + from.width / 2, from.y + (to.y + 5 - from.y) * i / 20);
    await page.mouse.up();
    await page.waitForTimeout(300);
    assert.equal((await trainNames(page))[0], 'RDL');
  });

  await step('生成すると並べ替えた順で保存され、下書きが1件減る・顧客情報は保持', async () => {
    const rdl = page.locator('#trainRows .tb2').nth(0);
    await rdl.locator('input').nth(3).fill('47.5'); // 2行目の重量を手で訂正
    await page.click('.btn-gen');
    await page.waitForSelector('#resultArea:not(.ph)');
    const s = await store(page);
    const md = s.records[0].content;
    const tm = JSON.parse(md.split('---json\n')[1].split('\n---')[0]).training_menu;
    assert.deepEqual(Object.keys(tm), ['RDL', 'チェストオープナー', 'バードドッグ', 'ローリング', 'プランク', 'ワイドスクワット']);
    assert.equal(tm.RDL, '40kg 10reps 2sets / 47.5kg 8reps 1sets');
    assert.match(md, /## 体調\n腰に張り。睡眠5h/);
    assert.equal(s.drafts.length, 1);
    assert.deepEqual(s.clients['玉谷'], { sessions: 4, lastDate: '2026-10-06', wuPreset: 'WU-A' });
    assert.ok(s.trainAssets.find(a => a.name === 'ワイドスクワット'));
  });

  await step('保存時に AI の読み取りとの差分を学習用に送る', async () => {
    await page.waitForFunction(() => JSON.parse(localStorage.getItem('training_karte_v2')).pendingFeedback.length === 0);
    assert.equal(api.feedback.length, 1);
    const f = api.feedback[0];
    assert.equal(f.job_id, DONE.id);
    assert.equal(f.draft_index, 0);
    assert.equal(f.last, false); // 同じ写真の下書きがまだ残っている
    assert.equal(f.draft.client_name, '玉谷');
    assert.deepEqual(f.diffs.find(d => d.field === 'train_value'), { field: 'train_value', name: 'RDL', ai: '40kg 10reps 2sets / 45kg 8reps 1sets', final: '40kg 10reps 2sets / 47.5kg 8reps 1sets' });
    const ord = f.diffs.find(d => d.field === 'train_order');
    assert.equal(ord.ai[0], 'チェストオープナー');
    assert.equal(ord.final[0], 'RDL');
    assert.equal(f.diffs.length, 2);
    assert.equal(f.final.training[0].name, 'RDL');
  });

  await step('プリセット名が無いページは顧客の「いつもの WU」を使う', async () => {
    await page.click('#draftList .ocr-row:nth-child(2) button');
    assert.equal(await page.inputValue('#clientName'), '出口');
    assert.deepEqual(await trainNames(page), ['チェストオープナー', 'ローリング']);
    assert.match(await page.textContent('#draftBar'), /いつもの「WU-B」を使いました/);
    assert.match(await page.textContent('#draftBar'), /顧客名の読み取りに自信がありません/);
  });

  await step('下書きを破棄すると、その写真が片付いたこと（last）だけを送る', async () => {
    await page.click('#draftBar button');
    await page.waitForFunction(() => JSON.parse(localStorage.getItem('training_karte_v2')).drafts.length === 0);
    await page.waitForFunction(() => JSON.parse(localStorage.getItem('training_karte_v2')).pendingFeedback.length === 0);
    assert.equal(api.feedback.length, 2);
    assert.deepEqual([api.feedback[1].job_id, api.feedback[1].last, api.feedback[1].diffs], [DONE.id, true, []]);
  });

  assert.deepEqual(errors, []);
  await ctx.close();
}

// ─── スマホ（タッチ）───
{
  const { ctx, page, errors } = await setup({ hasTouch: true, isMobile: true, viewport: { width: 390, height: 844 } });
  await step('タッチのドラッグで並べ替えできる', async () => {
    await page.waitForSelector('#draftList .ocr-row');
    await page.click('#draftList .ocr-row:nth-child(2) button');
    const cdp = await ctx.newCDPSession(page);
    await page.evaluate(() => document.querySelector('#trainRows .tb2').scrollIntoView({ block: 'start' }));
    const from = await page.locator('#trainRows .tb2').nth(4).locator('.dh').boundingBox();
    const to = await page.locator('#trainRows .tb2').nth(0).boundingBox();
    const x = from.x + from.width / 2, y0 = from.y + from.height / 2;
    await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x, y: y0 }] });
    for (let i = 1; i <= 20; i++) {
      await cdp.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ x, y: y0 + (to.y + 5 - y0) * i / 20 }] });
      await page.waitForTimeout(16);
    }
    await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
    await page.waitForTimeout(300);
    assert.equal((await trainNames(page))[0], 'RDL');
  });
  await step('スマホ幅で横スクロールが出ない', async () => {
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
  });
  assert.deepEqual(errors, []);
  await ctx.close();
}

await browser.close();
server.close();
if (failures) { console.log(`\n${failures} 件失敗`); process.exit(1); }
console.log('\nall passed');
