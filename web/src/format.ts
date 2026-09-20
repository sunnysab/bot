/** 纯格式化：不碰 DOM，进得了 vitest。 */

/** 时间。面板只看「几点几分」，日期靠上下文 */
export const clock = (ts: number) =>
  new Date(ts * 1000).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });

export const stamp = (ts: number) => new Date(ts * 1000).toLocaleString('zh-CN');

/** 比率。后端给 null 表示分母是 0，写成 — 而不是 0% */
export const percent = (v: number | null | undefined) =>
  v == null ? '—' : (v * 100).toFixed(1) + '%';

/** 一页消息里的 cursor 范围。心跳事件只给 msg_id（64 位），不能拿来比大小，所以只用 cursor */
export function cursorsOf(rows: { cursor?: number | null }[]): { oldest: number; newest: number } {
  const cs = rows.map(m => m.cursor).filter((c): c is number => typeof c === 'number' && c > 0);
  return { oldest: cs.length ? Math.min(...cs) : 0, newest: cs.length ? Math.max(...cs) : 0 };
}

/** 值 → 一格一格的 kv 表。跟后端 JSON 一个写法，字符串也带引号（读起来像真的值） */
export const json = (v: unknown) => JSON.stringify(v);
