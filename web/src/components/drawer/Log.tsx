import { useState } from 'react';

import { get } from '../../api';
import { usePoll } from '../../hooks';

/** 日志尾巴。loguru 一直在写，面板里看得到就不必 SSH。 */
export function Log() {
  const [only, setOnly] = useState('');
  const { data, reload } = usePoll(
    () => get<{ lines: string[] }>(`/api/log?tail=300${only ? `&only=${encodeURIComponent(only)}` : ''}`),
    0, [only]);
  const lines = data?.lines ?? [];
  return (
    <>
      <div className="tabs">
        <button className={only ? '' : 'on'} onClick={() => setOnly('')}>全部</button>
        <button className={only ? 'on' : ''} onClick={() => setOnly('WARNING,ERROR')}>
          只看 WARNING/ERROR
        </button>
        <button onClick={reload}>刷新</button>
      </div>
      {lines.length
        ? lines.map((l, i) => <div key={i} className={`logline ${severity(l)}`}>{l}</div>)
        : <div className="empty">没有日志</div>}
    </>
  );
}

const severity = (line: string) =>
  line.includes('| ERROR') ? 'err' : line.includes('| WARNING') ? 'warn' : '';
