import { memo, useCallback, useLayoutEffect, useState } from 'react';
import type { MutableRefObject, RefObject } from 'react';

import type { Msg, StatusEvent } from '../api';
import { stamp } from '../format';
import { Message } from './Message';

/** 这次列表变化后滚动条该怎么放。
 *
 * `keep` 是「变化前的 scrollHeight - scrollTop」——往上加载更早的消息时，
 * 视口必须钉在原地，不然内容一插进来整个屏幕就跳走了。
 */
export interface ScrollHint {
  bottom?: boolean;
  keep?: number;
}

/** 都算「在底部」的容差。太小：新消息自动跟随会失灵；太大：明明没到底还偷偷滚下去 */
const STICK_PX = 40;
/** 相邻消息隔这么久就插一条时间分隔（微信是 5 分钟，这里跟着它走） */
const GAP_SECONDS = 300;

interface Props {
  messages: Msg[];
  own: Record<string, StatusEvent>;
  reply: Record<string, StatusEvent>;
  selected: string | null;
  flash: string;
  error: string;
  hasChat: boolean;
  /** 还没看过的新消息条数（不在底部时显示在按钮上） */
  newCount: number;
  box: RefObject<HTMLElement | null>;
  scroll: MutableRefObject<ScrollHint>;
  onPick: (msgId: string) => void;
  onReply: (m: Msg) => Promise<void>;
  onZoom: (url: string) => void;
  onJump: (msgId: string) => Promise<boolean>;
  /** 报告「视口在不在底部」。App 靠它决定新消息要不要自动跟下去 */
  onStick: (atBottom: boolean) => void;
  onToBottom: () => void;
}

/** 消息流。
 *
 * 包 `memo`：App 每秒因为事件游标重画，300 条气泡不该跟着重建一遍。
 */
export const Stream = memo(function Stream({
  messages, own, reply, selected, flash, error, hasChat, newCount, box, scroll,
  onPick, onReply, onZoom, onJump, onStick, onToBottom,
}: Props) {
  const [atBottom, setAtBottom] = useState(true);

  useLayoutEffect(() => {
    const el = box.current;
    const hint = scroll.current;
    if (el) {
      if (hint.bottom) el.scrollTop = el.scrollHeight;
      else if (hint.keep != null) el.scrollTop = el.scrollHeight - hint.keep;
    }
    scroll.current = {};
  }, [messages, box, scroll]);

  // 切会话 / 整页重拉之后不一定有 scroll 事件，所以这里主动算一次，
  // 不然「自动跟随」会停在上一轮的 false 上不再生效
  useLayoutEffect(() => {
    const el = box.current;
    if (!el) return;
    const bottom = el.scrollHeight - el.scrollTop - el.clientHeight < STICK_PX;
    setAtBottom(prev => (prev === bottom ? prev : bottom));
    onStick(bottom);
  }, [messages, box, onStick]);

  const onScroll = useCallback(() => {
    const el = box.current;
    if (!el) return;
    const bottom = el.scrollHeight - el.scrollTop - el.clientHeight < STICK_PX;
    setAtBottom(prev => (prev === bottom ? prev : bottom));
    onStick(bottom);
  }, [box, onStick]);

  return (
    <div className="stream-wrap">
      <section id="stream" ref={box} onScroll={onScroll}>
        {!hasChat ? (
          <div className="empty">左边选一个会话</div>
        ) : error ? (
          <div className="empty">消息没取到：{error}（2 秒后重试）</div>
        ) : !messages.length ? (
          <div className="empty">这个会话没有消息</div>
        ) : (
          withDays(messages).map(node => {
            if (typeof node === 'number') {
              return <div className="day" key={`day-${node}`}>{stamp(node)}</div>;
            }
            return (
              <Message
                key={node.msg_id} m={node} own={own[node.msg_id]} reply={reply[node.msg_id]}
                selected={selected === node.msg_id} flash={flash === node.msg_id}
                onPick={onPick} onReply={onReply} onZoom={onZoom} onJump={onJump}
              />
            );
          })
        )}
      </section>
      {!atBottom && hasChat && (
        <button className="to-bottom" onClick={onToBottom} title="跳到最新">
          {newCount ? `↓ ${newCount} 条新消息` : '↓'}
        </button>
      )}
    </div>
  );
});

/** 在时间跳空的地方插一条分隔，返回「消息 | 时间戳」的混排序列。
 *
 * 返回 `number` 表示这里要画一条时间分隔（就是它的 ts）。不用额外类型包一层——
 * 调用方只做一件事，多一个接口不值得。
 */
function withDays(messages: Msg[]): Array<Msg | number> {
  const out: Array<Msg | number> = [];
  let prev = 0;
  for (const m of messages) {
    if (!prev || m.ts - prev > GAP_SECONDS) out.push(m.ts);
    prev = m.ts;
    out.push(m);
  }
  return out;
}
