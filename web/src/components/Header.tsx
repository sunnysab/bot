import type { DaemonInfo, Res, StateRes } from '../api';

interface Props {
  state: Res<StateRes> | null;
  daemon: DaemonInfo | undefined;
  /** 事件轮询断了（不是 daemon 的问题，是这个页面与后台之间断了） */
  down: boolean;
  seq: number;
  asideOff: boolean;
  /** 右侧「状态」抽屉开着没有 */
  statusOpen: boolean;
  onAside: () => void;
  onDry: () => void;
  onScope: (mode: string) => void;
  onStatus: () => void;
}

export function Header({
  state, daemon, down, seq, asideOff, statusOpen, onAside, onDry, onScope, onStatus,
}: Props) {
  const s = state ?? ({} as StateRes);
  const d = daemon ?? { ok: false };
  // daemon 的三态：正常 / 未登录 / 取不到状态
  const ok = !down && d.ok && d.logged_in;
  const label = down ? '管理后台断开'
    : !d.ok ? 'daemon 取不到状态'
      : d.logged_in ? `${d.nickname || d.wxid || ''}（daemon 正常）` : '未登录';
  return (
    <header>
      <b>{(s.nickname || s.wxid || '?') + ' · bot'}</b>
      <button onClick={onAside} title="显示 / 隐藏会话列表">
        {asideOff ? '会话列表' : '收起列表'}
      </button>
      <span><span className={`dot ${ok ? 'ok' : 'bad'}`} />{label}</span>
      <span style={{ color: 'var(--dim)' }}>
        {Object.entries(s.groups ?? {}).map(([k, v]) => `${k} ${v}`).join(' · ')}
      </span>
      {s.dry_run && <span className="badge">演习模式</span>}
      <button className={s.dry_run ? 'on' : ''} onClick={onDry}>演习模式</button>
      <span style={{ color: 'var(--dim)' }}>回复范围</span>
      <select value={s.scope?.mode ?? 'all'} title="允许回复哪些会话"
              onChange={e => onScope(e.target.value)}>
        <option value="all">除静音的之外都回</option>
        <option value="allow">只回勾选的</option>
      </select>
      <span style={{ marginLeft: 'auto', color: 'var(--dim)' }}>事件 #{seq}</span>
      <button className={statusOpen ? 'on' : ''} onClick={onStatus}>状态</button>
    </header>
  );
}
