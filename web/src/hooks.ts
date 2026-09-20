/** 两个小 hook：轮询取数、记住本地偏好。没有别的东西要共享，所以不引状态库。 */

import { useCallback, useEffect, useRef, useState } from 'react';
import type { DependencyList } from 'react';

export interface Polled<T> {
  data: T | null;
  /** 请求本身失败（网络/解析）。后端说「某一路出错」是 data.error，不是这个 */
  error: string;
  reload: () => void;
}

/** 轮询取数。
 *
 * - `ms = 0`：只取一次，`deps` 变了再取。
 * - 下一次永远在上一次**回来之后**才排，所以慢接口不会叠请求。
 * - 卸载或 `deps` 变化后，旧请求的结果不再写回 state。
 * - `fn` 每次渲染都是新函数，用 ref 收着，不进依赖数组。
 */
export function usePoll<T>(fn: () => Promise<T>, ms = 0, deps: DependencyList = []): Polled<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState('');
  const [tick, setTick] = useState(0);
  const latest = useRef(fn);
  latest.current = fn;

  useEffect(() => {
    let timer = 0;
    let stop = false;
    const run = async () => {
      try {
        const value = await latest.current();
        if (!stop) { setData(value); setError(''); }
      } catch (e) {
        if (!stop) setError(String(e));
      }
      if (!stop && ms) timer = window.setTimeout(run, ms);
    };
    void run();
    return () => { stop = true; window.clearTimeout(timer); };
     
  }, [ms, tick, ...deps]);

  return { data, error, reload: useCallback(() => setTick(t => t + 1), []) };
}

/** localStorage 在隐私模式和 file:// 下会直接抛。读不到就用默认值，别让它把面板带停。 */
export const local = {
  get(key: string, fallback: string): string {
    try { return localStorage.getItem(key) ?? fallback; } catch { return fallback; }
  },
  set(key: string, value: string): void {
    try { value === '' ? localStorage.removeItem(key) : localStorage.setItem(key, value); } catch { /* 存不下就算了 */ }
  },
};
