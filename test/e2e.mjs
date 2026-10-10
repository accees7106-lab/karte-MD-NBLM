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
const QUEUED = { id: 'aaaaaaaa-0000-0000-0000-000000000002', status: 'queued', filename: 'PXL_queued.jpg', created_at: new Date(Date.now() - 5 * 60000).toISOString(), attempts: 0 };
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
  const api = { posts: [], deletes: [], patches: [], feedback: [], records: [], recordsFail: false, jobs: [DONE, QUEUED, ERRJOB], gets: 0,
    worker: { state: 'idle', updated_at: new Date().toISOString(), interval_sec: 300, last_error: null, last_error_at: null } };
  await ctx.route('**/api/records**', async route => {
    const req = route.request();
    assert.equal(req.headers()['authorization'], 'Bearer t');
    if (api.recordsFail) return route.fulfill({ status: 503, json: { error: 'Vault への書き込みが競合しました' } });
    api.records.push(req.postDataJSON());
    return route.fulfill({ json: { path: 'Karte/カルテ/x/y.md' } });
  });
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
    if (m === 'GET') { api.gets++; return route.fulfill({ json: { jobs: api.jobs, worker: api.worker } }); }
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
    assert.match(await page.textContent('#workerLine'), /ノートPC：待機中/);
    assert.match(await page.textContent('#ocrJobs'), /読み取り待ち PXL_queued\.jpg\s*送信 5分前/);
  });

  await step('ノートPC の状態：学習中・直近のエラー・応答なし・読み取り中を表示し、自動で更新する', async () => {
    const line = () => page.textContent('#workerLine');
    api.worker = { state: 'learning', updated_at: new Date().toISOString(), interval_sec: 300,
      last_error: 'claude -p が 300 秒で終わらなかったため止めました', last_error_at: new Date(Date.now() - 120000).toISOString() };
    api.jobs = [QUEUED, ERRJOB, { id: 'aaaaaaaa-0000-0000-0000-000000000009', status: 'processing', filename: 'PXL_now.jpg',
      created_at: new Date().toISOString(), started_at: new Date(Date.now() - 20000).toISOString() }];
    const before = api.gets;
    await page.evaluate(() => { AUTO_SYNC_MS = 300; startAutoSync(); });   // 読み取り待ちがあるので自動で取りに行く
    await page.waitForFunction(() => document.getElementById('workerLine').textContent.includes('学習中'));
    assert.ok(api.gets > before);
    assert.match(await line(), /直近のエラー（2分前）：claude -p が 300 秒で終わらなかったため止めました/);
    assert.match(await page.textContent('#ocrJobs'), /読み取り中 PXL_now\.jpg\s*AI が読み取っています（開始から 2\d秒）/);
    api.worker = { state: 'idle', updated_at: new Date(Date.now() - 40 * 60000).toISOString(), interval_sec: 300 };
    await page.waitForFunction(() => document.getElementById('workerLine').textContent.includes('応答なし'));
    assert.match(await line(), /ノートPC：応答なし（最終確認 40分前）/);
    api.worker = null;
    await page.waitForFunction(() => document.getElementById('workerLine').textContent.includes('まだ状態の報告がありません'));
    api.worker = { state: 'idle', updated_at: new Date().toISOString(), interval_sec: 300 };
    api.jobs = [QUEUED, ERRJOB];
    await page.evaluate(() => { AUTO_SYNC_MS = 15000; startAutoSync(); syncJobs(false); });
    await page.waitForFunction(() => document.getElementById('workerLine').textContent.includes('待機中'));
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

  await step('保存したカルテを Vault へ送る（顧客名・日付・生成した Markdown そのまま）', async () => {
    await page.waitForFunction(() => JSON.parse(localStorage.getItem('training_karte_v2')).pendingRecords.length === 0);
    assert.equal(api.records.length, 1);
    const r = api.records[0];
    assert.deepEqual([r.id, r.client_name, r.date], ['20261006_玉谷', '玉谷', '2026-10-06']);
    assert.match(r.content, /^---json\n/);
    assert.match(r.content, /"RDL": "40kg 10reps 2sets \/ 47\.5kg 8reps 1sets"/);
    assert.equal(r.content, (await store(page)).records[0].content);
  });

  await step('保存手順の途中で通信できなくても、カルテは端末に残り、次に送り直す', async () => {
    api.recordsFail = true;
    await page.evaluate(() => { document.getElementById('clientName').value = '出口'; document.getElementById('sessionDate').value = '2026-10-07'; });
    await page.click('.btn-gen');
    await page.waitForFunction(() => document.getElementById('toast').textContent.length > 0);
    try {
      await page.waitForFunction(() => JSON.parse(localStorage.getItem('training_karte_v2')).pendingRecords.length === 1);
      assert.equal(api.records.length, 1);
      // 送信の失敗が画面に出るのは、通信が失敗して戻ってきてから
      await page.waitForFunction(() => document.getElementById('ocrStatus').textContent.includes('Vault未送信のカルテ 1件'));
    } finally {
      api.recordsFail = false;
    }
    await page.waitForFunction(() => !flushingRec);
    await page.evaluate(() => flushRecords());
    assert.equal(api.records.length, 2);
    assert.equal(api.records[1].id, '20261007_出口');
    assert.equal((await store(page)).pendingRecords.length, 0);
  });

  await step('「これまでのカルテを Vault へ送る」で端末のカルテを一括送信', async () => {
    api.records.length = 0;
    await page.click('details.cfg summary');
    await page.click('text=これまでのカルテを Vault へ送る');
    await page.waitForFunction(() => document.getElementById('toast').textContent.includes('2件を Vault に送りました'));
    assert.deepEqual(api.records.map(r => r.id).sort(), ['20261006_玉谷', '20261007_出口']);
    assert.match(await page.textContent('#vaultView'), /未送信のカルテはありません/);
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

  // ─── 黄色の枠：事例ごとの訂正 ───
  const inject = drafts => page.evaluate(ds => { store.drafts.push(...ds); save(); renderDrafts(); }, drafts);
  const openLast = () => page.click('#draftList .ocr-row:last-child button');
  const note = text => page.locator('#draftNotes li', { hasText: text });
  const feedbackSent = () => page.waitForFunction(() => JSON.parse(localStorage.getItem('training_karte_v2')).pendingFeedback.length === 0);

  await step('黄色の枠：顧客名を直すと、カルテに反映し、対応を覚える', async () => {
    await inject([{ id: 'job-c_0', jobId: 'cccccccc-0000-0000-0000-000000000001', date: '2026-10-09',
      data: { client_name: '玉屋', client_confidence: 'low', date: '2026-10-09', condition: null,
        warmup: { preset: 'WU-Z', changes: [{ op: 'replace', from: 'ローリング', to: 'ハーフローリング' }] },
        training: [{ name: 'ヒップスラスト', sets: [{ kg: 60, reps: 10, sets: 3 }], text: null }],
        notes: null, uncertain: ['ヒップスラストの重量が 60 か 80 か判別しにくい'] } }]);
    await openLast();
    const li = note('「玉屋」は未登録の顧客です');
    await li.locator('input').fill('玉谷');
    await li.locator('button').click();
    assert.equal(await page.inputValue('#clientName'), '玉谷');
    assert.match(await note('顧客名').textContent(), /次回から「玉屋」は「玉谷」と読みます/);
    assert.deepEqual((await store(page)).localAliases.map(a => [a.kind, a.written, a.correct]), [['client', '玉屋', '玉谷']]);
  });

  await step('黄色の枠：未登録の WU プリセットを選び直すと、WU を作り直して先頭に並べる', async () => {
    assert.deepEqual(await trainNames(page), ['ハーフローリング', 'ヒップスラスト']);
    const li = note('WUプリセット「WU-Z」が登録されていません');
    await li.locator('select').selectOption('WU-B');
    await li.locator('button').click();
    assert.deepEqual(await trainNames(page), ['チェストオープナー', 'ハーフローリング', 'ヒップスラスト']);
    assert.match(await note('WU-Z').textContent(), /WU を「WU-B」で作り直しました/);
    assert.ok((await store(page)).localAliases.find(a => a.kind === 'preset' && a.written === 'WU-Z' && a.correct === 'WU-B'));
  });

  await step('黄色の枠：新しい種目の読み違いを直す（空欄なら、そのまま登録）', async () => {
    const li = note('新しい種目');
    await li.locator('.fix').nth(0).locator('input').fill('ローリング');
    await li.locator('.fix').nth(0).locator('button').click();
    assert.deepEqual(await trainNames(page), ['チェストオープナー', 'ローリング', 'ヒップスラスト']);
    assert.equal(await page.locator('#trainRows .tb2').nth(1).locator('.nbadge').count(), 0); // 登録済みの種目なので NEW が外れる
    await li.locator('.fix').nth(0).locator('button').click();                                // ヒップスラスト：空欄＝このまま
    const t = await li.textContent();
    assert.match(t, /「ハーフローリング」→「ローリング」/);
    assert.match(t, /「ヒップスラスト」のまま/);
  });

  await step('黄色の枠：読めなかった箇所に文章で指摘を書ける／枠に無い読み間違いも指定できる', async () => {
    const li = note('判別しにくい');
    await li.locator('input').fill('80kg が正しい');
    await li.locator('button').click();
    assert.match(await note('判別しにくい').textContent(), /記録しました：80kg が正しい/);
    await page.click('#draftNotes details.manual summary');
    await page.selectOption('#mf_kind', 'exercise');
    await page.fill('#mf_written', 'ヒップスラスト');
    await page.fill('#mf_correct', 'ヒップリフト');
    await page.click('#draftNotes details.manual button');
    assert.ok((await trainNames(page)).includes('ヒップリフト'));
    assert.ok(await page.locator('#draftNotes details.manual').evaluate(e => e.open));   // 指定のあとも開いたまま
    assert.match(await page.textContent('#draftNotes details.manual'), /種目名：「ヒップスラスト」→「ヒップリフト」/);
  });

  await step('保存すると、指定した訂正（対応・指摘・種目名の変更）を学習用に送る', async () => {
    const n = api.feedback.length;
    await page.fill('#sessionBlocks textarea', '確認用');
    await page.click('.btn-gen');
    await page.waitForSelector('#resultArea:not(.ph)');
    await feedbackSent();
    const f = api.feedback[n];
    assert.equal(f.job_id, 'cccccccc-0000-0000-0000-000000000001');
    assert.equal(f.last, true);
    const has = (field, pred) => f.diffs.some(d => d.field === field && pred(d));
    assert.ok(has('alias', d => d.name === 'client' && d.ai === '玉屋' && d.final === '玉谷'));
    assert.ok(has('alias', d => d.name === 'preset' && d.ai === 'WU-Z' && d.final === 'WU-B'));
    assert.ok(has('alias', d => d.name === 'exercise' && d.ai === 'ハーフローリング' && d.final === 'ローリング'));
    assert.ok(has('alias', d => d.name === 'exercise' && d.ai === 'ヒップスラスト' && d.final === 'ヒップリフト' && d.manual));
    assert.ok(has('hint', d => /判別しにくい/.test(d.name) && d.final === '80kg が正しい'));
    assert.ok(has('train_renamed', d => d.ai === 'ヒップスラスト' && d.final === 'ヒップリフト'));
    assert.ok(has('client_name', d => d.ai === '玉屋' && d.final === '玉谷'));
    assert.ok(!f.diffs.some(d => d.field === 'train_order'));   // 名前を変えただけでは「順番の変更」にならない
    const md = (await store(page)).records[0].content;
    assert.match(md, /"ヒップリフト": "60kg 10reps 3sets"/);
  });

  await step('次の下書きでは、指定した対応が最初から効く（学習の結果を待たない）', async () => {
    await inject([{ id: 'job-d_0', jobId: 'dddddddd-0000-0000-0000-000000000001', date: '2026-10-10',
      data: { client_name: '玉屋', client_confidence: 'high', date: '2026-10-10', condition: '良好',
        warmup: { preset: 'WU-Z', changes: [] },
        training: [{ name: 'ヒップスラスト', sets: [{ kg: 80, reps: 8, sets: 3 }], text: null }],
        notes: null, uncertain: [] } }]);
    await openLast();
    assert.equal(await page.inputValue('#clientName'), '玉谷');
    assert.deepEqual(await trainNames(page), ['チェストオープナー', 'ローリング', 'ヒップリフト']);
    assert.match(await page.textContent('#draftBar'), /要確認の項目はありません/);
  });

  await step('設定欄で、この端末で指定した対応を確認・取り消せる', async () => {
    await page.evaluate(() => { document.querySelector('details.cfg').open = true; });
    const mv = page.locator('#memView');
    assert.match(await mv.textContent(), /この端末で指定した対応/);
    const before = (await store(page)).localAliases.length;
    await mv.locator('.rtag button').first().click();
    assert.equal((await store(page)).localAliases.length, before - 1);
  });

  await step('一覧の「削除」で、開かずに下書きを消せる（写真の最後の下書きなら片付いたことを送る）', async () => {
    const job = 'eeeeeeee-0000-0000-0000-000000000001';
    const mk = i => ({ id: `${job}_${i}`, jobId: job, date: '2026-10-11', data: { client_name: `削除テスト${i}`, warmup: { preset: null, changes: [] }, training: [], uncertain: [] } });
    await inject([mk(0), mk(1)]);
    const before = api.feedback.length;
    const current = await page.evaluate(() => curDraftId);
    const row = n => page.locator('#draftList .ocr-row', { hasText: n });
    await row('削除テスト0').locator('button', { hasText: '削除' }).click();
    assert.equal(await row('削除テスト0').count(), 0);
    assert.equal(await page.evaluate(() => curDraftId), current);           // 開いている下書きはそのまま
    await feedbackSent();
    assert.equal(api.feedback.length, before);                                // 同じ写真の下書きがまだ残る → 何も送らない
    await row('削除テスト1').locator('button', { hasText: '削除' }).click();
    await feedbackSent();
    assert.equal(api.feedback.length, before + 1);
    assert.deepEqual([api.feedback[before].job_id, api.feedback[before].last, api.feedback[before].diffs], [job, true, []]);
    assert.ok(!(await store(page)).drafts.some(d => d.jobId === job));
  });

  await step('トレーニングの詳細が空欄でも出力できる（種目名は残る）', async () => {
    await page.evaluate(() => {
      clearForm();
      document.getElementById('clientName').value = '空欄テスト';
      addTBlock('RDL', 'structured', false);          // セットの行はあるが値は空
      addTBlock('チェストオープナー', 'free', false);  // フリー入力も空
      addTBlock('ローリング', 'free', false);
      document.getElementById('v_' + TRL[2].rowId).value = '左右10回';
      addSBlock();
    });
    await page.fill('#sessionBlocks textarea', '確認用');
    await page.click('.btn-gen');
    await page.waitForSelector('#resultArea:not(.ph)');
    assert.equal(await page.isVisible('#errOverlay.open'), false);
    const md = (await store(page)).records[0].content;
    const tm = JSON.parse(md.split('---json\n')[1].split('\n---')[0]).training_menu;
    assert.deepEqual(tm, { RDL: '', チェストオープナー: '', ローリング: '左右10回' });
    const text = await page.textContent('#menuText');
    assert.match(text, /■ RDL\n■ チェストオープナー\n■ ローリング\n  左右10回/);
  });

  await step('セッション詳細が空欄でも出力できる（空のブロックは出さない）', async () => {
    await page.evaluate(() => {
      clearForm();
      document.getElementById('clientName').value = '空欄テスト2';
      document.getElementById('sessionDate').value = '2026-10-12';
      addTBlock('RDL', 'free', false);
      addSBlock(); addSBlock();
    });
    await page.fill('#sessionBlocks .sbl:nth-child(2) input', '肩周り');   // タイトルだけで中身が空
    await page.click('.btn-gen');
    await page.waitForSelector('#resultArea:not(.ph)');
    assert.equal(await page.isVisible('#errOverlay.open'), false);
    const rec = (await store(page)).records.find(r => r.id === '20261012_空欄テスト2');
    assert.ok(rec);
    assert.doesNotMatch(rec.content, /セッション詳細・所感/);
    assert.doesNotMatch(await page.textContent('#sessionText'), /セッション詳細・所感/);
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
