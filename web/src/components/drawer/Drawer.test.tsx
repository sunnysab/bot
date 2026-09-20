/** 抽屉的两种模式必须互相分得开：点气泡进来的是「这条消息」，表头「状态」
 *  进来的是那排 tab。混在一起就会点一条消息先看到「健康」。
 */

import { renderToStaticMarkup } from 'react-dom/server';
import { expect, test } from 'vitest';

import type { Tab } from './Drawer';
import { Drawer } from './Drawer';

const base = {
  tab: 'health' as Tab,
  onTab: () => {},
  onClose: () => {},
  msgId: '1',
  picked: '正文',
  traceTick: 0,
  health: null,
  onRefresh: () => {},
  chatName: '',
};

test('消息模式：只有标题，没有健康/配置那排 tab', () => {
  const out = renderToStaticMarkup(<Drawer {...base} panel="message" />);
  expect(out).toContain('这条消息');
  expect(out).not.toContain('健康');
  expect(out).not.toContain('配置');
});

test('状态模式：是那排 tab，没有「这条消息」', () => {
  const out = renderToStaticMarkup(<Drawer {...base} panel="status" />);
  expect(out).toContain('健康');
  expect(out).toContain('配置');
  expect(out).not.toContain('这条消息');
});

test('关着就什么都不画', () => {
  expect(renderToStaticMarkup(<Drawer {...base} panel={null} />)).toBe('');
});
