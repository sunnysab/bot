import { Fragment } from 'react';

/** 两列的键值表。值已经是字符串（调用方自己决定怎么格式化），这里只排版 */
export function Kv({ rows }: { rows: [string, string][] }) {
  return (
    <div className="kv">
      {rows.map(([k, v], i) => (
        <Fragment key={`${k}-${i}`}><span>{k}</span><span>{v}</span></Fragment>
      ))}
    </div>
  );
}

/** 一段等宽文本。空了写「（无）」——留白会让人以为是加载中 */
export function Pre({ text }: { text?: string }) {
  return <pre>{text || '（无）'}</pre>;
}
