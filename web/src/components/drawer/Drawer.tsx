import type { HealthRes, Res } from '../../api';
import { Assets } from './Assets';
import { Config } from './Config';
import { Health } from './Health';
import { Login } from './Login';
import { Log } from './Log';
import { Stats } from './Stats';
import { Trace } from './Trace';

export type Tab = 'trace' | 'health' | 'login' | 'log' | 'stats' | 'assets' | 'config';

const TABS: [Tab, string][] = [
  ['trace', '这条消息'], ['health', '健康'], ['login', '登录'], ['log', '日志'],
  ['stats', '统计'], ['assets', '资料'], ['config', '配置'],
];

interface Props {
  open: boolean;
  tab: Tab;
  onTab: (tab: Tab) => void;
  onClose: () => void;
  msgId: string | null;
  picked: string;
  traceTick: number;
  health: Res<HealthRes> | null;
  onRefresh: () => void;
  chatName: string;
}

/** 右侧抽屉。只挂载当前这个 tab——切回来就是重新取一次，跟改版前一致 */
export function Drawer({
  open, tab, onTab, onClose, msgId, picked, traceTick, health, onRefresh, chatName,
}: Props) {
  if (!open) return null;
  return (
    <div className="drawer">
      <span className="close" onClick={onClose}>✕</span>
      <div className="tabs">
        {TABS.map(([key, label]) => (
          <button key={key} className={key === tab ? 'on' : ''} onClick={() => onTab(key)}>{label}</button>
        ))}
      </div>
      {tab === 'trace' && <Trace msgId={msgId} picked={picked} tick={traceTick} />}
      {tab === 'health' && <Health health={health} />}
      {tab === 'login' && <Login daemon={health?.daemon} onRefresh={onRefresh} />}
      {tab === 'log' && <Log />}
      {tab === 'stats' && <Stats />}
      {tab === 'assets' && <Assets />}
      {tab === 'config' && <Config chat={chatName} onSaved={onRefresh} />}
    </div>
  );
}
