// GitHub API の偽物（テスト用）。Contents API と Git Data API のうち、このアプリが使う部分だけを真似る。
//   node test/fake-github.js   → "PORT=<n>" を出して待ち受ける（Python のテストが起動する）
//   require('./fake-github').start() → { port, close, state }（Node のテストから使う）
// 制御用: POST /__fail {n}  次の n 回の PUT を 409 にする（同時書き込みの競合の再現）
//         POST /__reset  すべて消して、opts.seed（既定ブランチ main を持つリポジトリ）だけ作り直す
//         GET  /__tree?repo=&branch=  ファイル一覧   GET /__file?repo=&branch=&path=  中身
//         GET  /__commits?repo=&branch=  コミットの数
const http = require('http');
const crypto = require('crypto');

const sha1 = buf => crypto.createHash('sha1').update(buf).digest('hex');

function start(opts = {}) {
  const token = opts.token || 'tok';
  const state = { repos: {}, failPuts: 0, calls: [] };
  const branchOf = (repo, name, create) => {
    const r = (state.repos[repo] = state.repos[repo] || { branches: {}, objects: {} });
    if (!r.branches[name] && create) r.branches[name] = { files: new Map(), commits: [] };
    return r.branches[name];
  };
  const commit = (b, message, path, parents = null) => {
    const sha = sha1(Buffer.from(String(Math.random()) + message));
    const prev = b.commits.length ? [b.commits[b.commits.length - 1].sha] : [];
    b.commits.push({ sha, message, date: new Date(Date.now() - (state.ageMs || 0)).toISOString(), parents: parents || prev, paths: path ? [path] : [] });
    return sha;
  };
  // 実際の GitHub と同じく、既定ブランチ main は最初から存在する
  state.seed = (repo, branch = 'main') => branchOf(repo, branch, true);
  (opts.seed || []).forEach(r => state.seed(r));

  const srv = http.createServer((req, res) => {
    const chunks = [];
    req.on('data', c => chunks.push(c));
    req.on('end', () => {
      const raw = Buffer.concat(chunks);
      const url = new URL(req.url, 'http://x');
      const send = (code, body, type = 'application/json') => {
        res.writeHead(code, { 'Content-Type': type });
        res.end(Buffer.isBuffer(body) ? body : JSON.stringify(body));
      };
      const body = () => (raw.length ? JSON.parse(raw.toString('utf8')) : {});
      const path = decodeURIComponent(url.pathname);
      state.calls.push(`${req.method} ${path}`);

      if (path === '/__fail') { state.failPuts = body().n; return send(200, {}); }
      if (path === '/__age') { state.ageMs = body().ms || 0; return send(200, {}); }
      if (path === '/__readonly') { state.readOnly = !!body().on; return send(200, {}); }
      if (path === '/__reset') { state.repos = {}; state.failPuts = 0; state.readOnly = false; state.ageMs = 0; state.calls = []; (opts.seed || []).forEach(r => state.seed(r)); return send(200, {}); }
      if (path === '/__tree' || path === '/__file' || path === '/__commits') {
        const b = branchOf(url.searchParams.get('repo'), url.searchParams.get('branch'));
        if (!b) return send(404, {});
        if (path === '/__tree') return send(200, [...b.files.keys()].sort());
        if (path === '/__commits') return send(200, b.commits);
        const f = b.files.get(url.searchParams.get('path'));
        return f ? send(200, f.buf, 'application/octet-stream') : send(404, {});
      }

      if ((req.headers.authorization || '') !== `Bearer ${token}`) return send(401, { message: 'Bad credentials' });
      let m;

      // コミットの一覧（path で絞り込み、新しい順）
      if ((m = path.match(/^\/repos\/([^/]+\/[^/]+)\/commits$/)) && req.method === 'GET') {
        const b = branchOf(m[1], url.searchParams.get('sha') || 'main');
        if (!b) return send(404, { message: 'Not Found' });
        const pfx = url.searchParams.get('path');
        let list = b.commits.filter(c => !pfx || (c.paths || []).some(x => x === pfx || x.startsWith(pfx + '/'))).slice().reverse();
        list = list.slice(0, Number(url.searchParams.get('per_page') || 30));
        return send(200, list.map(c => ({ sha: c.sha, commit: { message: c.message, committer: { date: c.date } } })));
      }

      // リポジトリとブランチの確認（worker.py --check 用）
      if ((m = path.match(/^\/repos\/([^/]+\/[^/]+)$/)) && req.method === 'GET') {
        return state.repos[m[1]] ? send(200, { full_name: m[1], permissions: state.readOnly ? { push: false } : { push: true } }) : send(404, { message: 'Not Found' });
      }
      if ((m = path.match(/^\/repos\/([^/]+\/[^/]+)\/branches\/(.+)$/)) && req.method === 'GET') {
        return branchOf(m[1], m[2]) ? send(200, { name: m[2] }) : send(404, { message: 'Branch not found' });
      }

      // Contents API
      if ((m = path.match(/^\/repos\/([^/]+\/[^/]+)\/contents\/(.+)$/))) {
        const [, repo, p] = m;
        const branch = url.searchParams.get('ref') || (body().branch) || 'main';
        const b = branchOf(repo, branch);
        if (req.method === 'GET') {
          if (!b) return send(404, { message: 'No commit found for the ref ' + branch });
          const f = b.files.get(p);
          if (f) {
            if ((req.headers.accept || '').includes('raw')) return send(200, f.buf, 'application/octet-stream');
            const big = f.buf.length > 1024 * 1024;
            return send(200, { type: 'file', name: p.split('/').pop(), path: p, sha: f.sha, size: f.buf.length,
              encoding: big ? 'none' : 'base64', content: big ? '' : f.buf.toString('base64') });
          }
          const prefix = p.replace(/\/$/, '') + '/';
          const kids = new Map();
          for (const [k, v] of b.files) {
            if (!k.startsWith(prefix)) continue;
            const rest = k.slice(prefix.length), name = rest.split('/')[0];
            kids.set(name, rest.includes('/') ? { type: 'dir', name, path: prefix + name, sha: sha1(Buffer.from(prefix + name)) }
              : { type: 'file', name, path: k, sha: v.sha, size: v.buf.length });
          }
          return kids.size ? send(200, [...kids.values()]) : send(404, { message: 'Not Found' });
        }
        const b2 = branchOf(repo, body().branch);
        if (!b2) return send(404, { message: 'Branch not found' });
        if (req.method === 'PUT') {
          if (state.failPuts > 0) { state.failPuts--; return send(409, { message: 'is at abc but expected def' }); }
          const { content, sha, message } = body();
          const cur = b2.files.get(p);
          if (cur && sha !== cur.sha) return send(422, { message: cur && !sha ? '"sha" wasn\'t supplied.' : 'sha does not match' });
          if (!cur && sha) return send(422, { message: 'sha does not match' });
          const buf = Buffer.from(content, 'base64');
          const nsha = sha1(buf);
          b2.files.set(p, { buf, sha: nsha });
          commit(b2, message, p);
          return send(cur ? 200 : 201, { content: { sha: nsha, path: p } });
        }
        if (req.method === 'DELETE') {
          const cur = b2.files.get(p);
          if (!cur) return send(404, { message: 'Not Found' });
          if (body().sha !== cur.sha) return send(409, { message: 'sha does not match' });
          b2.files.delete(p);
          commit(b2, body().message, p);
          return send(200, { commit: {} });
        }
      }

      // Git Data API（inbox ブランチの作成・作り直し用）
      if ((m = path.match(/^\/repos\/([^/]+\/[^/]+)\/git\/(.+)$/))) {
        const [, repo, rest] = m;
        const r = (state.repos[repo] = state.repos[repo] || { branches: {}, objects: {} });
        if (req.method === 'POST' && rest === 'blobs') {
          const buf = Buffer.from(body().content, body().encoding === 'base64' ? 'base64' : 'utf8');
          const sha = sha1(buf); r.objects[sha] = { type: 'blob', buf }; return send(201, { sha });
        }
        if (req.method === 'POST' && rest === 'trees') {
          const sha = sha1(Buffer.from(JSON.stringify(body().tree))); r.objects[sha] = { type: 'tree', tree: body().tree };
          return send(201, { sha });
        }
        if (req.method === 'POST' && rest === 'commits') {
          const sha = sha1(Buffer.from(String(Math.random())));
          r.objects[sha] = { type: 'commit', tree: body().tree, message: body().message, parents: body().parents, date: new Date().toISOString() };
          return send(201, { sha });
        }
        if (req.method === 'POST' && rest === 'refs') {
          const name = body().ref.replace(/^refs\/heads\//, '');
          if (r.branches[name]) return send(422, { message: 'Reference already exists' });
          return send(201, { ref: body().ref, object: { sha: applyCommit(r, name, body().sha, false) } });
        }
        if ((m = rest.match(/^ref\/heads\/(.+)$/)) && req.method === 'GET') {
          const b = r.branches[m[1]];
          return b ? send(200, { object: { sha: b.commits[b.commits.length - 1].sha } }) : send(404, { message: 'Not Found' });
        }
        if ((m = rest.match(/^refs\/heads\/(.+)$/)) && req.method === 'PATCH') {
          if (!r.branches[m[1]]) return send(404, { message: 'Not Found' });
          if (!body().force) return send(422, { message: 'Update is not a fast forward' });
          return send(200, { object: { sha: applyCommit(r, m[1], body().sha, true) } });
        }
        if ((m = rest.match(/^commits\/(.+)$/)) && req.method === 'GET') {
          for (const b of Object.values(r.branches)) {
            const c = b.commits.find(x => x.sha === m[1]);
            if (c) return send(200, { sha: c.sha, message: c.message, committer: { date: c.date }, parents: c.parents });
          }
          const o = r.objects[m[1]];
          return o && o.type === 'commit' ? send(200, { sha: m[1], message: o.message, committer: { date: o.date }, parents: o.parents }) : send(404, {});
        }
      }
      send(404, { message: 'Not Found ' + path });
    });
  });

  // 作った（親なしの）コミットでブランチを作る／作り直す。tree の中身だけがファイルになる
  function applyCommit(r, name, sha, reset) {
    const o = r.objects[sha];
    const b = { files: new Map(), commits: [] };
    for (const t of (r.objects[o.tree] || {}).tree || []) {
      const blob = r.objects[t.sha];
      b.files.set(t.path, { buf: blob.buf, sha: t.sha });
    }
    b.commits.push({ sha, message: o.message, date: o.date, parents: o.parents, paths: [...b.files.keys()] });
    r.branches[name] = b;
    return sha;
  }

  return new Promise(resolve => srv.listen(0, '127.0.0.1', () => resolve({
    port: srv.address().port, state, close: () => new Promise(r => srv.close(r)),
  })));
}

module.exports = { start };

if (require.main === module) {
  start({ seed: (process.env.FAKE_SEED || '').split(',').filter(Boolean), token: process.env.FAKE_TOKEN || undefined }).then(s => { console.log('PORT=' + s.port); });
}
