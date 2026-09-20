/** XML 格式化与着色的断言。
 *
 * 最要紧的一条是**还原性**：tokenize 出来的片段拼回去必须等于原文。
 * 着色错了只是难看，丢字符就是丢证据——抽屉存在的唯一理由就是「原文能看到」。
 */

import { expect, test } from 'vitest';

import { format, tokenize } from './xml';

// admin.py / message.py 里跑过的真实样本
const QUOTED = ('<msg><appmsg><title>直接用这个点进去对比</title><type>57</type>'
  + '<fromusername>wxid_sender</fromusername><refermsg><type>49</type>'
  + '<svrid>3159424424184903022</svrid><chatusr>wxid_kf7zbyjjxs8r22</chatusr>'
  + '<displayname>zzq</displayname><content>&lt;msg&gt;&lt;appmsg&gt;&lt;title&gt;和我一起测测'
  + '&lt;/title&gt;&lt;/appmsg&gt;&lt;/msg&gt;</content></refermsg></appmsg></msg>');
// 出站引用：没有 <msg> 外壳，正文前还拼了 wxid
const OUTBOUND = ('wxid_bob:\n<appmsg appid="" sdkver="0"><title>在呢，看你们聊呢</title><type>57</type>'
  + '<refermsg><type>1</type><svrid>4443715993374741156</svrid><content>@雨墨蝉烟 咋不说话了'
  + '</content></refermsg></appmsg><fromusername>wxid_me</fromusername>');

test('format 按层级缩进，同行开闭的标签不加深', () => {
  expect(format('<msg><a><b>1</b></a></msg>')).toBe('<msg>\n  <a>\n    <b>1</b>\n  </a>\n</msg>');
  // 自闭合不加深
  expect(format('<msg><img md5="m" /></msg>')).toBe('<msg>\n  <img md5="m" />\n</msg>');
});

test('format 剥掉 daemon 拼的 wxid 前缀，但不动别的行', () => {
  const got = format(OUTBOUND);
  expect(got.startsWith('<appmsg')).toBe(true);
  expect(got).not.toContain('wxid_bob');
});

test('format 不认识的输入原样返回，不吞内容', () => {
  expect(format('不是 xml')).toBe('不是 xml');
  expect(format('   ')).toBe('');
});

test('format 保留转义嵌套那一层的原文', () => {
  expect(format(QUOTED)).toContain('&lt;msg&gt;&lt;appmsg&gt;&lt;title&gt;和我一起测测');
});

test('tokenize 拼回去等于原文（不丢字符）', () => {
  for (const src of [QUOTED, OUTBOUND, '<!-- 注释 --><msg/>', '<?xml version="1.0"?><msg/>', '纯文本']) {
    expect(tokenize(src).map(([, t]) => t).join('')).toBe(src);
  }
  // format 后的文本同样要能原样还原
  const pretty = format(QUOTED);
  expect(tokenize(pretty).map(([, t]) => t).join('')).toBe(pretty);
});

test('tokenize 认得出标签名、属性名、属性值', () => {
  const toks = tokenize('<msg><img md5="m" /></msg>');
  expect(toks.filter(([k]) => k === 'name').map(([, t]) => t)).toEqual(['msg', 'img', 'msg']);
  expect(toks).toContainEqual(['attr', 'md5']);
  expect(toks).toContainEqual(['str', '"m"']);
  expect(toks.filter(([k]) => k === 'decl')).toEqual([]);
});

test('tokenize 把声明、注释、CDATA 整块当一种颜色', () => {
  const kinds = tokenize('<?xml version="1.0"?><!-- x --><msg/>').map(([k]) => k);
  expect(kinds[0]).toBe('decl');
  expect(kinds).toContain('decl');
});
