/** 纯函数与状态表的断言。`npm test`（vitest，node 环境，不装 jsdom）。 */

import { expect, test } from 'vitest';

import { cursorsOf, percent } from './format';
import { whyNoChats } from './components/ChatList';
import { GROUP, LABEL, SPIN } from './status';
import { zoomAt } from './zoom';

// events.py 里 Status 的全部取值。少登记一个，气泡上就会显示裸状态名
const STATUSES = [
  'received', 'stale', 'phone', 'muted', 'gated', 'backoff', 'rate_limited',
  'deciding', 'planner_silent', 'planner_failed', 'expressing', 'express_failed',
  'empty_reply', 'placeholder_blocked', 'queued', 'typing', 'sent', 'dry_run', 'failed',
];

test('每个状态都有中文标签和颜色分组', () => {
  const missing = STATUSES.filter(s => !LABEL[s] || !GROUP[s]);
  expect(missing).toEqual([]);
  // 进行中的状态才带转圈
  expect([...SPIN].every(s => STATUSES.includes(s))).toBe(true);
});

test('为什么没有会话：三种空要说清是哪种', () => {
  expect(whyNoChats('401 session expired', { ok: true, logged_in: false }))
    .toBe('daemon 未登录 —— 右上「详情 → 登录」扫码，登录后这里自动刷新');
  expect(whyNoChats('boom', { ok: true, logged_in: true })).toBe('取不到会话：boom');
  expect(whyNoChats('dial fail', { ok: false })).toBe('daemon 连不上：dial fail');
  expect(whyNoChats('', { ok: true, logged_in: true })).toBe('这个账号还没有会话');
});

test('cursorsOf 取最小/最大 cursor，忽略缺失和 0', () => {
  expect(cursorsOf([{ cursor: 5 }, { cursor: 9 }, { cursor: null }, { cursor: 0 }]))
    .toEqual({ oldest: 5, newest: 9 });
  expect(cursorsOf([])).toEqual({ oldest: 0, newest: 0 });
});

test('percent 把 null 写成 — ，不写成 0%', () => {
  expect(percent(null)).toBe('—');
  expect(percent(0)).toBe('0.0%');
  expect(percent(0.1234)).toBe('12.3%');
});

test('zoomAt 让光标底下那一点在缩放前后不动', () => {
  const near = (a: number, b: number) => expect(Math.abs(a - b)).toBeLessThan(1e-9);
  // 光标在屏幕 (mx,my)，容器中心 (cx,cy)。这条函数的全部意义就是：缩放后，
  // 原来在光标底下的那个图像点还在光标底下
  const [cx, cy, mx, my] = [400, 300, 520, 260];
  const before = { s: 1.6, x: -30, y: 12 };
  const dx = (mx - cx - before.x) / before.s; // 光标处的图像点（相对图心）
  const dy = (my - cy - before.y) / before.s;
  for (const k of [1.2, 1 / 1.2, 3, 0.01]) {
    const v = zoomAt(before, k, mx, my, cx, cy);
    near(v.s, Math.min(20, Math.max(1, before.s * k)));
    near(cx + v.x + dx * v.s, mx);
    near(cy + v.y + dy * v.s, my);
  }
  // 下限 1 = 适应窗口；上限 20，免得一把滚没了
  expect(zoomAt({ s: 1, x: 0, y: 0 }, 0.5, mx, my, cx, cy).s).toBe(1);
  expect(zoomAt({ s: 20, x: 0, y: 0 }, 1.2, mx, my, cx, cy).s).toBe(20);
  // 在中心缩放就只变倍数，不位移
  const c = zoomAt({ s: 1, x: 0, y: 0 }, 2, cx, cy, cx, cy);
  near(c.x, 0); near(c.y, 0); near(c.s, 2);
});
