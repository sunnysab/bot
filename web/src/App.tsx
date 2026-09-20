import { useCallback, useEffect, useRef, useState } from 'react';

import { get, post } from './api';
import type { Chat, EventsRes, HealthRes, MessagesRes, Msg, Res, StateRes, StatusEvent } from './api';
import { ChatList } from './components/ChatList';
import { Composer } from './components/Composer';
import { Header } from './components/Header';
import { Lightbox } from './components/Lightbox';
import { Stream } from './components/Stream';
import type { ScrollHint } from './components/Stream';
import { Toolbar } from './components/Toolbar';
import { Drawer } from './components/drawer/Drawer';
import type { Panel, Tab } from './components/drawer/Drawer';
import { cursorsOf } from './format';
import { local, usePoll } from './hooks';

/** 三条数据流：
 *
 * 1. 每秒一条 `/api/events` —— 状态章的唯一来源，按 msg_id / reply_to 分进两张表
 * 2. 每 5 秒 `/api/state` + `/api/health` + `/api/chats` —— 头栏、daemon 三点灯、会话列表
 * 3. 按需 `/api/messages` —— 切会话拉满一页，之后只靠事件驱动的增量
 */
export default function App() {
  const [chats, setChats] = useState<Chat[]>([]);
  const [chatsError, setChatsError] = useState('');
  const [chatId, setChatId] = useState('');
  const [messages, setMessages] = useState<Msg[]>([]);
  const [streamError, setStreamError] = useState('');
  const [own, setOwn] = useState<Record<string, StatusEvent>>({});
  const [reply, setReply] = useState<Record<string, StatusEvent>>({});
  const [selected, setSelected] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>('health');
  /** 右侧抽屉。null = 关着；'message' = 点气泡进来的那条消息；'status' = 表头「状态」 */
  const [panel, setPanel] = useState<Panel | null>(null);
  /** 事件说「选中的这条有新动静」时 +1，消息详情靠它重取 */
  const [traceTick, setTraceTick] = useState(0);
  const [zoom, setZoom] = useState('');
  const [seq, setSeq] = useState(0);
  const [down, setDown] = useState(false);
  /** 跳到某条引用后要闪的那条（空串=不闪）。用 id 而不是 ref，Message 的 memo 才认得出 */
  const [jumpId, setJumpId] = useState('');
  /** 没看过的新消息条数。不在底部时挂在「跳到最新」按钮上 */
  const [newCount, setNewCount] = useState(0);
  /** 浮条提示（跳引用没命中之类），2 秒自己消失 */
  const [note, setNote] = useState('');
  const [aside, setAside] = useState(() => ({
    w: Number(local.get('aside.w', '230')) || 230,
    off: local.get('aside.off', '') === '1',
  }));

  const box = useRef<HTMLElement>(null);
  const scroll = useRef<ScrollHint>({});
  /** 视口在不在底部。在底部新消息就自动跟下去，不在就只计数 */
  const stick = useRef(true);
  // 当前已加载消息的 daemon 游标（messages.id，不是 64 位 msg_id）：翻页和增量都靠它
  const cursor = useRef({ oldest: 0, newest: 0 });
  const since = useRef(0);
  /** 整页重拉进行中：这期间不做增量，免得和它抢 cursor */
  const busy = useRef(false);
  // 轮询回调要读最新的会话/选中，但不想因此重建定时器
  const live = useRef({ chatId, selected, panel });
  live.current = { chatId, selected, panel };

  const chat = chats.find(c => c.chat_id === chatId) ?? null;
  const chatName = chat?.name ?? '';
  const state = usePoll(() => get<StateRes>('/api/state'), 5000);
  const health = usePoll(() => get<HealthRes>('/api/health'), 5000);
  const reloadState = state.reload;
  const reloadHealth = health.reload;
  const refresh = useCallback(() => { reloadState(); reloadHealth(); }, [reloadState, reloadHealth]);

  // ---------- 会话列表 ----------

  const loadChats = useCallback(async () => {
    const d = await get<{ chats: Chat[] }>('/api/chats');
    setChats(d.chats ?? []); // 显式覆盖：登出后不能留着上一版那份列表
    setChatsError(d.error ?? '');
  }, []);

  useEffect(() => {
    void loadChats();
    // 空的会话列表多半是在等扫码，5 秒再看一眼（跟 /api/state 同一个节奏）
    const timer = window.setInterval(loadChats, 5000);
    return () => window.clearInterval(timer);
  }, [loadChats]);

  // ---------- 消息 ----------

  const loadMessages = useCallback(async (id: string) => {
    busy.current = true;
    const d = await get<MessagesRes>(`/api/messages?chat=${encodeURIComponent(id)}&limit=300`);
    busy.current = false;
    if (live.current.chatId !== id) return; // 期间切了会话，这批不算数
    if (!d.messages) {
      setMessages([]);
      setStreamError(d.error || '后台没响应');
      return;
    }
    setStreamError('');
    cursor.current = cursorsOf(d.messages);
    scroll.current = { bottom: true };
    // 切会话/重拉一定是「从头看」，跟随状态跟着归位，否则回到底部这件事再也不会自动发生
    stick.current = true;
    setNewCount(0);
    setMessages(d.messages);
  }, []);

  useEffect(() => {
    if (!chatId) return;
    setSelected(null);
    void loadMessages(chatId);
  }, [chatId, loadMessages]);

  // 后台 500 多半是 daemon RPC 超时（启动时识图/下载会把 RPC 排队），2 秒后再来一次
  useEffect(() => {
    if (!streamError) return;
    const timer = window.setTimeout(() => void loadMessages(chatId), 2000);
    return () => window.clearTimeout(timer);
  }, [streamError, chatId, loadMessages]);

  // 只拉最新的那几条追加，不再每次收到消息就重拉 300 条（长会话下这是主要开销）
  const loadNew = useCallback(async () => {
    const id = live.current.chatId;
    if (!id || busy.current) return;
    const d = await get<MessagesRes>(
      `/api/messages?chat=${encodeURIComponent(id)}&after=${cursor.current.newest}`);
    const rows = d.messages;
    if (live.current.chatId !== id || !rows?.length) return;
    setMessages(prev => [...prev, ...rows]);
    // 在底部：新消息直接跟下去（微信就是这个手感）。翻上去看历史时不动视口，
    // 只累加计数——否则每来一条消息就把人踢回底部
    if (stick.current) scroll.current = { bottom: true };
    else setNewCount(n => n + rows.length);
    const c = cursorsOf(rows);
    cursor.current = {
      newest: Math.max(cursor.current.newest, c.newest),
      oldest: cursor.current.oldest || c.oldest,
    };
  }, []);

  const loadEarlier = useCallback(async (el: HTMLElement | null) => {
    const id = live.current.chatId;
    if (!id || !cursor.current.oldest) return;
    const d = await get<MessagesRes>(
      `/api/messages?chat=${encodeURIComponent(id)}&limit=100&before=${cursor.current.oldest}`);
    const rows = d.messages;
    if (live.current.chatId !== id || !rows?.length) return;
    // 先记下视口在底部的位置，插完之后按它还原——不然内容一进来屏幕就跳走
    if (el) scroll.current = { keep: el.scrollHeight - el.scrollTop };
    setMessages(prev => [...rows, ...prev]);
    cursor.current.oldest = cursorsOf(rows).oldest;
  }, []);

  // ---------- 事件（状态章的唯一来源） ----------

  const onEvents = useCallback((events: StatusEvent[]) => {
    setOwn(prev => merge(prev, events, 'msg_id'));
    setReply(prev => merge(prev, events, 'reply_to'));
    const { chatId: id, selected: picked, panel: current } = live.current;
    let stale = false;
    let touched = false;
    for (const e of events) {
      if (e.chat_id && e.chat_id === id && (e.status === 'received' || e.status === 'phone')) stale = true;
      // 消息详情只会被它自己的事件改，别的消息刷屏不必重画
      if (picked && (e.msg_id === picked || e.reply_to === picked)) touched = true;
    }
    if (stale && !busy.current) void loadNew();
    if (touched && current === 'message') setTraceTick(t => t + 1);
  }, [loadNew]);

  useEffect(() => {
    let stopped = false;
    const poll = async () => {
      try {
        const d = await get<EventsRes>(`/api/events?since=${since.current}`);
        if (stopped) return;
        // 后台真出错时不能把游标写成 undefined：那会让下一次请求带 ?since=undefined，
        // 从此一直 500，面板就永久「断开」了
        if (d.seq !== undefined) since.current = d.seq;
        setSeq(d.seq ?? 0);
        setDown(false);
        onEvents(d.events ?? []);
      } catch {
        if (!stopped) setDown(true);
      }
    };
    const timer = window.setInterval(poll, 1000);
    return () => { stopped = true; window.clearInterval(timer); };
  }, [onEvents]);

  // ---------- 动作 ----------

  /** 点气泡 = 直接看这条的详情，不看别的 */
  const onPick = useCallback((msgId: string) => {
    setSelected(msgId);
    setPanel('message');
  }, []);
  const onZoom = useCallback((url: string) => setZoom(url), []);
  const onStick = useCallback((v: boolean) => { stick.current = v; }, []);

  const toBottom = useCallback(() => {
    const el = box.current;
    if (el) el.scrollTop = el.scrollHeight;
    setNewCount(0);
  }, []);

  /** 点引用块：当前页里没有就去 daemon 取一段以它为中心的历史，换来定位。
   *
   * 返回 false 表示真定位不了（daemon 缓存里也没这条）——气泡会自己展开被引全文。
   */
  const loadAround = useCallback(async (msgId: string): Promise<boolean> => {
    const id = live.current.chatId;
    if (!id) return false;
    setNote('定位中…');
    busy.current = true;   // 这期间不做增量，免得和整页替换抢 cursor
    let d: Res<MessagesRes>;
    try {
      d = await get<MessagesRes>(
        `/api/messages?chat=${encodeURIComponent(id)}&limit=120&around=${encodeURIComponent(msgId)}`);
    } catch (e) {
      // fetch 本身失败（后台没起/断网）：别把 busy 卡死，否则增量再也不跑了
      setNote(`取不到那条消息：${e}`);
      return false;
    } finally {
      busy.current = false;
    }
    if (live.current.chatId !== id) return false;   // 期间切了会话，这批不算数
    if (!d.messages?.length) {
      setNote(d.error || '取不到那条消息');
      return false;
    }
    cursor.current = cursorsOf(d.messages);
    stick.current = false;        // 人在历史里，新消息不该把视口踢回底部
    scroll.current = {};          // 不按 bottom/keep 摆，交给目标那条自己滚过来
    setMessages(d.messages);
    setJumpId(msgId);
    setNewCount(0);
    setNote('');
    return true;
  }, []);

  /** 点引用块：被引那条已加载就跳过去（返回 true），否则现取一段再跳。
   *
   * 「不在这一页」是真会发生的：一页 300 条，被引的可能是三天前那条。
   */
  const onJump = useCallback(async (msgId: string): Promise<boolean> => {
    if (!msgId) return false;
    if (messages.some(m => m.msg_id === msgId)) {
      setJumpId(msgId);
      setNote('');
      return true;
    }
    const ok = await loadAround(msgId);
    if (!ok) setNote('被引消息不在 daemon 的缓存里了，已展开全文');
    return ok;
  }, [messages, loadAround]);

  useEffect(() => {
    if (!jumpId) return;
    const timer = window.setTimeout(() => setJumpId(''), 1600);
    return () => window.clearTimeout(timer);
  }, [jumpId]);

  useEffect(() => {
    if (!note) return;
    const timer = window.setTimeout(() => setNote(''), 2600);
    return () => window.clearTimeout(timer);
  }, [note]);


  const allow = useCallback(async (name: string, on: boolean) => {
    // 「允许回复」名单的键是**显示名**（插件链按它分发），不是 peer id
    await post('/api/allow', { chat: name, on });
    void loadChats();
  }, [loadChats]);

  const focus = useCallback(async (name: string, on: boolean) => {
    await post('/api/focus', { chat: name, on });
    void loadChats();
  }, [loadChats]);

  const send = useCallback(async (text: string) => {
    // 手动发的一条走 chat_id（peer id）：daemon 只认它
    const r = await post('/api/send', { chat: chatId, text });
    return r.error ? `失败：${r.error}` : '已排队（走限速）';
  }, [chatId]);

  const dryRun = useCallback(async () => {
    await post('/api/dry_run', { on: !state.data?.dry_run });
    refresh();
  }, [state.data?.dry_run, refresh]);

  const scope = useCallback(async (mode: string) => {
    await post('/api/scope', { mode });
    refresh();
    void loadChats();
  }, [refresh, loadChats]);

  const toggleAside = useCallback((off: boolean) => {
    setAside(prev => ({ ...prev, off }));
    local.set('aside.off', off ? '1' : '');
  }, []);

  const setAsideWidth = useCallback((px: number) => {
    const w = Math.min(600, Math.max(130, Math.round(px)));
    setAside(prev => ({ ...prev, w }));
    local.set('aside.w', String(w));
  }, []);

  const pickedMsg = messages.find(m => m.msg_id === selected);
  const picked = pickedMsg?.text ?? '';

  return (
    <>
      <Header
        state={state.data} daemon={health.data?.daemon} down={down} seq={seq}
        asideOff={aside.off} statusOpen={panel === 'status'}
        onAside={() => toggleAside(!aside.off)} onDry={dryRun} onScope={scope}
        onStatus={() => setPanel(p => p === 'status' ? null : 'status')}
      />
      <main className={aside.off ? 'aside-off' : ''}>
        <ChatList
          chats={chats} error={chatsError} daemon={health.data?.daemon} currentId={chatId}
          off={aside.off} width={aside.w}
          onSelect={c => setChatId(c.chat_id)} onAllow={allow}
          onWidth={setAsideWidth} onToggle={() => toggleAside(!aside.off)}
        />
        <div className="center">
          <Toolbar
            chatName={chatName} allowed={!!chat?.allowed} focus={!!chat?.focus}
            onAllow={() => void allow(chatName, !chat?.allowed)}
            onFocus={() => void focus(chatName, !chat?.focus)}
            onEarlier={() => void loadEarlier(box.current)}
          />
          {note && <div className="note">{note}</div>}
          <Stream
            messages={messages} own={own} reply={reply} selected={selected} error={streamError}
            flash={jumpId} newCount={newCount} hasChat={!!chatId} box={box} scroll={scroll}
            onPick={onPick} onZoom={onZoom} onJump={onJump}
            onStick={onStick} onToBottom={toBottom}
          />
          <Composer chatName={chatName} onSend={send} />
        </div>
        <Drawer
          panel={panel} tab={tab} onTab={setTab} onClose={() => setPanel(null)}
          msgId={selected} picked={picked} pickedXml={pickedMsg?.xml ?? ''} traceTick={traceTick}
          health={health.data} onRefresh={refresh} chatName={chatName}
        />
      </main>
      <Lightbox src={zoom} onClose={() => setZoom('')} />
    </>
  );
}

/** 事件按 key 合进 map，**只替换变化的 key**。
 *
 * 没变的那几条保持同一对象引用，`Message` 的 memo 才有意义——否则每秒 300 条气泡全重画。
 */
function merge(
  prev: Record<string, StatusEvent>, events: StatusEvent[], key: 'msg_id' | 'reply_to',
): Record<string, StatusEvent> {
  let next = prev;
  for (const event of events) {
    const id = event[key];
    if (!id || next[id] === event) continue;
    if (next === prev) next = { ...prev };
    next[id] = event;
  }
  return next;
}
