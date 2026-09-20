/** 原始载荷（XML）的格式化与着色。纯函数，进得了 vitest。
 *
 * 两个函数都**不做解析**：只按标签边界断行缩进 + 词法着色。daemon 给的真实样本里，
 * 有三种正经解析器会直接失败、而恰恰最需要看清的形态：
 *
 * 1. 正文前拼了 `wxid_bob:\n`（不是 XML 的一部分）
 * 2. 出站引用没有 `<msg>` 外壳，直接以 `<appmsg>` 开头
 * 3. refermsg 里那层转义嵌套（`&lt;msg&gt;…`）
 *
 * 认不出来就原样返回——格式怪不能成为「不显示」的理由。
 */

/** 缩进。按 `><` 断行，只跟「这一行开了还是闭了」的深度，不建树 */
export function format(src: string): string {
  const body = src.replace(/^\S*:\s*\n/, '').trim();
  const out: string[] = [];
  let depth = 0;
  for (const line of body.replace(/>\s*</g, '>\n<').split('\n')) {
    const t = line.trim();
    if (!t) continue;
    if (t.startsWith('</')) depth = Math.max(0, depth - 1);
    out.push('  '.repeat(depth) + t);
    if (opens(t)) depth++;
  }
  return out.join('\n');
}

/** 这行是不是「开了还没闭」的开始标签。同行开闭（`<title>x</title>`）不算 */
function opens(line: string): boolean {
  if (!line.startsWith('<') || /^<[/!?]/.test(line) || line.endsWith('/>')) return false;
  return !/<\/[A-Za-z_:][\w:.-]*\s*>/.test(line);
}

export type Kind = 'tag' | 'name' | 'attr' | 'str' | 'decl' | 'text';

/** 词法着色 → `[类型, 文本]` 序列，由 React 渲染成 span。
 *
 * **永远不拼 HTML 字符串**：正文一律转义是贯穿面板的规矩（`Message.test.tsx` 有断言守着），
 * 这里也走同一套——tokenize 出数组，React 负责转义。
 *
 * 是个状态机而不是一条大正则：正则从左往右找 token，会在属性名之前的空格上
 * 用「纯文本」分支把 `md5` 一起吃掉，属性就永远染不上色了。
 * 近似之处：属性值里的 `>` 会被当成标签结尾（载荷里不存在，不为它加引号状态）。
 */
export function tokenize(src: string): Array<[Kind, string]> {
  const out: Array<[Kind, string]> = [];
  const push = (k: Kind, s: string) => { if (s) out.push([k, s]); };
  let i = 0;
  while (i < src.length) {
    const lt = src.indexOf('<', i);
    if (lt !== i) {
      push('text', src.slice(i, lt < 0 ? src.length : lt));
      if (lt < 0) break;
      i = lt;
    }
    // 注释 / CDATA / 声明：整块一种颜色，不再往里看
    const closer = src.startsWith('<!--', i) ? '-->'
      : src.startsWith('<![CDATA[', i) ? ']]>'
        : src.startsWith('<?', i) ? '?>' : '';
    if (closer) {
      const end = src.indexOf(closer, i + 2);
      const stop = end < 0 ? src.length : end + closer.length;
      push('decl', src.slice(i, stop));
      i = stop;
      continue;
    }
    const open = /^<\/?([A-Za-z_:][\w:.-]*)/.exec(src.slice(i));
    if (!open) { push('text', '<'); i++; continue; }
    push('tag', open[0].startsWith('</') ? '</' : '<');
    push('name', open[1]);
    i += open[0].length;
    const gt = src.indexOf('>', i);
    const end = gt < 0 ? src.length : gt;
    while (i < end) {
      const rest = src.slice(i, end);
      const attr = /^([A-Za-z_:][\w:.-]*)(\s*=\s*)("[^"]*"|'[^']*')/.exec(rest);
      if (attr) {
        push('attr', attr[1]); push('text', attr[2]); push('str', attr[3]);
        i += attr[0].length;
        continue;
      }
      const gap = /^\s+/.exec(rest)?.[0];
      if (gap) { push('text', gap); i += gap.length; continue; }
      // 自闭合的那个 `/`：跟 `>` 同一种颜色，不然 `<img />` 里会夹一个灰点
      push(rest === '/' ? 'tag' : 'text', src[i]);
      i++;
    }
    if (gt >= 0) { push('tag', '>'); i = gt + 1; }
    else break;
  }
  return out;
}
