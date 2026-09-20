import { memo, useEffect, useRef, useState } from 'react';

import { mediaUrl } from '../api';
import type { Msg, StatusEvent } from '../api';
import { clock } from '../format';
import { Avatar } from './Avatar';
import { Chip } from './Chip';

interface Props {
  m: Msg;
  /** 这条消息自己的状态事件（入站埋点）。没被事件碰过就是 undefined，引用天然稳定 */
  own?: StatusEvent;
  /** 挂在它上面的出站进度事件 */
  reply?: StatusEvent;
  selected: boolean;
  /** 刚从别处跳过来的那条：闪一下，让人知道落在哪里 */
  flash: boolean;
  onPick: (msgId: string) => void;
  onZoom: (url: string) => void;
  /** 点引用块：跳得过去（包含现取一页）返回 true；返回 false 时气泡自己展开被引全文 */
  onJump: (msgId: string) => Promise<boolean>;
}

/** 一条气泡。
 *
 * `memo` 不是可选项：面板每秒拉一次事件，300 条气泡都得靠「props 引用没变就跳过」。
 * App 在合并事件时只替换变化的那几个 key，所以 `own`/`reply` 对没动的消息保持同一引用。
 */
export const Message = memo(function Message(
  { m, own, reply, selected, flash, onPick, onZoom, onJump }: Props,
) {
  const [open, setOpen] = useState(false);
  const row = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (flash) row.current?.scrollIntoView({ block: 'center', behavior: 'smooth' });
  }, [flash]);

  // 点引用：先试跳过去（不在当前页时 App 会现取一段），跳不了就就地展开全文。
  // 总比「点了没反应」强
  const pickQuote = async () => {
    const ref = m.quote?.ref_msg_id;
    if (ref && await onJump(ref)) return;
    setOpen(v => !v);
  };

  // 系统提示（拍一拍/撕回/入群）：微信里是居中一行小灰字，没有头像、没有气泡、不可点
  if (m.system) return <div className="sysline">{m.text}</div>;

  return (
    <div ref={row} className={`row ${m.outgoing ? 'out' : ''} ${flash ? 'jump' : ''}`}>
      <Avatar head={m.sender_head} name={m.sender} />
      <div className={`bubble ${selected ? 'sel' : ''}`} onClick={() => onPick(m.msg_id)}>
        <div className="who">{m.sender} · {clock(m.ts)}</div>
        {m.quote && (
          <div
            className={`quote ${open ? 'open' : ''}`}
            title={m.quote.ref_msg_id ? '点一下跳过去' : '点一下看全文'}
            onClick={e => { e.stopPropagation(); void pickQuote(); }}
          >
            <b>{m.quote.name}</b>：{m.quote.text}
          </div>
        )}
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
