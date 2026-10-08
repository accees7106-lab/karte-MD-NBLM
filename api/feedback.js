// 読み取りの訂正（学習用）
//   POST  {job_id, draft_index, draft, final, diffs, last}
//         下書きを保存したときの「AI の読み取り」と「人が直した最終版」の差分を Vault の Karte/訂正ログ/ に残す。
//         last=true（その写真の下書きがすべて片付いた）のとき：
//           学習待ちの訂正が無ければ写真とジョブを消す／あれば status=feedback にして写真を残す
//           （ノートPCの ocr-worker が写真と訂正を見比べて読み癖を学習し、その後に消す）
//   GET   学習済みの読み癖（最新版）と、まだ学習に回っていない訂正の件数
const store = require('./_store');
const { route } = require('./_lib');

module.exports = route({
  GET: () => store.getMemory(),
  POST: ({ body }) => store.addFeedback(body),
});
