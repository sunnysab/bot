/** 气泡渲染的断言。用 `react-dom/server` 直接出字符串——不需要 jsdom，
 *  也不用 @testing-library（多一个依赖只为这几行断言不值）。
 */

import { renderToStaticMarkup } from 'react-dom/server';
import { expect, test } from 'vitest';

import type { Msg } from '../api';
import { Message } from './Message';

const msg = (over: Partial<Msg>): Msg => ({
  cursor: 1, msg_id: '123', ts: 1700000000, sender: 'Alice', sender_head: '',
  outgoing: false, system: false, text: '', xml: '',
  status: '', status_detail: '', score: 0, reply_status: '', reply_detail: '',
  dry_text: '', media: null, quote: null, ...over,
});

const html = (m: Msg) =>
  renderToStaticMarkup(
    <Message m={m} selected={false} flash={false}
             onPick={() => {}} onZoom={() => {}} onJump={() => Promise.resolve(false)} />,
  );

test('正文默认被转义，不再自己拼 HTML', () => {
  const out = html(msg({ text: '<b>hi</b>' }));
  expect(out).toContain('&lt;b&gt;hi&lt;/b&gt;');
  expect(out).not.toContain('<b>hi</b>');
});

test('附件按 kind 画 img/audio/video，认不出的退回文字', () => {
  const base = { msg_id: '7', text: '[图片]' };
  expect(html(msg({ ...base, media: { kind: 'image', md5: '', description: '', duration: 0, size: 0 } })))
    .toContain('src="/api/media?msg_id=7"');
  expect(html(msg({ ...base, media: { kind: 'voice', md5: '', description: '', duration: 3, size: 0 } })))
    .toContain('<audio');
  expect(html(msg({ ...base, media: { kind: 'video', md5: '', description: '', duration: 0, size: 0 } })))
    .toContain('<video');
  // 未知类型不能画成半个标签
  expect(html(msg({ ...base, media: { kind: 'sticker', md5: '', description: '', duration: 0, size: 0 } })))
    .toContain('<div class="body">[图片]</div>');
});

test('引用画成引用块，引用者名字也转义', () => {
  const out = html(msg({ text: '正文', quote: { name: '<b>B</b>', text: '被引', ref_msg_id: '' } }));
  expect(out).toContain('class="quote');
  expect(out).toContain('&lt;b&gt;B&lt;/b&gt;');
  expect(out).not.toContain('<b>B</b>');
});

test('有被引 msg_id 时说「点一下跳过去」，没有就说「看全文」', () => {
  const jump = html(msg({ quote: { name: 'B', text: '被引', ref_msg_id: '999' } }));
  expect(jump).toContain('点一下跳过去');
  const fold = html(msg({ quote: { name: 'B', text: '被引', ref_msg_id: '' } }));
  expect(fold).toContain('点一下看全文');
});

test('系统提示画成居中一行，不是气泡（没有头像/气泡/状态章）', () => {
  const out = html(msg({ system: true, text: '拍了拍 李四的脑袋' }));
  expect(out).toContain('class="sysline"');
  expect(out).not.toContain('bubble');
  expect(out).not.toContain('av');
});

test('头像：有 URL 就画 img，没 URL 或不认就退成首字块', () => {
  expect(html(msg({ sender_head: 'https://wx.qlogo.cn/a/132' })))
    .toContain('src="https://wx.qlogo.cn/a/132"');
  const bare = html(msg({ text: 'x' }));
  expect(bare).toContain('av fallback');
  expect(bare).toContain('>A<');            // 「Alice」的首字
});

test('出站带 out 类，演习回复单独一行', () => {
  expect(html(msg({ outgoing: true, text: 'x' }))).toContain('row out');
  expect(html(msg({ text: 'x', dry_text: '演习' }))).toContain('dry-text');
});

test('状态章用中文标签，出站进度挂在被回复的那条上', () => {
  expect(html(msg({ text: 'x', status: 'gated', status_detail: '45/60' }))).toContain('跳过·门控');
  expect(html(msg({ text: 'x', reply_status: 'sent' }))).toContain('已回复');
});

test('图片就地打开查看器，不新开标签页', () => {
  const out = html(msg({ text: '[图片]', media: { kind: 'image', md5: '', description: '', duration: 0, size: 0 } }));
  expect(out).not.toContain('window.open');
  expect(out).not.toMatch(/<img[^>]+target=/);
});
