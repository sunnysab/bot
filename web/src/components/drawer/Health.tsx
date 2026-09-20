import type { HealthRes, Res } from '../../api';
import { json } from '../../format';
import { Kv } from './parts';

/** 健康：daemon 三态、专注、退避、发言频率、队列窗口库。
 *
 * 数据由 App 每 5 秒统一拉一次（头栏那颗 daemon 灯也要用），这里只画。
 */
export function Health({ health }: { health: Res<HealthRes> | null }) {
  if (!health) return <div className="empty">取中…</div>;
  if (health.error) return <div className="empty">健康检查失败：{health.error}</div>;
  if (!health.daemon) return <div className="empty">健康检查失败</div>;

  const kv = (o: object) => (
    <Kv rows={Object.entries(o).map(([k, v]) => [k, json(v) ?? ''])} />
  );
  return (
    <>
      <h3>daemon</h3>
      {kv(health.daemon)}
      <h3>专注会话（进 {health.focus_counts.entered} / 出 {health.focus_counts.exited}）</h3>
      {health.focus.length ? (
        <Kv rows={health.focus.map(f => [
          f.chat,
          `${f.rounds} 轮 · ${f.reason} · 空闲 ${Math.round(f.idle_left)}s / 上限 ${Math.round(f.max_left)}s`,
        ])} />
      ) : <div className="kv"><span>无</span><span /></div>}
      <h3>沉默退避</h3>
      {kv(health.backoff)}
      <h3>发言频率（窗口内已说几条）</h3>
      {kv(health.speech)}
      <h3>队列 / 窗口 / 库</h3>
      {kv({
        '待发队列': health.outbox,
        '模型可见条数': health.window,
        '表情（总/能发/有场景）': `${health.emojis.total}/${health.emojis.sendable}/${health.emojis.scene}`,
        '中期记忆条数': health.moments,
        '累计回复/表情/错别字': health.counters,
      })}
    </>
  );
}
