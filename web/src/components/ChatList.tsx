import { useRef } from 'react';

import type { Chat, DaemonInfo } from '../api';

interface Props {
  chats: Chat[];
  error: string;
  daemon: DaemonInfo | undefined;
  currentId: string;
  off: boolean;
  width: number;
  onSelect: (chat: Chat) => void;
  onAllow: (name: string, on: boolean) => void;
  onWidth: (px: number) => void;
  onToggle: () => void;
}

export function ChatList({
  chats, error, daemon, currentId, off, width, onSelect, onAllow, onWidth, onToggle,
}: Props) {
  if (off) return null;
  return (
    <>
      <aside id="chats" style={{ width }}>
        {chats.length ? chats.map(c => (
          <div
            key={c.chat_id} className={`chat ${c.chat_id === currentId ? 'on' : ''}`}
            onClick={() => onSelect(c)}
          >
            <div className="n">{c.name}
              <span style={{ display: 'flex', gap: 6, alignItems: 'center', flex: 'none' }}>
                <em>{c.count || ''}</em>
                <span
                  className={`allow ${c.allowed ? 'on' : ''}`} title="允许回复"
                  onClick={e => { e.stopPropagation(); onAllow(c.name, !c.allowed); }}
                >{c.allowed ? '✓' : '✕'}</span>
              </span>
            </div>
            <div className="l">
              {c.allowed ? '' : <span className="tag">🔇不回</span>}
              {c.focus && <span className="tag">🎯专注</span>}
              {c.last}
            </div>
          </div>
        )) : <div className="empty">{whyNoChats(error, daemon)}</div>}
      </aside>
      <Splitter width={width} onWidth={onWidth} onToggle={onToggle} />
    </>
  );
}

// 分界线：拖动调宽度，双击折叠。用 pointer capture，不往 window 上挂全局监听
function Splitter({ width, onWidth, onToggle }: { width: number; onWidth: (px: number) => void; onToggle: () => void }) {
  const from = useRef(0);
  const startWidth = useRef(0);
  return (
    <div
      id="splitter" title="拖动调整宽度，双击折叠"
      onPointerDown={e => {
        e.preventDefault();
        from.current = e.clientX;
        startWidth.current = width;
        e.currentTarget.setPointerCapture(e.pointerId);
      }}
      onPointerMove={e => {
        if (e.currentTarget.hasPointerCapture(e.pointerId)) {
          onWidth(startWidth.current + e.clientX - from.current);
        }
      }}
      onDoubleClick={onToggle}
    />
  );
}

/** 会话列表为空就说清楚是哪种空。以前一律写「daemon 可能还没起」——「可能」等于没说 */
export function whyNoChats(error: string, daemon: DaemonInfo | undefined): string {
  if (!error) return '这个账号还没有会话';
  if (daemon?.ok === false) return 'daemon 连不上：' + error;
  return daemon?.logged_in === false
    ? 'daemon 未登录 —— 右上「详情 → 登录」扫码，登录后这里自动刷新'
    : '取不到会话：' + error;
}
