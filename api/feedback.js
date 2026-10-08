// 読み取りの訂正（学習用）
//   POST  {job_id, draft_index, draft, final, diffs, last}
//         下書きを保存したときの「AI の読み取り」と「人が直した最終版」の差分を記録する。
//         last=true（その写真の下書きがすべて片付いた）のとき：
//           訂正が1件も無ければ写真とジョブを消す／あれば status=feedback にして写真を残す
//           （ノートPCの ocr-worker が写真と訂正を見比べて読み癖を学習し、その後に消す）
//   GET   学習済みの読み癖（最新版）と、まだ学習に回っていない訂正の件数
const { fail, sb, validId, deleteJobRow, route } = require('./_lib');

const MAX_DIFFS = 200;

async function postFeedback({ body }) {
  const jobId = validId(body.job_id);
  const diffs = Array.isArray(body.diffs) ? body.diffs.slice(0, MAX_DIFFS) : [];
  if (diffs.length) {
    const ins = await sb('/rest/v1/karte_ocr_feedback', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Prefer: 'return=minimal' },
      body: JSON.stringify({
        job_id: jobId,
        draft_index: Number.isInteger(body.draft_index) ? body.draft_index : 0,
        draft: body.draft && typeof body.draft === 'object' ? body.draft : {},
        final: body.final && typeof body.final === 'object' ? body.final : {},
        diffs,
      }),
    });
    if (!ins.ok) throw fail('訂正の記録に失敗しました', 502);
  }
  let job = 'kept';
  if (body.last) {
    const r = await sb(`/rest/v1/karte_ocr_feedback?select=id&job_id=eq.${jobId}&limit=1`);
    if (!r.ok) throw fail('訂正の確認に失敗しました', 502);
    if (Array.isArray(r.data) && r.data.length) {
      const u = await sb(`/rest/v1/karte_ocr_jobs?id=eq.${jobId}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json', Prefer: 'return=minimal' },
        body: JSON.stringify({ status: 'feedback' }),
      });
      if (!u.ok) throw fail('ジョブの更新に失敗しました', 502);
      job = 'feedback';
    } else {
      await deleteJobRow(jobId);
      job = 'deleted';
    }
  }
  return { recorded: diffs.length, job };
}

async function getMemory() {
  const m = await sb('/rest/v1/karte_ocr_memory?select=version,created_at,rules,aliases,feedback_count&order=version.desc&limit=1');
  const p = await sb('/rest/v1/karte_ocr_feedback?select=id&consolidated=eq.false&limit=1000');
  if (!m.ok || !p.ok) throw fail('学習内容の取得に失敗しました', 502);
  return {
    memory: (Array.isArray(m.data) && m.data[0]) || null,
    pending: Array.isArray(p.data) ? p.data.length : 0,
  };
}

module.exports = route({ GET: getMemory, POST: postFeedback });
