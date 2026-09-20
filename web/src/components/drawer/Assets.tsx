import { useState } from 'react';

import { get, post } from '../../api';
import type { EmojiItem, Moment } from '../../api';
import { stamp } from '../../format';
import { usePoll } from '../../hooks';

/** 资料：提示词 / 表情库 / 记忆。三份都是「看得见、能删/能改」。 */
export function Assets() {
  const [tick, setTick] = useState(0);
  // 提示词在编的时候不要被重取覆盖，所以草稿单独存；没编过就用服务端那份
  const [drafts, setDrafts] = useState<Record<string, string>>({});

  const { data: prompts } = usePoll(() => get<Record<string, string>>('/api/prompts'), 0, [tick]);
  const { data: emojis } = usePoll(() => get<{ emojis: EmojiItem[] }>('/api/emojis'), 0, [tick]);
  const { data: moments } = usePoll(() => get<{ moments: Moment[] }>('/api/moments'), 0, [tick]);
  const reload = () => { setDrafts({}); setTick(t => t + 1); };

  const savePrompt = async (name: string) => {
    const text = drafts[name] ?? prompts?.[name] ?? '';
    const r = await post<{ saved: string; lines: number }>('/api/prompts', { name, text });
    alert(r.error ? `保存失败：${r.error}` : `已保存 ${r.saved}（${r.lines} 行）并热重载`);
  };
  const delEmoji = async (md5: string) => {
    if (!confirm('删掉这张表情？场景描述是花钱识图来的，删了不再补。')) return;
    await post('/api/emoji_delete', { md5 });
    reload();
  };
  const delMoment = async (id: number) => {
    if (!confirm('删掉这条记忆？')) return;
    await post('/api/moment_delete', { id });
    reload();
  };

  const list = emojis?.emojis ?? [];
  const memos = moments?.moments ?? [];
  return (
    <>
      <h3>提示词（保存即热重载）</h3>
      {Object.entries(prompts ?? {}).map(([name, text]) => (
        <div key={name} style={{ marginBottom: 10 }}>
          <div className="detail">
            {name}.txt <button onClick={() => savePrompt(name)}>保存并热重载</button>
          </div>
          <textarea
            value={drafts[name] ?? text}
            onChange={e => setDrafts(d => ({ ...d, [name]: e.target.value }))}
          />
        </div>
      ))}
      <h3>表情库（{list.length} 张；⛔ = 缺下载地址发不出去）</h3>
      {list.length ? list.map(x => (
        <div className="bar" key={x.md5}>
          <span title={x.md5}>{x.desc || '(无标签)'}{x.sendable ? '' : ' ⛔'}</span>
          <em style={{ flex: 1 }}>{x.scene || '（没写场景）'}</em>
          <em>{x.count}</em>
          <button onClick={() => delEmoji(x.md5)}>删</button>
        </div>
      )) : <div className="detail">空库</div>}
      <h3>记忆（{memos.length} 条）</h3>
      {memos.length ? memos.map(x => (
        <div className="mem" key={x.id}>
          <div className="detail">
            {x.chat} · {stamp(x.t_start)}
            <button style={{ float: 'right' }} onClick={() => delMoment(x.id)}>删</button>
          </div>
          <div>{x.summary}</div>
        </div>
      )) : <div className="detail">还没有记忆（消息滑出窗口 100 条才压一批）</div>}
    </>
  );
}
