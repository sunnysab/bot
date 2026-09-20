import { Fragment, useMemo } from 'react';

import { format, tokenize } from '../../xml';

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

/** 原始载荷（XML）：先缩进再着色。
 *
 * 不引高亮库（为一个抽屉不值），也不拼 HTML 字符串——tokenize 出片段让 React 渲染，
 * 转义交给 React，和正文同一条规矩。
 */
export function Xml({ text }: { text: string }) {
  const toks = useMemo(() => tokenize(format(text)), [text]);
  return (
    <pre className="xml-body">
      {toks.map(([kind, s], i) => <span key={i} className={`t-${kind}`}>{s}</span>)}
    </pre>
  );
}
