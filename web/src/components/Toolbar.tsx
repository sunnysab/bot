import { useState } from 'react';

interface Props {
  chatName: string;
  allowed: boolean;
  focus: boolean;
  onAllow: () => void;
  onFocus: () => void;
  onEarlier: () => void;
  /** 发一条。返回输入框的新 placeholder（成功是「已排队」，失败是错误） */
  onSend: (text: string) => Promise<string>;
}

export function Toolbar({
  chatName, allowed, focus, onAllow, onFocus, onEarlier, onSend,
}: Props) {
  const [say, setSay] = useState('');
  const [hint, setHint] = useState('手动发一条…');
  const idle = !chatName;

  const send = async () => {
    const text = say.trim();
    if (!text) return;
    setSay('');
    setHint(await onSend(text));
  };

  return (
    <div className="toolbar">
      <b>{chatName || '未选会话'}</b>
      <button className={allowed ? 'on' : ''} disabled={idle} onClick={onAllow}>允许回复</button>
      <button className={focus ? 'on' : ''} disabled={idle} onClick={onFocus}>专注</button>
      <span className="grow" />
      <input
        value={say} placeholder={hint} disabled={idle}
        style={{ width: 240, padding: '3px 8px', border: '1px solid var(--line)', borderRadius: 6 }}
        onChange={e => setSay(e.target.value)}
        onKeyDown={e => { if (e.key === 'Enter') void send(); }}
      />
      <button disabled={idle} onClick={send}>发送</button>
      <button disabled={idle} onClick={onEarlier} title="往上加载更早的消息">更早</button>
    </div>
  );
}
