import { useEffect, useState } from 'react';

import { get, post } from '../../api';
import type { ReplayRes, TraceView } from '../../api';
import { usePoll } from '../../hooks';
import { Chip } from '../Chip';
import { Kv, Pre, Xml } from './parts';

interface Props {
  msgId: string | null;
  /** 选中那条气泡的正文。以前从 DOM 里捞，现在直接从 messages 里取 */
  picked: string;
  /** 选中那条的原始载荷（XML）。渲染层丢掉的字段只能从这里看 */
  xml: string;
  /** 事件轮询说「这条消息有新事件」，靠它重取 */
  tick: number;
}

/** 「这条消息」：为什么被跳过 / 这一轮模型看到了什么。 */
export function Trace({ msgId, picked, xml, tick }: Props) {
  const { data } = usePoll(
    () => msgId ? get<TraceView>(`/api/trace/${encodeURIComponent(msgId)}`) : Promise.resolve(null),
    0, [msgId, tick]);
  const [out, setOut] = useState('');
  const [replayOn, setReplayOn] = useState(false);

  // 试回是后台跑的（一轮模型调用 20 秒以上），发起之后轮询同一个地址取结果
  const { data: job } = usePoll(
    () => (replayOn && msgId
      ? get<ReplayRes>(`/api/replay/${encodeURIComponent(msgId)}`)
      : Promise.resolve(null)),
    replayOn ? 1500 : 0, [msgId, replayOn]);

  useEffect(() => {
    if (!job) return;
    if (job.status === 'done') {
      setOut(`会回（${job.latency_ms} ms）：\n${(job.reply ?? []).join('\n') || '（不发言）'}`);
      setReplayOn(false);
    } else if (job.status === 'error') {
      setOut(`失败：${job.error ?? '未知错误'}`);
      setReplayOn(false);
    }
  }, [job]);

  if (!msgId) return <div className="empty">点一条消息看它为什么被跳过</div>;
  const view = data;
  if (!view) return <div className="empty">取中…</div>;
  if (view.error) return <div className="empty">取不到：{view.error}</div>;
  const tr = view.trace;

  const replay = async () => {
    setOut('试回中（会真的调模型，但不会发出去）…');
    setReplayOn(true);
    const r = await post<ReplayRes>(`/api/replay/${encodeURIComponent(msgId)}`, {});
    if (r.error) {
      setOut(`失败：${r.error}`);
      setReplayOn(false);
    }
  };

  return (
    <>
      <div className="kv">
        <span>内容</span><span>{picked}</span>
        <span>msg_id</span><span>{view.msg_id}</span>
        <span>自己的状态</span>
        <span>{view.event ? <Chip status={view.event.status} detail={view.event.detail} /> : '（没有埋点）'}</span>
        <span>回复进度</span>
        <span>{view.reply ? <Chip status={view.reply.status} detail={view.reply.detail} /> : '—'}</span>
      </div>

      {/* 渲染层把 XML 收成了人话，出问题时（引用/媒体解析）只有原文能说明白 */}
      {xml && (
        <details className="xml">
          <summary>原始 XML</summary>
          <Xml text={xml} />
        </details>
      )}

      {!tr ? (
        <>
          <h3>模型轮次</h3>
          <div className="detail">没有 trace——门控就拦下了，没走到模型</div>
        </>
      ) : (
        <>
          <h3>模型轮次</h3>
          <Kv rows={[
            ['触发', `${tr.trigger} · 分数 ${tr.score} · ${(tr.reasons ?? []).join('+')}`],
            ['延迟', `${tr.latency_ms} ms`],
            ['回复', (tr.reply ?? []).join(' / ') || '（不发言）'],
          ]} />
          <h3>决策</h3>
          <Pre text={tr.plan} />
          <h3>当时看到的聊天记录</h3>
          <Pre text={tr.transcript} />
          <h3>试回</h3>
          <div className="detail">
            用当前代码把这条重跑一遍，**不发送**：跑的是真实链路（agent 模式也一样）。
            会真的花 token。上下文用现在还能重建的那份——消息还在窗口里就几乎和当时一致，
            滑出去了就只能从 daemon 拉一段冷的（图会变成占位符）。
          </div>
          <div style={{ margin: '6px 0' }}>
            <button onClick={replay}>试回一次</button>
          </div>
          {/* 没重放过就不画这个框：`.drawer pre` 带边框，空着就是一个多余的空盒子 */}
          {out && <pre>{out}</pre>}
        </>
      )}
    </>
  );
}
