import { useState } from 'react';

/** 输入区。放在消息流下面（微信的位置），管理按钮留在顶栏。
 *
 * 手动发的这条走 `chat_id`（peer id），不是显示名——daemon 只认 peer id。
 */
export function Composer({ chatName, onSend }: {
  chatName: string;
  /** 返回新的 placeholder（成功是「已排队」，失败是错误） */
  onSend: (text: string) => Promise<string>;
}) {
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
    <div className="composer">
      <input
        value={say} placeholder={hint} disabled={idle}
        onChange={e => setSay(e.target.value)}
        onKeyDown={e => { if (e.key === 'Enter') void send(); }}
      />
      <button disabled={idle} onClick={send}>发送</button>
    </div>
  );
}
