/* i18n.js —— 批H：界面语言（text-as-key：中文即键；切换 = 写存储 + 整页 reload）
   机制：
   - t(zh, vars?)：翻译单个字符串；en 缺项回退中文原文；{x} 插值。
   - data-i18n / data-i18n-title / data-i18n-placeholder：静态骨架标记；
     data-i18n 只翻译元素的「直接文本节点」（嵌套元素自挂自己的标记）。
   - 切换语义：setLang() 写 localStorage（tsov.lang）+ <html lang>；调用方 reload。
   - ?lang=zh|en 优先于存储（并写回存储）——便于验收脚本与分享。
   - document.title 特判翻译（head 不进 body 遍历）。 */
import { EN } from './i18n-en.js';

/* 语言解析在模块顶层完成——ES module 按依赖序求值，i18n.js 先于各消费模块的
   顶层常量求值（state.js 的参数字典等），切换 = reload 天然一致。 */
function _resolveLang() {
  try {
    const q = new URLSearchParams(location.search).get('lang');
    if (q === 'en' || q === 'zh') {
      try { localStorage.setItem('tsov.lang', q); } catch (e) { /* ignore */ }
      return q;
    }
  } catch (e) { /* ignore */ }
  try { return localStorage.getItem('tsov.lang') || 'zh'; } catch (e) { return 'zh'; }
}

let LANG = _resolveLang();
document.documentElement.lang = (LANG === 'en') ? 'en' : 'zh-CN';

export function getLang() { return LANG; }

export function setLang(l) {
  LANG = (l === 'en') ? 'en' : 'zh';
  try { localStorage.setItem('tsov.lang', LANG); } catch (e) { /* ignore */ }
  document.documentElement.lang = (LANG === 'en') ? 'en' : 'zh-CN';
}

/* 初始化（boot 第一步）：en 时整体翻译静态骨架（DOM 已就绪：module 脚本天然 defer） */
export function initI18n() {
  if (LANG === 'en') {
    applyI18n(document.body);
    document.title = t(document.title);
  }
}

/* 翻译单个字符串：en 模式查词典；带 {name} 占位时做替换（中英共用一个签名） */
export function t(zh, vars) {
  let s = zh;
  if (LANG === 'en') s = (EN[zh] !== undefined) ? EN[zh] : zh;
  if (vars) for (const k in vars) s = s.split('{' + k + '}').join(String(vars[k]));
  return s;
}

/* 静态骨架应用器：属性面 + data-i18n 元素的直接文本节点 */
export function applyI18n(root) {
  if (LANG !== 'en' || !root) return;
  for (const el of root.querySelectorAll('[data-i18n-title]')) el.title = t(el.title);
  for (const el of root.querySelectorAll('[data-i18n-placeholder]')) el.placeholder = t(el.placeholder);
  for (const el of root.querySelectorAll('[data-i18n]')) {
    for (const node of el.childNodes) {
      if (node.nodeType !== Node.TEXT_NODE) continue;
      const m = node.nodeValue.match(/^(\s*)([\s\S]*?)(\s*)$/);
      if (!m || !m[2]) continue;
      const hit = EN[m[2]];
      if (hit !== undefined) node.nodeValue = m[1] + hit + m[3];
    }
  }
}

/* 便利：动态注入 HTML 后局部翻译（H2 用；手动调用，不做全局 observer） */
export function translateIn(el) { applyI18n(el); }
