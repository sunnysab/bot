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
const ui = require('./admin.js');

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
