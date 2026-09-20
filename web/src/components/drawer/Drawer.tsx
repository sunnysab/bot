import type { HealthRes, Res } from '../../api';
import { Assets } from './Assets';
import { Config } from './Config';
import { Health } from './Health';
import { Login } from './Login';
import { Log } from './Log';
import { Stats } from './Stats';
import { Trace } from './Trace';

export type Tab = 'health' | 'login' | 'log' | 'stats' | 'assets' | 'config';
/** 抽屉的两种模式：看某一条消息（点气泡进来）/ 看面板自己（表头「状态」进来） */
export type Panel = 'message' | 'status';

const TABS: [Tab, string][] = [
  ['health', '健康'], ['login', '登录'], ['log', '日志'],
  ['stats', '统计'], ['assets', '资料'], ['config', '配置'],
];

interface Props {
  panel: Panel | null;
  tab: Tab;
  onTab: (tab: Tab) => void;
  onClose: () => void;
  msgId: string | null;
  picked: string;
  /** 选中那条的原始载荷，喂给「这条消息」的 XML 折叠块 */
  pickedXml: string;
  traceTick: number;
  health: Res<HealthRes> | null;
  onRefresh: () => void;
  chatName: string;
}

/** 右侧抽屉。
 *
 * **「这条消息」不在这排 tab 里**：它是点气泡出来的，跟健康/日志/配置这些
 * 「面板自己」的东西不是一回事。两种模式互斥，切回来就是重新取一次。
 */
export function Drawer({
  panel, tab, onTab, onClose, msgId, picked, pickedXml, traceTick, health, onRefresh, chatName,
}: Props) {
  if (!panel) return null;
  return (
    <div className="drawer">
      <span className="close" onClick={onClose}>✕</span>
      {panel === 'message' ? (
        <>
          <h3>这条消息</h3>
          <Trace msgId={msgId} picked={picked} xml={pickedXml} tick={traceTick} />
        </>
      ) : (
        <>
          <div className="tabs">
            {TABS.map(([key, label]) => (
              <button key={key} className={key === tab ? 'on' : ''} onClick={() => onTab(key)}>{label}</button>
            ))}
          </div>
          {tab === 'health' && <Health health={health} />}
          {tab === 'login' && <Login daemon={health?.daemon} onRefresh={onRefresh} />}
          {tab === 'log' && <Log />}
          {tab === 'stats' && <Stats />}
          {tab === 'assets' && <Assets />}
          {tab === 'config' && <Config chat={chatName} onSaved={onRefresh} />}
        </>
      )}
    </div>
  );
}
