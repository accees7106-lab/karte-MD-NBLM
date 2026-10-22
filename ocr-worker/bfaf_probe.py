"""BeforeAfter の画面構造を記録する（自動入力を作るための下調べ）。

使い方（ノートPC）:
    python -m pip install playwright
    python bfaf_probe.py

Edge が開くので、ふだんどおりログインして
「顧客一覧 → 顧客詳細 → カルテ新規作成（入力欄が出た状態）」まで画面を開く。
各画面でこのウィンドウに戻って Enter を押すと、その画面の構造を記録する。
終わったら q + Enter。bfaf_structure.json ができる。

記録するのは入力欄・ボタン・リンクの「種類・名前・ラベル」だけ。
入力済みの値は保存しない。数字の並び（顧客IDなど）は # に置き換え、
顧客名などの個人情報は残らないようにしている。
ログイン状態は bfaf_profile フォルダに残る（後で自動入力に使う。人に渡さない）。
"""
import json
import re
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
OUT = HERE / "bfaf_structure.json"
PROFILE = HERE / "bfaf_profile"

JS = r"""
() => {
  const mask = s => (s || '').replace(/\s+/g, ' ').trim().slice(0, 80);
  const label = el => {
    if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) return mask(l.innerText); }
    const p = el.closest('label'); if (p) return mask(p.innerText);
    return mask(el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.getAttribute('title'));
  };
  const sel = el => {
    if (el.id) return '#' + el.id;
    if (el.name) return `${el.tagName.toLowerCase()}[name="${el.name}"]`;
    return el.tagName.toLowerCase() + (el.className && typeof el.className === 'string' ? '.' + el.className.trim().split(/\s+/).join('.') : '');
  };
  const fields = [...document.querySelectorAll('input, textarea, select, [contenteditable="true"]')].map(el => ({
    tag: el.tagName.toLowerCase(), type: el.type || null, name: el.name || null, id: el.id || null,
    selector: sel(el), label: label(el), required: !!el.required,
    options: el.tagName === 'SELECT' ? [...el.options].slice(0, 30).map(o => mask(o.text)) : undefined,
    visible: !!(el.offsetWidth || el.offsetHeight),
  }));
  const buttons = [...document.querySelectorAll('button, input[type=submit], [role=button], a.btn, a.button')].map(el => ({
    text: mask(el.innerText || el.value), selector: sel(el), type: el.type || null,
  }));
  const links = [...document.querySelectorAll('a[href]')].map(a => ({ text: mask(a.innerText), href: a.getAttribute('href') }));
  const forms = [...document.forms].map(f => ({ action: f.getAttribute('action'), method: f.method, selector: sel(f) }));
  const headings = [...document.querySelectorAll('h1,h2,h3,h4,th,dt,legend')].map(h => mask(h.innerText)).slice(0, 80);
  return { title: document.title, fields, buttons, links: links.slice(0, 150), forms, headings };
}
"""


def scrub(obj, names):
    """数字列と、ページに出ていた顧客名らしき文字列を伏せる。"""
    if isinstance(obj, dict):
        return {k: scrub(v, names) for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub(v, names) for v in obj]
    if isinstance(obj, str):
        s = re.sub(r"\d{3,}", "#", obj)
        for n in names:
            s = s.replace(n, "〇〇")
        return s
    return obj


def main():
    names = [n.strip() for n in input("伏せたい顧客名があればカンマ区切りで（なければ Enter）: ").split(",") if n.strip()]
    pages = []
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(str(PROFILE), channel="msedge", headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://bfaf.jp/")
        while True:
            cmd = input("記録したい画面を開いたら Enter（終了は q）: ").strip().lower()
            if cmd == "q":
                break
            page = ctx.pages[-1]  # 新しいタブで開いた場合も最後のタブを記録
            data = page.evaluate(JS)
            data["url"] = page.url
            pages.append(scrub(data, names))
            print(f"  記録 {len(pages)}: {re.sub(r'[0-9]{3,}', '#', page.url)}  入力欄 {len(data['fields'])} / ボタン {len(data['buttons'])}")
        ctx.close()
    OUT.write_text(json.dumps(pages, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"保存しました: {OUT}")


if __name__ == "__main__":
    main()
