interface Props {
  chatName: string;
  allowed: boolean;
  focus: boolean;
  onAllow: () => void;
  onFocus: () => void;
  onEarlier: () => void;
}

/** 会话工具条：管理动作（允许回复 / 专注 / 更早上翻）。
 *
 * 说话在 `.composer`（底部），这里只管「这个会话怎么处理」——两件事不该混在一行里。
 */
export function Toolbar({ chatName, allowed, focus, onAllow, onFocus, onEarlier }: Props) {
  const idle = !chatName;
  return (
    <div className="toolbar">
      <b>{chatName || '未选会话'}</b>
      <button className={allowed ? 'on' : ''} disabled={idle} onClick={onAllow}>允许回复</button>
      <button className={focus ? 'on' : ''} disabled={idle} onClick={onFocus}>专注</button>
      <span className="grow" />
      <button disabled={idle} onClick={onEarlier} title="往上加载更早的消息">更早</button>
    </div>
  );
}
