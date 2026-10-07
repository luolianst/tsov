/* md.js —— 批B P10：LLM 输出 markdown 安全子集渲染（零依赖）。
   原则：先 HTML 转义、后渲染白名单子集；不支持的原样呈现为文本。
   子集：```代码块``` / 行内 `code` / **粗** / *斜* / #..#### 标题 / 无序·有序列表 /
         [文本](http(s) 链接) / --- 分隔线 / 段落与行内换行。 */

function esc(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function inline(s) {
  let x = s;
  x = x.replace(/`([^`]+)`/g, (m, c) => '<code>' + c + '</code>');
  x = x.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  x = x.replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, '$1<em>$2</em>');
  x = x.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
    '<a href="$2" target="_blank" rel="noopener">$1</a>');
  return x;
}

export function mdToHtml(src) {
  const raw = String(src == null ? '' : src);
  const blocks = [];
  // 1) 抽代码块（占位先行，内容只此处转义一次）
  let s = raw.replace(/```[\w+-]*\n?([\s\S]*?)```/g, (m, code) => {
    blocks.push('<pre class="md-code">' + esc(code.replace(/\n+$/, '')) + '</pre>');
    return '\u0000B' + (blocks.length - 1) + '\u0000';
  });
  // 2) 转义，再行级渲染
  s = esc(s);
  const lines = s.split('\n');
  const out = [];
  let list = null;   // 'ul' | 'ol'
  let para = [];
  const flushPara = () => {
    if (para.length) { out.push('<div class="md-p">' + para.join('<br>') + '</div>'); para = []; }
  };
  const closeList = () => { if (list) { out.push('</' + list + '>'); list = null; } };
  const flushAll = () => { flushPara(); closeList(); };
  for (const ln of lines) {
    const ph = /^\u0000B(\d+)\u0000$/.exec(ln.trim());
    if (ph) { flushAll(); out.push(blocks[Number(ph[1])] || ''); continue; }
    const h = /^(#{1,4})\s+(.+)$/.exec(ln);
    if (h) { flushAll(); out.push('<div class="md-h">' + inline(h[2]) + '</div>'); continue; }
    if (/^-{3,}$/.test(ln.trim())) { flushAll(); out.push('<hr>'); continue; }
    const ul = /^[-*]\s+(.+)$/.exec(ln);
    if (ul) {
      flushPara();
      if (list !== 'ul') { closeList(); out.push('<ul>'); list = 'ul'; }
      out.push('<li>' + inline(ul[1]) + '</li>');
      continue;
    }
    const ol = /^\d+[.)]\s+(.+)$/.exec(ln);
    if (ol) {
      flushPara();
      if (list !== 'ol') { closeList(); out.push('<ol>'); list = 'ol'; }
      out.push('<li>' + inline(ol[1]) + '</li>');
      continue;
    }
    if (ln.trim() === '') { flushAll(); continue; }
    closeList();
    para.push(inline(ln));
  }
  flushAll();
  let html = out.join('');
  // 3) 兜底：行内出现的代码块占位（罕见）还原
  html = html.replace(/\u0000B(\d+)\u0000/g, (m, i) => blocks[Number(i)] || '');
  return html;
}
