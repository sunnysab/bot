import { useCallback, useEffect, useRef, useState } from 'react';

import { get, post } from './api';
import type { Chat, EventsRes, HealthRes, MessagesRes, Msg, StateRes, StatusEvent } from './api';
import { ChatList } from './components/ChatList';
import { Header } from './components/Header';
import { Lightbox } from './components/Lightbox';
import { Stream } from './components/Stream';
import type { ScrollHint } from './components/Stream';
import { Toolbar } from './components/Toolbar';
import { Drawer } from './components/drawer/Drawer';
import type { Tab } from './components/drawer/Drawer';
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
  const [tab, setTab] = useState<Tab>('trace');
  const [drawerOpen, setDrawerOpen] = useState(false);
  /** 事件说「选中的这条有新动静」时 +1，抽屉里的「这条消息」靠它重取 */
  const [traceTick, setTraceTick] = useState(0);
  const [zoom, setZoom] = useState('');
  const [seq, setSeq] = useState(0);
  const [down, setDown] = useState(false);
  const [aside, setAside] = useState(() => ({
    w: Number(local.get('aside.w', '230')) || 230,
    off: local.get('aside.off', '') === '1',
  }));

  const box = useRef<HTMLElement>(null);
  const scroll = useRef<ScrollHint>({});
  // 当前已加载消息的 daemon 游标（messages.id，不是 64 位 msg_id）：翻页和增量都靠它
  const cursor = useRef({ oldest: 0, newest: 0 });
  const since = useRef(0);
  /** 整页重拉进行中：这期间不做增量，免得和它抢 cursor */
  const busy = useRef(false);
  // 轮询回调要读最新的会话/选中，但不想因此重建定时器
  const live = useRef({ chatId, selected, tab });
  live.current = { chatId, selected, tab };

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
    const { chatId: id, selected: picked, tab: current } = live.current;
    let stale = false;
    let touched = false;
    for (const e of events) {
      if (e.chat_id && e.chat_id === id && (e.status === 'received' || e.status === 'phone')) stale = true;
      // 抽屉里画的「这条消息」只会被它自己的事件改，别的消息刷屏不必重画
      if (picked && (e.msg_id === picked || e.reply_to === picked)) touched = true;
    }
    if (stale && !busy.current) void loadNew();
    if (touched && current === 'trace') setTraceTick(t => t + 1);
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

  const onPick = useCallback((msgId: string) => setSelected(msgId), []);
  const onZoom = useCallback((url: string) => setZoom(url), []);

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

  const picked = messages.find(m => m.msg_id === selected)?.text ?? '';

  return (
    <>
      <Header
        state={state.data} daemon={health.data?.daemon} down={down} seq={seq}
        asideOff={aside.off} onAside={() => toggleAside(!aside.off)}
        onDry={dryRun} onScope={scope}
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
            chatName={chatName} allowed={!!chat?.allowed} focus={!!chat?.focus} drawerOpen={drawerOpen}
            onAllow={() => void allow(chatName, !chat?.allowed)}
            onFocus={() => void focus(chatName, !chat?.focus)}
            onEarlier={() => void loadEarlier(box.current)}
            onDrawer={() => setDrawerOpen(o => !o)}
            onSend={send}
          />
          <Stream
            messages={messages} own={own} reply={reply} selected={selected} error={streamError}
            hasChat={!!chatId} box={box} scroll={scroll} onPick={onPick} onZoom={onZoom}
          />
        </div>
        <Drawer
          open={drawerOpen} tab={tab} onTab={setTab} onClose={() => setDrawerOpen(false)}
          msgId={selected} picked={picked} traceTick={traceTick}
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
