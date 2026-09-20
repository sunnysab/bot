import { memo, useLayoutEffect } from 'react';
import type { MutableRefObject, RefObject } from 'react';

import type { Msg, StatusEvent } from '../api';
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

interface Props {
  messages: Msg[];
  own: Record<string, StatusEvent>;
  reply: Record<string, StatusEvent>;
  selected: string | null;
  error: string;
  hasChat: boolean;
  box: RefObject<HTMLElement | null>;
  scroll: MutableRefObject<ScrollHint>;
  onPick: (msgId: string) => void;
  onZoom: (url: string) => void;
}

/** 消息流。
 *
 * 包 `memo`：App 每秒因为事件游标重画，300 条气泡不该跟着重建一遍。
 */
export const Stream = memo(function Stream({
  messages, own, reply, selected, error, hasChat, box, scroll, onPick, onZoom,
}: Props) {
  useLayoutEffect(() => {
    const el = box.current;
    const hint = scroll.current;
    if (el) {
      if (hint.bottom) el.scrollTop = el.scrollHeight;
      else if (hint.keep != null) el.scrollTop = el.scrollHeight - hint.keep;
    }
    scroll.current = {};
  }, [messages, box, scroll]);

  return (
    <section id="stream" ref={box}>
      {!hasChat ? (
        <div className="empty">左边选一个会话</div>
      ) : error ? (
        <div className="empty">消息没取到：{error}（2 秒后重试）</div>
      ) : !messages.length ? (
        <div className="empty">这个会话没有消息</div>
      ) : (
        messages.map(m => (
          <Message
            key={m.msg_id} m={m} own={own[m.msg_id]} reply={reply[m.msg_id]}
            selected={selected === m.msg_id} onPick={onPick} onZoom={onZoom}
          />
        ))
      )}
    </section>
  );
});
