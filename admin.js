/* 面板的纯渲染函数：不碰 DOM、不读全局状态，所以能直接用 node 跑断言
 * （`node --test test_admin_ui.js`）。浏览器里通过 window.AdminUI 暴露给
 * admin.html 的内联脚本，node 里走 module.exports。
 *
 * 只放「给定数据 → HTML 字符串」这类函数。任何读写页面状态、发请求、操作 DOM 的
 * 东西都留在 admin.html —— 那些正是要人盯着看的部分，不值得为测试把它们抽象掉。
 *
 * **整体包在 IIFE 里**：普通 <script> 顶层的 const 会进全局词法作用域，内联脚本
 * 再解构一次 `const { esc } = ...` 就成了重复声明（SyntaxError，整页脚本停住）。
 */
(function () {
  'use strict';

  // 状态 → 中文 / 颜色分组。新增状态忘了加这里时，chip 会显示裸状态名；
  // admin.py 的自检会读这个文件，断言每个 Status 都在 LABEL 里
  const LABEL = {
    received: '已接收', stale: '积压·只记不回', phone: '手机发的', muted: '已静音',
    gated: '跳过·门控', backoff: '跳过·退避', rate_limited: '跳过·太频繁',
    deciding: '决策中', planner_silent: '不发言', planner_failed: '决策调用失败',
    expressing: '生成回复中', express_failed: '表达失败', empty_reply: '回复为空',
    placeholder_blocked: '占位符已拦', queued: '已排队', typing: '输入中',
    sent: '已回复', dry_run: '演习回复', failed: '发送报错'
  };
  const GROUP = {
    received: 'pending', deciding: 'pending', expressing: 'pending', queued: 'pending', typing: 'pending',
    sent: 'replied', phone: 'phone', dry_run: 'dry_run',
    stale: 'skipped', muted: 'skipped', gated: 'skipped', backoff: 'skipped',
    rate_limited: 'skipped',
    planner_silent: 'skipped', empty_reply: 'skipped', placeholder_blocked: 'skipped',
    planner_failed: 'warn', express_failed: 'failed', failed: 'failed'
  };
  const SPIN = new Set(['deciding', 'expressing', 'queued', 'typing']);

  // 转义必须覆盖 `"`：好几个模板把它用在 `data-*="${esc(...)}"` 里
  const esc = s => (s ?? '').toString().replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

  function chip(status, detail) {
    if (!status) return '';
    const spin = SPIN.has(status) ? '<span class="spin">◌</span> ' : '';
    return `<span class="chip ${GROUP[status] || ''}">${spin}${LABEL[status] || status}</span>`
      + (detail ? `<span class="detail">${esc(detail)}</span>` : '');
  }

  // 附件主体：有字节就直接画，取不到就把占位文本还回来（图片/语音各自失败点不同）。
  // data-fallback 里存的就是 render() 给的 [图片] / [表情：…] / [语音 3″]。
  function mediaBody(r) {
    const m = r.media, url = `/api/media?msg_id=${encodeURIComponent(r.msg_id)}`;
    const fb = `data-fallback="${esc(r.text)}" onerror="mediaFail(this)"`;
    if (m.kind === 'image' || m.kind === 'emoji') {
      const cap = m.description ? `<div class="detail">${esc(m.description)}</div>` : '';
      return `<img class="media ${m.kind}" loading="lazy" src="${url}" ${fb}
                   onclick="window.open(this.src)">${cap}`;
    }
    if (m.kind === 'voice') {
      // preload=none：不点开就不去 daemon 要字节（语音要现下+转码，慢）
      return `<audio controls preload="none" src="${url}" ${fb}></audio>
              <div class="detail">${esc(r.text)}</div>`;
    }
    if (m.kind === 'video') {
      // daemon 已支持 CDN 帧下视频（filetype=4）；preload=none 理由同语音
      return `<video class="media" controls preload="none" src="${url}" ${fb}></video>`;
    }
    // 其它：老实留文字
    return `<div class="body">${esc(r.text)}</div>`;
  }

  function row(r) {
    const m = r.reply_status ? `<div class="meta" data-reply="${r.msg_id}">${chip(r.reply_status, r.reply_detail)}</div>` : '';
    const dry = r.dry_text ? `<div class="body dry-text">${esc(r.dry_text)}</div>` : '';
    const quote = r.quote ? `<div class="quote"><b>${esc(r.quote.name)}</b>：${esc(r.quote.text)}</div>` : '';
    const body = r.media ? mediaBody(r) : `<div class="body">${esc(r.text)}</div>`;
    const t = new Date(r.ts * 1000).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
    return `<div class="row ${r.outgoing ? 'out' : ''}" data-id="${r.msg_id}">
      <div class="bubble" onclick="pick('${r.msg_id}')">
        <div class="who">${esc(r.sender)} · ${t}</div>
        ${quote}${body}${dry}
        <div class="meta" data-own="${r.msg_id}">${chip(r.status, r.status_detail)}</div>
        ${m}
      </div></div>`;
  }

  // 一页消息里的游标范围。心跳事件只给 msg_id（64 位），不能拿来比大小，所以只用 cursor
  function cursorsOf(rows) {
    const cs = rows.map(m => m.cursor).filter(c => typeof c === 'number' && c);
    return { oldest: cs.length ? Math.min(...cs) : 0, newest: cs.length ? Math.max(...cs) : 0 };
  }

  const AdminUI = { LABEL, GROUP, SPIN, esc, chip, mediaBody, row, cursorsOf };
  if (typeof module !== 'undefined' && module.exports) module.exports = AdminUI;
  if (typeof window !== 'undefined') window.AdminUI = AdminUI;
})();
