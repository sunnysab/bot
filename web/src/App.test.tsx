/** 整页的初帧。数据还没到、也没选会话时不能炸，得看到「左边选一个会话」
 *  ——这正是以前最容易静默变白屏的地方（`admin.js` 没加载那种）。
 */

import { renderToStaticMarkup } from 'react-dom/server';
import { expect, test } from 'vitest';

import App from './App';

test('App 在有数据之前渲染出空壳，而不是崩掉', () => {
  const out = renderToStaticMarkup(<App />);
  expect(out).toContain('左边选一个会话');
  expect(out).toContain('未选会话');
  expect(out).toContain('会话列表');
  expect(out).toContain('事件 #0');
  // 抽屉默认关着
  expect(out).not.toContain('这条消息');
});
