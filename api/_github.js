// GitHub の Contents / Git Data API 経由で Vault（obsidian-vault リポジトリ）を読み書きする。
//
//   Vault    : main ブランチ。完成したカルテ・訂正ログ・読み癖など「残すデータ」を Karte/ 配下に置く
//   Inbox    : 使い捨てブランチ karte-inbox。写真と読み取りの処理状態だけ（履歴ごと作り直せる）
//
// 環境変数: GITHUB_TOKEN（必須・対象リポジトリの Contents 読み書き権限）/ GITHUB_REPO / VAULT_BRANCH /
//           INBOX_REPO / INBOX_BRANCH / GITHUB_API_URL（テスト用）
const { fail } = require('./_lib');

const DEFAULT_REPO = 'accees7106-lab/obsidian-vault';

function conf() {
  const token = process.env.GITHUB_TOKEN;
  if (!token) throw fail('サーバー設定 GITHUB_TOKEN がありません', 500);
  const repo = process.env.GITHUB_REPO || DEFAULT_REPO;
  const vault = { repo, branch: process.env.VAULT_BRANCH || 'main' };
  const inbox = { repo: process.env.INBOX_REPO || repo, branch: process.env.INBOX_BRANCH || 'karte-inbox' };
  // 使い捨てブランチは履歴ごと作り直すので、Vault 本体と同じ場所を指していたら動かさない
  if (inbox.repo === vault.repo && inbox.branch === vault.branch) throw fail('INBOX_BRANCH が Vault のブランチと同じです', 500);
  return { token, vault, inbox, api: (process.env.GITHUB_API_URL || 'https://api.github.com').replace(/\/+$/, '') };
}

async function gh(method, path, body) {
  const c = conf();
  const res = await fetch(c.api + path, {
    method,
    headers: Object.assign({
      Authorization: `Bearer ${c.token}`,
      Accept: 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28',
      'User-Agent': 'karte-md-nblm',
    }, body ? { 'Content-Type': 'application/json' } : {}),
    body: body ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  return { ok: res.ok, status: res.status, data };
}

const enc = p => p.split('/').map(encodeURIComponent).join('/');
const contentsPath = (loc, p) => `/repos/${loc.repo}/contents/${enc(p)}`;
const sleep = ms => new Promise(r => setTimeout(r, ms));
const retryMs = () => Number(process.env.KARTE_RETRY_MS == null ? 400 : process.env.KARTE_RETRY_MS);
const ATTEMPTS = 6;

// 同じブランチへの同時書き込みで返る競合（409 / sha 不一致の 422）
const isConflict = r => r.status === 409 || (r.status === 422 && /sha/i.test(JSON.stringify(r.data)));

// GitHub の書き込み制限（内容を作る操作が短時間に続いたとき）
const isRateLimited = r => r.status === 429 || (r.status === 403 && /rate limit|abuse/i.test(JSON.stringify(r.data)));
function writeFailure(r) {
  return isRateLimited(r) ? fail('GitHub の書き込み制限に当たりました。少し待ってからもう一度お試しください', 429)
    : fail('Vault への書き込みに失敗しました', 502);
}

async function getFile(loc, path) {
  const r = await gh('GET', `${contentsPath(loc, path)}?ref=${encodeURIComponent(loc.branch)}`);
  if (r.status === 404) return null;
  if (!r.ok) throw fail('Vault の読み取りに失敗しました', 502);
  if (Array.isArray(r.data) || r.data.type !== 'file') throw fail('ファイルではありません: ' + path, 502);
  // 1MB を超えるファイル（写真）は中身が付かない。sha だけ返す
  return { sha: r.data.sha, buf: r.data.encoding === 'base64' ? Buffer.from(r.data.content, 'base64') : null };
}

async function listDir(loc, path) {
  const r = await gh('GET', `${contentsPath(loc, path)}?ref=${encodeURIComponent(loc.branch)}`);
  if (r.status === 404) return [];
  if (!r.ok) throw fail('Vault の一覧取得に失敗しました', 502);
  return Array.isArray(r.data) ? r.data : [];
}

async function put(loc, path, buf, message, sha) {
  return gh('PUT', contentsPath(loc, path), {
    message, branch: loc.branch, content: Buffer.from(buf).toString('base64'), ...(sha ? { sha } : {}),
  });
}

// 新規作成（既にあれば上書きしない）
async function createFile(loc, path, buf, message) {
  for (let i = 0; i < ATTEMPTS; i++) {
    const r = await put(loc, path, buf, message);
    if (r.ok) return r.data.content.sha;
    if (!isConflict(r)) throw writeFailure(r);
    await sleep(retryMs() * (i + 1) * (0.5 + Math.random()));
  }
  throw fail('Vault への書き込みが競合しました。もう一度お試しください', 503);
}

// あれば上書き、無ければ作成
async function upsertFile(loc, path, buf, message) {
  for (let i = 0; i < ATTEMPTS; i++) {
    const cur = await getFile(loc, path);
    const r = await put(loc, path, buf, message, cur && cur.sha);
    if (r.ok) return r.data.content.sha;
    if (!isConflict(r)) throw writeFailure(r);
    await sleep(retryMs() * (i + 1) * (0.5 + Math.random()));
  }
  throw fail('Vault への書き込みが競合しました。もう一度お試しください', 503);
}

// 読む → 変える → 書く（競合したら読み直してやり直す）。mutate が null を返したら書かない
async function updateJson(loc, path, mutate, message) {
  for (let i = 0; i < ATTEMPTS; i++) {
    const cur = await getFile(loc, path);
    if (!cur || !cur.buf) return null;
    const next = mutate(JSON.parse(cur.buf.toString('utf8')));
    if (next == null) return null;
    const r = await put(loc, path, Buffer.from(JSON.stringify(next, null, 2) + '\n'), message, cur.sha);
    if (r.ok) return next;
    if (!isConflict(r)) throw writeFailure(r);
    await sleep(retryMs() * (i + 1) * (0.5 + Math.random()));
  }
  throw fail('Vault への書き込みが競合しました。もう一度お試しください', 503);
}

async function deleteFile(loc, path, message) {
  for (let i = 0; i < ATTEMPTS; i++) {
    const cur = await getFile(loc, path);
    if (!cur) return false;
    const r = await gh('DELETE', contentsPath(loc, path), { message, branch: loc.branch, sha: cur.sha });
    if (r.ok) return true;
    if (r.status === 404) return false;
    if (!isConflict(r)) throw fail('Vault からの削除に失敗しました', 502);
    await sleep(retryMs() * (i + 1) * (0.5 + Math.random()));
  }
  throw fail('Vault への書き込みが競合しました。もう一度お試しください', 503);
}

const INBOX_README = [
  '# karte-inbox（使い捨て）',
  '',
  'カルテの手書きノート読み取りで、写真と処理状態を一時的に置くブランチです。',
  '完成したカルテ・訂正ログ・読み癖は main の Karte/ にあります。',
  '履歴ごと作り直されるので、ここに大事なものを置かないでください。',
  '',
].join('\n');

const refPath = branch => `heads/${branch.split('/').map(encodeURIComponent).join('/')}`;

// 親を持たない（履歴が空の）コミットを作って、ブランチをそこへ向ける。force=true なら既存ブランチを作り直す
async function rootCommitBranch(loc, message, force) {
  const blob = await gh('POST', `/repos/${loc.repo}/git/blobs`, { content: INBOX_README, encoding: 'utf-8' });
  if (!blob.ok) throw fail('inbox ブランチを作れませんでした', 502);
  const tree = await gh('POST', `/repos/${loc.repo}/git/trees`, { tree: [{ path: 'README.md', mode: '100644', type: 'blob', sha: blob.data.sha }] });
  if (!tree.ok) throw fail('inbox ブランチを作れませんでした', 502);
  const commit = await gh('POST', `/repos/${loc.repo}/git/commits`, { message, tree: tree.data.sha, parents: [] });
  if (!commit.ok) throw fail('inbox ブランチを作れませんでした', 502);
  const ref = force
    ? await gh('PATCH', `/repos/${loc.repo}/git/refs/${refPath(loc.branch)}`, { sha: commit.data.sha, force: true })
    : await gh('POST', `/repos/${loc.repo}/git/refs`, { ref: `refs/${refPath(loc.branch)}`, sha: commit.data.sha });
  if (!ref.ok && ref.status !== 422) throw fail('inbox ブランチを作れませんでした', 502); // 422 = 同時に作られた
}

async function ensureInbox() {
  const { inbox } = conf();
  const r = await gh('GET', `/repos/${inbox.repo}/git/ref/${refPath(inbox.branch)}`);
  if (r.ok) return;
  if (r.status !== 404) throw fail('Vault の確認に失敗しました', 502);
  await rootCommitBranch(inbox, 'karte-inbox: init', false);
}

module.exports = { conf, getFile, listDir, createFile, upsertFile, updateJson, deleteFile, ensureInbox };
