/* 面板纯渲染函数的断言。node 自带测试运行器，不用装任何东西：
 *
 *     node --test test_admin_ui.js
 *
 * 这里只测 admin.js 里那些「数据 → HTML 字符串」的函数——它们正是以前最容易
 * 悄悄写错、又没人发现的部分（比如把 peer id 当成显示名发出去）。
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ui = require('./admin.js');

// 最小 DOM 垫片：只求把两个脚本在**同一个全局作用域**里跑完
function domStub() {
  const el = new Proxy({}, {
    get: (_t, k) => {
      if (k === 'classList') return { add() {}, remove() {}, toggle() {}, contains() { return false; } };
      if (k === 'style' || k === 'dataset') return {};
      if (k === 'replaceWith') return () => {};
      return typeof k === 'string' ? (() => el) : undefined;
    },
    set: () => true,
  });
  const sandbox = {
    document: { querySelector: () => el, querySelectorAll: () => [], createElement: () => el, body: el },
    setInterval: () => 0,
    fetch: () => new Promise(() => {}),   // 永不 resolve：异步分支停在那儿就行
    CSS: { escape: s => s },
  };
  sandbox.window = sandbox;
  return sandbox;
}

test('esc 转义 HTML 与引号（引号很重要：模板把它用在 data-* 属性里）', () => {
  assert.equal(ui.esc('<a href="x">&</a>'), '&lt;a href=&quot;x&quot;&gt;&amp;&lt;/a&gt;');
  assert.equal(ui.esc(null), '');
  assert.equal(ui.esc(0), '0');
});

test('chip 用中文标签和颜色分组，进行中带转圈', () => {
  assert.match(ui.chip('sent'), /class="chip replied"/);
  assert.match(ui.chip('sent'), /已回复/);
  assert.match(ui.chip('rate_limited'), /跳过·太频繁/);
  assert.match(ui.chip('gated'), /class="chip skipped"/);
  assert.match(ui.chip('deciding'), /spin/);
  assert.equal(ui.chip(''), '');
  assert.match(ui.chip('sent', '<x>'), /&lt;x&gt;/);
});

test('cursorsOf 取最小/最大 cursor，忽略缺失和 0', () => {
  assert.deepEqual(ui.cursorsOf([{ cursor: 5 }, { cursor: 9 }, { cursor: null }, { cursor: 0 }]),
                   { oldest: 5, newest: 9 });
  assert.deepEqual(ui.cursorsOf([]), { oldest: 0, newest: 0 });
});

test('文本消息渲染成带 msg_id 的气泡，正文被转义', () => {
  const html = ui.row({ msg_id: '123', ts: 1700000000, sender: 'Alice', text: '<b>hi</b>' });
  assert.match(html, /data-id="123"/);
  assert.match(html, /&lt;b&gt;hi&lt;\/b&gt;/);
  assert.ok(!html.includes('<b>hi</b>'), '正文不该有未转义标签');
  assert.match(html, /pick\('123'\)/);
});

test('附件按 kind 画 img/audio/video，都留 data-fallback', () => {
  const base = { msg_id: '7', ts: 1700000000, sender: 'A', text: '[图片]' };
  const img = ui.row({ ...base, media: { kind: 'image' } });
  assert.match(img, /<img[^>]+src="\/api\/media\?msg_id=7"/);
  assert.match(img, /data-fallback="\[图片\]"/);
  assert.match(ui.row({ ...base, media: { kind: 'voice' } }), /<audio[^>]+>/);
  assert.match(ui.row({ ...base, media: { kind: 'video' } }), /<video[^>]+>/);
  // 未知类型退回文字，不能画成半个标签
  assert.match(ui.row({ ...base, media: { kind: 'sticker' } }), /<div class="body">\[图片\]<\/div>/);
});

test('引用消息画引用块，引用者名字也转义', () => {
  const html = ui.row({ msg_id: '1', ts: 1, sender: 'A', text: '正文',
                        quote: { name: '<b>B</b>', text: '被引' } });
  assert.match(html, /class="quote"/);
  assert.match(html, /&lt;b&gt;B&lt;\/b&gt;/);
  assert.ok(!html.includes('<b>B</b>'));
});

test('出站消息带 out 类，演习回复单独一行', () => {
  assert.match(ui.row({ msg_id: '1', ts: 1, sender: '我', text: 'x', outgoing: true }), /row out/);
  assert.match(ui.row({ msg_id: '1', ts: 1, sender: '我', text: 'x', dry_text: '演习' }),
             /dry-text/);
});

// 回归：admin.js 曾经是普通脚本里的顶层 `const esc`，内联脚本再解构一次
// `const { esc } = window.AdminUI` 就是重复声明 → SyntaxError，整页脚本停住。
// 分文件跑、各自 new Function 都测不出来，必须在同一个 context 里依次执行。
// admin.js + admin.html 的内联脚本跑在同一个全局作用域里——分文件各自
// new Function 测不出重复声明（曾经就这么把整页脚本搞停过）
function panelContext() {
  const context = vm.createContext(domStub());
  vm.runInContext(fs.readFileSync(path.join(__dirname, 'admin.js'), 'utf8'), context);
  const inline = fs.readFileSync(path.join(__dirname, 'admin.html'), 'utf8')
                   .split('<script>')[1].split('</script>')[0];
  vm.runInContext(inline, context);
  return context;
}

test('会话列表为空时说清楚是哪种空，不猜', () => {
  const context = panelContext();
  const why = (state, data) => {
    vm.runInContext(`daemonHeader(${JSON.stringify(state)})`, context);
    return vm.runInContext(`whyNoChats(${JSON.stringify(data)})`, context);
  };
  assert.equal(why({ ok: true, logged_in: false }, { error: '401 session expired' }),
               'daemon 未登录 —— 右上「详情 → 登录」扫码，登录后这里自动刷新');
  assert.equal(why({ ok: true, logged_in: true }, { error: 'boom' }), '取不到会话：boom');
  assert.equal(why({ ok: false }, { error: 'dial fail' }), 'daemon 连不上：dial fail');
  assert.equal(why({ ok: true, logged_in: true }, {}), '这个账号还没有会话');
});

test('admin.js 与 admin.html 的内联脚本能共存于同一个全局作用域', () => {
  const context = panelContext();
  assert.equal(vm.runInContext('Object.keys(window.AdminUI).sort().join(",")', context),
               'GROUP,LABEL,SPIN,chip,cursorsOf,esc,mediaBody,row');
});
