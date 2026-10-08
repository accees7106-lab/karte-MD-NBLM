// 完成したカルテを Vault（Karte/カルテ/<顧客>/）に保存する
//   POST  {id, client_name, date, content}  同じ id は上書き
// 内容は index.html が生成する Markdown（NotebookLM 用の形式）そのまま。
const store = require('./_store');
const { route } = require('./_lib');

module.exports = route({
  POST: ({ body }) => store.saveRecord(body),
});
