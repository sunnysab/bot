import { useEffect, useState } from 'react';

import { get, post } from '../../api';
import type { DaemonInfo, LoginPoll, LoginQr } from '../../api';

interface Props {
  daemon: DaemonInfo | undefined;
  /** 登录成功 / 退出后让 App 立刻重取健康状态，头栏那颗灯跟着变 */
  onRefresh: () => void;
}

/** 登录卡：扫码登录 / 退出登录。二维码由 daemon 现取，状态靠轮询
 *  `/api/login`；面板自己不等，扫多久都行。
 */
export function Login({ daemon, onRefresh }: Props) {
  const [qr, setQr] = useState<LoginQr | null>(null);
  const [msg, setMsg] = useState('');
  const d = daemon ?? { ok: false };

  // 轮询扫码结果。terminal 状态一出现就把 qr 清掉，副作用自然停
  useEffect(() => {
    if (!qr) return;
    let timer = 0;
    let stop = false;
    const tick = async () => {
      const r = await get<LoginPoll>('/api/login');
      if (stop) return;
      if (r.status === 'logged_in') {
        setQr(null);
        setMsg(`登录成功：${r.nickname || r.wxid || ''}`);
        onRefresh();
        return;
      }
      if (r.status === 'cancelled') { setQr(null); setMsg('已在手机上取消'); return; }
      if (r.status === 'idle') { setQr(null); setMsg('登录流程已结束：' + (r.error ?? '')); return; }
      // Retry：网络拖了一下，接着等
      if (r.error) setMsg('等待中…（' + r.error + '）');
      timer = window.setTimeout(tick, 2000);
    };
    timer = window.setTimeout(tick, 2000);
    return () => { stop = true; window.clearTimeout(timer); };
  }, [qr, onRefresh]);

  if (!d.ok) return <div className="empty">取不到 daemon 状态</div>;

  if (d.logged_in) {
    return (
      <>
        <h3>已登录</h3>
        <div className="kv">
          <span>账号</span><span>{d.nickname}（{d.wxid}）</span>
        </div>
        <div className="cfg-actions"><button onClick={logout}>退出登录</button></div>
        <div className="detail">退出会服务端注销、停长链并清本地会话材料（凭据保留）。想再上就重新扫码。</div>
      </>
    );
  }

  return (
    <>
      <h3>扫码登录</h3>
      <div className="detail">
        daemon 当前未登录{d.status === 'expired' ? '（会话已失效，需重新登录）' : ''}。
      </div>
      {!qr ? (
        <div id="qrbox"><button onClick={start}>出示二维码</button></div>
      ) : (
        <div id="qrbox">
          {qr.png ? (
            <img
              alt="登录二维码" src={`data:image/png;base64,${qr.png}`}
              style={{ width: 220, height: 220, imageRendering: 'pixelated' }}
            />
          ) : (
            <div className="detail">daemon 没回图，请自己把 url 生成二维码：<br />{qr.url}</div>
          )}
          <div className="detail">{msg || `用手机微信扫一扫（${qr.expires_in ?? 300}s 内有效）`}</div>
          <button onClick={cancel}>取消</button>
        </div>
      )}
      {!qr && msg && <div className="detail">{msg}</div>}
    </>
  );

  async function start() {
    setMsg('');
    const r = await post<LoginQr>('/api/login');
    if (r.error) { setMsg('取二维码失败：' + r.error); return; }
    setQr(r);
  }

  async function cancel() {
    setQr(null);
    setMsg('');
    await post('/api/login_cancel');
    onRefresh();
  }

  async function logout() {
    if (!confirm('退出登录会停掉 daemon 的长链，确认？')) return;
    const r = await post('/api/logout');
    if (r.error) setMsg('退出失败：' + r.error);
    onRefresh();
  }
}
