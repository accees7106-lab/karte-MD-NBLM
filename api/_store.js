// Vault に置くものの形と置き場所（ocr-worker/worker.py と同じ取り決め。変えるときは両方）
//
//   inbox ブランチ（使い捨て）   jobs/<id>.json        ジョブの状態・読み取り結果
//                               jobs/<id>.jpg|png|webp 写真
//   main ブランチ（残るもの）    Karte/カルテ/<顧客>/<日付>_<顧客>.md   完成したカルテ（NotebookLM 用の形式のまま）
//                               Karte/訂正ログ/<年-月>/<時刻>_<jobId>_<n>.json  AI の読み取りと訂正の差分（消さない。
//                                                  Contents API は1フォルダ1000件までしか一覧できないので月ごとに分ける）
//                               Karte/読み癖/memory.json 他              学習した規則・対応表（版ごとに追記）
const crypto = require('crypto');
const gh = require('./_github');
const { fail, validId } = require('./_lib');

const JOBS_DIR = 'jobs';
const DIR_RECORDS = 'Karte/カルテ';
const DIR_FEEDBACK = 'Karte/訂正ログ';
const MEMORY_FILE = 'Karte/読み癖/memory.json';
const MAX_IMAGE_BYTES = 3 * 1024 * 1024;   // Vercel Function のリクエスト上限は 4.5MB（base64 で約1.33倍）
const MAX_RECORD_BYTES = 200 * 1024;
const MAX_DIFFS = 200;
const VISIBLE = ['queued', 'processing', 'done', 'error'];
const PUBLIC_FIELDS = ['id', 'status', 'filename', 'taken_at', 'created_at', 'started_at', 'processed_at', 'attempts', 'result', 'error'];

const HEARTBEAT_PATH = 'worker/heartbeat.json';   // ノートPC の ocr-worker が書く「今の状態」
const jobPath = id => `${JOBS_DIR}/${id}.json`;
const now = () => new Date().toISOString();

// ファイル名に使えない文字を潰す（パス区切りや .. でフォルダの外に出られないように）
function safeName(s, fallback = '名前不明') {
  const t = String(s == null ? '' : s).replace(/[\\/:*?"<>|\u0000-\u001f]/g, '_').replace(/\.{2,}/g, '_').replace(/^[.\s]+|[.\s]+$/g, '').slice(0, 80);
  return t || fallback;
}

async function mapLimit(items, limit, fn) {
  const out = new Array(items.length);
  let i = 0;
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, async () => {
    while (i < items.length) { const k = i++; out[k] = await fn(items[k]); }
  }));
  return out;
}

async function readJob(id) {
  const { inbox } = gh.conf();
  const f = await gh.getFile(inbox, jobPath(id));
  return f && f.buf ? JSON.parse(f.buf.toString('utf8')) : null;
}

// ─── ジョブ ───
async function listJobs() {
  const { inbox } = gh.conf();
  const names = (await gh.listDir(inbox, JOBS_DIR)).filter(e => e.type === 'file' && e.name.endsWith('.json'));
  const jobs = await mapLimit(names, 6, async e => {
    const f = await gh.getFile(inbox, e.path);
    return f && f.buf ? JSON.parse(f.buf.toString('utf8')) : null;
  });
  return jobs.filter(j => j && VISIBLE.includes(j.status)).sort((a, b) => String(a.created_at).localeCompare(String(b.created_at)))
    .map(j => Object.fromEntries(PUBLIC_FIELDS.map(k => [k, j[k] === undefined ? null : j[k]])));
}

// ノートPC の状態（無ければ null）。画面に「待機中・学習中・応答なし」を出すため
async function workerStatus() {
  const { inbox } = gh.conf();
  const f = await gh.getFile(inbox, HEARTBEAT_PATH);
  if (!f || !f.buf) return null;
  try { return JSON.parse(f.buf.toString('utf8')); } catch { return null; }
}

function parseImage(dataUrl) {
  const m = /^data:(image\/(?:jpeg|png|webp));base64,([A-Za-z0-9+/=]+)$/.exec(String(dataUrl || ''));
  if (!m) throw fail('画像の形式が不正です', 400);
  const buf = Buffer.from(m[2], 'base64');
  if (!buf.length) throw fail('画像が空です', 400);
  if (buf.length > MAX_IMAGE_BYTES) throw fail('画像が大きすぎます（3MBまで）', 413);
  return { ext: m[1] === 'image/png' ? 'png' : m[1] === 'image/webp' ? 'webp' : 'jpg', buf };
}

async function createJob(body) {
  const img = parseImage(body.image);
  const { inbox } = gh.conf();
  await gh.ensureInbox();
  const id = crypto.randomUUID();
  const imagePath = `${JOBS_DIR}/${id}.${img.ext}`;
  await gh.createFile(inbox, imagePath, img.buf, `karte-inbox: photo ${id}`);
  const job = {
    id, status: 'queued', image_path: imagePath, created_at: now(),
    filename: String(body.filename || '').slice(0, 200) || null,
    taken_at: isNaN(Date.parse(body.taken_at)) ? null : new Date(body.taken_at).toISOString(),
    context: body.context && typeof body.context === 'object' ? body.context : {},
    result: null, error: null, attempts: 0, started_at: null, processed_at: null, imported_at: null,
  };
  try {
    // ジョブの JSON を書いて初めてワーカーから見える（写真だけが先に置かれても読み取りは始まらない）
    await gh.createFile(inbox, jobPath(id), Buffer.from(JSON.stringify(job, null, 2) + '\n'), `karte-inbox: job ${id}`);
  } catch (e) {
    await gh.deleteFile(inbox, imagePath, `karte-inbox: cleanup ${id}`).catch(() => {});
    throw e;
  }
  return { id, status: 'queued' };
}

// 遷移できるのは error→queued（再読み取り）と done→imported（取り込み済み）だけ
const TRANSITIONS = { queued: 'error', imported: 'done' };

async function updateJob(id, to) {
  const from = TRANSITIONS[to];
  if (!from) throw fail('status は queued か imported のみ指定できます', 400);
  const { inbox } = gh.conf();
  const next = await gh.updateJson(inbox, jobPath(id), job => {
    if (job.status !== from) return null;
    return to === 'queued'
      ? Object.assign(job, { status: 'queued', error: null })
      : Object.assign(job, { status: 'imported', imported_at: now() });
  }, `karte-inbox: ${id} → ${to}`);
  if (!next) throw fail('対象のジョブが見つかりません', 404);
  return { id, status: to };
}

async function removeJob(id) {
  const { inbox } = gh.conf();
  const job = await readJob(id);
  if (job && job.image_path) await gh.deleteFile(inbox, job.image_path, `karte-inbox: remove photo ${id}`);
  const deleted = await gh.deleteFile(inbox, jobPath(id), `karte-inbox: remove job ${id}`);
  return deleted ? 1 : 0;
}

// ─── 訂正ログと読み癖 ───
async function readMemory() {
  const { vault } = gh.conf();
  const f = await gh.getFile(vault, MEMORY_FILE);
  return f && f.buf ? JSON.parse(f.buf.toString('utf8')) : null;
}

// 訂正ログのファイル名は 20261008T001122Z_<jobId>_<n>.json。置き場所は名前の年月から決まる
const feedbackDir = name => `${DIR_FEEDBACK}/${name.slice(0, 4)}-${name.slice(4, 6)}`;

// upto（読み癖に取り込み済みの最後のファイル名）より後の訂正ログの名前。取り込み済みの月は見に行かない
async function feedbackNames(upto = '') {
  const { vault } = gh.conf();
  const from = upto ? `${upto.slice(0, 4)}-${upto.slice(4, 6)}` : '';
  const months = (await gh.listDir(vault, DIR_FEEDBACK)).filter(e => e.type === 'dir' && e.name >= from).map(e => e.name).sort();
  const lists = await mapLimit(months, 4, m => gh.listDir(vault, `${DIR_FEEDBACK}/${m}`));
  return lists.flat().filter(e => e.type === 'file' && e.name.endsWith('.json') && e.name > upto).map(e => e.name).sort();
}

async function addFeedback(body) {
  const jobId = validId(body.job_id);
  const { vault } = gh.conf();
  const diffs = Array.isArray(body.diffs) ? body.diffs.slice(0, MAX_DIFFS) : [];
  if (diffs.length) {
    const idx = Number.isInteger(body.draft_index) ? body.draft_index : 0;
    const ts = now().replace(/[-:]/g, '').replace(/\.\d+Z$/, 'Z');
    const rec = {
      job_id: jobId, draft_index: idx, created_at: now(),
      draft: body.draft && typeof body.draft === 'object' ? body.draft : {},
      final: body.final && typeof body.final === 'object' ? body.final : {},
      diffs,
    };
    const name = `${ts}_${jobId}_${idx}.json`;
    await gh.createFile(vault, `${feedbackDir(name)}/${name}`,
      Buffer.from(JSON.stringify(rec, null, 2) + '\n'), `karte: 訂正ログ ${jobId}`);
  }
  let job = 'kept';
  if (body.last) {
    // その写真の訂正のうち、まだ読み癖に取り込まれていないものがあれば写真を残す
    const mem = await readMemory();
    const upto = (mem && mem.last_feedback) || '';
    const waiting = (await feedbackNames(upto)).some(n => n.includes(jobId));
    const { inbox } = gh.conf();
    if (waiting) {
      const next = await gh.updateJson(inbox, jobPath(jobId), j => Object.assign(j, { status: 'feedback' }), `karte-inbox: ${jobId} → feedback`);
      job = next ? 'feedback' : 'deleted';
    } else {
      await removeJob(jobId);
      job = 'deleted';
    }
  }
  return { recorded: diffs.length, job };
}

async function getMemory() {
  const mem = await readMemory();
  const upto = (mem && mem.last_feedback) || '';
  return { memory: mem, pending: (await feedbackNames(upto)).length };
}

// ─── 完成したカルテ ───
async function saveRecord(body) {
  const { vault } = gh.conf();
  const client = String(body.client_name || '').trim();
  const id = String(body.id || '').trim();
  const content = String(body.content || '');
  if (!client || !id) throw fail('顧客名と id が必要です', 400);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(String(body.date || ''))) throw fail('日付の形式が不正です', 400);
  if (!content.trim()) throw fail('内容が空です', 400);
  if (Buffer.byteLength(content) > MAX_RECORD_BYTES) throw fail('カルテが大きすぎます', 413);
  const path = `${DIR_RECORDS}/${safeName(client)}/${safeName(id)}.md`;
  await gh.upsertFile(vault, path, Buffer.from(content.endsWith('\n') ? content : content + '\n'), `karte: ${safeName(id)}`);
  return { path };
}

module.exports = { safeName, workerStatus, listJobs, createJob, updateJob, removeJob, addFeedback, getMemory, saveRecord };
