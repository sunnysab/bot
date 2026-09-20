import { Fragment, useState } from 'react';

import { get, post } from '../../api';
import type { ConfigItem, ConfigRes, PersonaRes } from '../../api';
import { usePoll } from '../../hooks';

/** 长文本字段独占整行（CSS 已经按这个排）。其余是单行输入 */
const LONG = new Set(['personality', 'reply_style', 'behavior_style', 'reply_styles']);
const PERSONA_LABELS: Record<string, string> = {
  personality: '人格', reply_style: '表达风格', behavior_style: '行动准则',
  reply_styles: '备用风格（| 分隔）', style_probability: '抽备用风格概率', aliases: '别名（| 分隔）',
};

interface Props {
  /** 当前会话的显示名。留空就只画全局那份 */
  chat: string;
  onSaved: () => void;
}

const HINT = '热字段保存后立即生效；带 ⚠ 的要重启进程；清空一个框 = 回默认值。';

/** 配置：全局 .env 的分组表单 + 当前会话的人设覆盖。 */
export function Config({ chat, onSaved }: Props) {
  const [tick, setTick] = useState(0);
  // 草稿只存「用户动过」的字段，没动过的显示服务端值——省掉一层初始化 effect
  const [draft, setDraft] = useState<Record<string, string | boolean>>({});
  const [personaDraft, setPersonaDraft] = useState<Record<string, string>>({});
  const [msg, setMsg] = useState(HINT);

  const { data } = usePoll(() => get<ConfigRes>('/api/config'), 0, [tick]);
  const { data: persona } = usePoll(
    () => chat ? get<PersonaRes>(`/api/persona?chat=${encodeURIComponent(chat)}`) : Promise.resolve(null),
    0, [tick, chat]);

  const valueOf = (item: ConfigItem) => draft[item.name] ?? item.value;

  const save = async (restart: boolean) => {
    const updates: Record<string, unknown> = {};
    for (const group of data?.groups ?? []) {
      for (const item of group.items) updates[item.name] = valueOf(item);
    }
    const r = await post<{ changed: Record<string, string>; stale: string[] }>('/api/config', { updates });
    if (r.error) { setMsg(`保存失败：${r.error}`); return; }
    const changed = Object.entries(r.changed ?? {});
    let text = changed.length ? '已生效：' + changed.map(([k, v]) => `${k}=${v}`).join('，') : '没有改动';
    if (r.stale?.length) text += `\n⚠ 要重启进程才生效：${r.stale.join('，')}`;
    setMsg(text);
    if (restart) { setMsg(text + '\n正在重启进程…'); await post('/api/restart'); return; }
    onSaved();
  };

  const savePersona = async () => {
    const overrides: Record<string, string> = {};
    for (const field of persona?.fields ?? []) {
      overrides[field] = (personaDraft[field] ?? String(persona?.overrides?.[field] ?? '')).trim();
    }
    const r = await post('/api/persona', { chat, overrides });
    alert(r.error ? `保存失败：${r.error}` : '已保存（下一轮生效）');
    setPersonaDraft({});
    setTick(t => t + 1);
  };

  return (
    <>
      {(data?.groups ?? []).map(group => (
        <Fragment key={group.group}>
          <h3>{group.group}</h3>
          <div className="cfg">
            {group.items.map(item => (
              <Row
                key={item.name} item={item} value={valueOf(item)}
                onChange={v => setDraft(d => ({ ...d, [item.name]: v }))}
              />
            ))}
          </div>
        </Fragment>
      ))}

      {persona && <>
        <h3>该会话的人设 · {persona.chat}</h3>
        <div className="detail">留空 = 继承全局（灰字就是全局值）；只盖这一个会话。改完下一轮生效，不用重启。</div>
        <div className="cfg">
          {persona.fields.map(field => {
            const long = LONG.has(field);
            const value = personaDraft[field] ?? String(persona.overrides?.[field] ?? '');
            const placeholder = globalOf(persona, field);
            const change = (v: string) => setPersonaDraft(d => ({ ...d, [field]: v }));
            return (
              <label key={field} className="cfg-row" style={long ? { alignItems: 'flex-start' } : undefined}>
                <span>{PERSONA_LABELS[field] ?? field}</span>
                {long
                  ? <textarea value={value} placeholder={placeholder} onChange={e => change(e.target.value)} />
                  : <input value={value} placeholder={placeholder} onChange={e => change(e.target.value)} />}
              </label>
            );
          })}
        </div>
        <div className="cfg-actions"><button onClick={savePersona}>保存本会话人设</button></div>
      </>}

      <div className="cfg-actions">
        <button onClick={() => save(false)}>保存</button>
        <button onClick={() => save(true)}>保存并重启进程</button>
      </div>
      <div className="detail" style={{ marginTop: 8, whiteSpace: 'pre-wrap' }}>{msg}</div>
    </>
  );
}

function Row({ item, value, onChange }:
  { item: ConfigItem; value: string | number | boolean; onChange: (v: string | boolean) => void }) {
  // 带 ⚠ 的字段要重启进程才生效
  const label = item.name + (item.hot ? '' : ' ⚠');
  if (item.type === 'bool') {
    return (
      <label className="cfg-row">
        <span>{label}</span>
        <input type="checkbox" checked={!!value} onChange={e => onChange(e.target.checked)} />
      </label>
    );
  }
  const long = LONG.has(item.name);
  const text = value == null ? '' : String(value);
  // 密钥不回显：留空 = 保持原值
  const placeholder = item.secret ? (item.isset ? '已设置（留空不变）' : '未设置') : '';
  return (
    <label className="cfg-row" style={long ? { alignItems: 'flex-start' } : undefined}>
      <span>{label}</span>
      {long
        ? <textarea value={text} placeholder={placeholder} onChange={e => onChange(e.target.value)} />
        : (
          <input
            type={item.secret ? 'password' : item.type === 'str' ? 'text' : 'number'}
            step={item.type === 'int' ? 1 : 'any'} value={text} placeholder={placeholder}
            onChange={e => onChange(e.target.value)}
          />
        )}
    </label>
  );
}

/** 全局值当占位符。数组（备用风格 / 别名）拼成 | 分隔的一行 */
function globalOf(persona: PersonaRes, field: string): string {
  const value = persona.global[field];
  return Array.isArray(value) ? value.join('|') : String(value ?? '');
}
