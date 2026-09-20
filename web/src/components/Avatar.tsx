import { useState } from 'react';

/** 头像。两种用法（消息气泡、会话列表）共用一套。
 *
 * 拿不到 URL 是**常态**：群里多数人不在联系人表里（头像唯一来源是 getcontact），
 * 所以退成首字色块不是「兜底」，而是常见形态。色块按名字定色，同一个人颜色稳定，
 * 群里一眼分得清谁是谁。加载失败（URL 过期 / 外网不通）也走同一条路。
 */
export function Avatar({ head, name, size = 36 }: { head: string; name: string; size?: number }) {
  // `!head || bad`：head 变了（切会话 / 后端补上头像）就自动重试，不用 effect 同步
  const [bad, setBad] = useState(false);
  const style = { width: size, height: size };
  if (!head || bad) {
    return (
      <div className="av fallback" style={{ ...style, background: tint(name) }} title={name}>
        {(name || '?').slice(0, 1)}
      </div>
    );
  }
  return (
    <img className="av" style={style} src={head} alt={name} title={name}
         loading="lazy" onError={() => setBad(true)} />
  );
}

// 微信头像底色偏灰调；6 个够用，多了反而分不清
const TINTS = ['#7c9cbf', '#8fb07a', '#bf9c7c', '#a58ec2', '#c28e8e', '#7ba8a8'];

const tint = (name: string) => {
  let h = 0;
  for (const c of name) h = (h + c.codePointAt(0)!) % 997;
  return TINTS[h % TINTS.length];
};
