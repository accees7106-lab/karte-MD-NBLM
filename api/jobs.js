// 手書きノート読み取りジョブの受付（Vercel Function）
//   POST   写真を受け取り、Vault リポジトリの使い捨てブランチ karte-inbox に保存してジョブ（queued）を作る
//   GET    画面に出すジョブ（読み取り待ち・読み取り中・完了・エラー）と読み取り結果
//   PATCH  ?id=  {status:'queued'} エラーを再読み取りに戻す／{status:'imported'} 下書きに取り込んだ印
//   DELETE ?id=  ジョブと写真を削除
// 読み取り本体はノートPCの ocr-worker が行う（ここでは AI を呼ばない）。
// 取り込み後の写真は、訂正の学習（api/feedback.js → ocr-worker）が済むまで残す。
const store = require('./_store');
const { validId, route } = require('./_lib');

module.exports = route({
  GET: async () => {
    const [jobs, worker] = await Promise.all([store.listJobs(), store.workerStatus()]);
    return { jobs, worker };
  },
  POST: ({ body }) => store.createJob(body),
  PATCH: ({ query, body }) => store.updateJob(validId(query.id), body && body.status),
  DELETE: async ({ query }) => { const id = validId(query.id); return { id, deleted: await store.removeJob(id) }; },
});
