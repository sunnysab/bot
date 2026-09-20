import { memo, useState } from 'react';

import { mediaUrl } from '../api';
import type { Msg, StatusEvent } from '../api';
import { clock } from '../format';
import { Chip } from './Chip';

interface Props {
  m: Msg;
  /** 这条消息自己的状态事件（入站埋点）。没被事件碰过就是 undefined，引用天然稳定 */
  own?: StatusEvent;
  /** 挂在它上面的出站进度事件 */
  reply?: StatusEvent;
  selected: boolean;
  onPick: (msgId: string) => void;
  onZoom: (url: string) => void;
}

/** 一条气泡。
 *
 * `memo` 不是可选项：面板每秒拉一次事件，300 条气泡都得靠「props 引用没变就跳过」。
 * App 在合并事件时只替换变化的那几个 key，所以 `own`/`reply` 对没动的消息保持同一引用。
 */
export const Message = memo(function Message({ m, own, reply, selected, onPick, onZoom }: Props) {
  return (
    <div className={`row ${m.outgoing ? 'out' : ''}`}>
      <div className={`bubble ${selected ? 'sel' : ''}`} onClick={() => onPick(m.msg_id)}>
        <div className="who">{m.sender} · {clock(m.ts)}</div>
        {m.quote && <div className="quote"><b>{m.quote.name}</b>：{m.quote.text}</div>}
        {m.media ? <Media m={m} onZoom={onZoom} /> : <div className="body">{m.text}</div>}
        {m.dry_text && <div className="body dry-text">{m.dry_text}</div>}
        <div className="meta">
          <Chip status={own?.status || m.status} detail={own ? own.detail : m.status_detail} />
        </div>
        {m.reply_status && (
          <div className="meta">
            <Chip status={reply?.status || m.reply_status} detail={reply ? reply.detail : m.reply_detail} />
          </div>
        )}
      </div>
    </div>
  );
});

/** 附件主体：取不到字节就退回占位文本（图片/语音各自失败点不同） */
function Media({ m, onZoom }: { m: Msg; onZoom: (url: string) => void }) {
  const [failed, setFailed] = useState(false);
  const info = m.media!;
  const url = mediaUrl(m.msg_id);
  const fail = () => setFailed(true);
  if (failed) return <div className="body">{m.text}</div>;

  if (info.kind === 'image' || info.kind === 'emoji') {
    return (
      <>
        <img
          className={`media ${info.kind}`} loading="lazy" src={url} alt={info.description}
          onError={fail} onClick={e => { e.stopPropagation(); onZoom(url); }}
        />
        {info.description && <div className="detail">{info.description}</div>}
      </>
    );
  }
  if (info.kind === 'voice') {
    // preload=none：不点开就不去 daemon 要字节（语音要现下 + 转码，慢）
    return (
      <>
        <audio controls preload="none" src={url} onError={fail} />
        <div className="detail">{m.text}</div>
      </>
    );
  }
  if (info.kind === 'video') {
    return <video className="media" controls preload="none" src={url} onError={fail} />;
  }
  // 其它：老实留文字
  return <div className="body">{m.text}</div>;
}
