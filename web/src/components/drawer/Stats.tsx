import { useState } from 'react';

import { get } from '../../api';
import type { StatsRes } from '../../api';
import { percent } from '../../format';
import { usePoll } from '../../hooks';
import { LABEL } from '../../status';
import { Kv } from './parts';

const WINDOWS: [number, string][] = [[1, '1 小时'], [24, '24 小时'], [168, '7 天']];

/** 统计看板。计数的分母来自 sidecar，被点名延迟来自 trace 文件，token 来自进程内累计。 */
export function Stats() {
  const [hours, setHours] = useState(24);
  const { data } = usePoll(() => get<StatsRes>(`/api/stats?hours=${hours}`), 0, [hours]);
  if (!data) return <div className="empty">取中…</div>;
  if (data.error) return <div className="empty">取不到统计：{data.error}</div>;

  const rows = Object.entries(data.counts ?? {}).sort((a, b) => b[1] - a[1]);
  const total = rows.reduce((n, [, v]) => n + v, 0) || 1;
  const u = data.usage;
  return (
    <>
      <div className="tabs">
        {WINDOWS.map(([h, label]) => (
          <button key={h} className={hours === h ? 'on' : ''} onClick={() => setHours(h)}>{label}</button>
        ))}
      </div>
      <h3>比率</h3>
      <Kv rows={[
        ['门控放行率', percent(data.rates.gate_pass)],
        ['planner 沉默率', percent(data.rates.planner_silent)],
        ['回复率', percent(data.rates.reply)],
        ['失败率', percent(data.rates.failed)],
      ]} />
      <h3>被点名延迟（最近 {data.latency.n} 轮）</h3>
      <Kv rows={[['p50 / p95', `${data.latency.p50 ?? '—'} / ${data.latency.p95 ?? '—'} ms`]]} />
      <h3>专注 / 累计</h3>
      <Kv rows={[
        ['专注进 / 出', `${data.focus.entered} / ${data.focus.exited}`],
        ['回复·表情·错别字', `${data.counters.reply} · ${data.counters.emoji} · ${data.counters.typo}`],
      ]} />
      <h3>token（进程内累计，重启归零）</h3>
      <Kv rows={[
        ['调用次数', String(u.calls)],
        ['prompt / completion', `${u.prompt} / ${u.completion}`],
        ['缓存命中 / 写入', `${u.cached} / ${u.cache_write}（命中率 ${percent(u.cache_hit_rate)}）`],
      ]} />
      <h3>状态分布（共 {total} 条事件）</h3>
      {rows.length ? rows.map(([status, n]) => (
        <div className="bar" key={status}>
          <span>{LABEL[status] ?? status}</span>
          <i style={{ width: `${Math.max(2, n / total * 100)}%` }} />
          <em>{n}</em>
        </div>
      )) : <div className="detail">这段时间没有事件（看板是从启用后台开始计的）</div>}
    </>
  );
}
