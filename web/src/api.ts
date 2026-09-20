/** 面板与后端的全部接缝。
 *
 * 两条老约定写在这儿，别的地方就不用再想：
 *
 * 1. **msg_id 一律 string。** daemon 的 server_id 是 64 位整数，实测 985 条里 958 条超过
 *    2^53。当数字过 JSON 会被 JS 悄悄改掉，然后状态永远贴不上，而且不报错。
 * 2. **会话的键是显示名**（群名 / 备注），不是 peer id。允许回复名单、分会话人设都按显示名
 *    存，插件链也按显示名分发；peer id（`chat_id`）只用来打 /api/messages、/api/media
 *    这类按会话取数据的接口。混用会写出一个和读取对不上的记录。
 *
 * 后台 500 时返回的是 `{error}`。以前失败一律给 `{}`，于是调用方只能瞎猜；现在照常解析，
 * 调用方要么读 error 说清楚，要么当没这回事——拿到的仍是普通对象，不会炸。
 */

export type Res<T extends object = object> = T & { error?: string };

async function parse<T extends object>(r: Response): Promise<Res<T>> {
  return (await r.json().catch(() => ({}) as Res<T>)) as Res<T>;
}

export function get<T extends object = object>(url: string): Promise<Res<T>> {
  return fetch(url).then(r => parse<T>(r));
}

export function post<T extends object = object>(url: string, body: unknown = {}): Promise<Res<T>> {
  return fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(r => parse<T>(r));
}

/** 附件原始字节的地址。msg_id 走 encodeURIComponent，别自己拼。 */
export const mediaUrl = (msgId: string) => `/api/media?msg_id=${encodeURIComponent(msgId)}`;

// ---------- 会话与消息 ----------

export interface Chat {
  chat_id: string;
  name: string;
  is_group: boolean;
  count: number;
  updated_at: number;
  last: string;
  allowed: boolean;
  focus: boolean;
}

export interface MediaInfo {
  kind: 'image' | 'emoji' | 'voice' | 'video' | string;
  md5: string;
  description: string;
  duration: number;
  size: number;
}

export interface Quote {
  name: string;
  text: string;
}

export interface Msg {
  cursor: number;
  msg_id: string;
  ts: number;
  sender: string;
  outgoing: boolean;
  text: string;
  status: string;
  status_detail: string;
  score: number;
  reply_status: string;
  reply_detail: string;
  dry_text: string;
  media: MediaInfo | null;
  quote: Quote | null;
}

export interface MessagesRes {
  messages: Msg[];
  has_more: boolean;
}

// ---------- 事件 ----------

export interface StatusEvent {
  seq: number;
  ts: number;
  status: string;
  chat_id: string;
  chat_name: string;
  msg_id: string;
  reply_to: string;
  detail: string;
  origin: string;
  text: string;
}

export interface EventsRes {
  seq: number;
  events: StatusEvent[];
}

// ---------- 状态 / 健康 ----------

export interface StateRes {
  wxid: string;
  nickname: string;
  dry_run: boolean;
  seq: number;
  groups: Record<string, number>;
  scope: { mode: string; allowed: string[]; blocked: string[] };
}

export interface DaemonInfo {
  ok: boolean;
  logged_in?: boolean;
  wxid?: string;
  nickname?: string;
  uin?: number;
  status?: string;
  error?: string;
}

export interface HealthRes {
  daemon: DaemonInfo;
  focus: { chat: string; rounds: number; reason: string; idle_left: number; max_left: number }[];
  focus_counts: { entered: number; exited: number };
  backoff: Record<string, number>;
  speech: Record<string, number>;
  outbox: number;
  window: Record<string, number>;
  emojis: { total: number; sendable: number; scene: number };
  moments: number;
  counters: { reply: number; emoji: number; typo: number };
  uptime: number;
}

// ---------- 抽屉 ----------

export interface TraceRecord {
  trigger: string;
  score: number;
  reasons: string[];
  latency_ms: number;
  reply: string[];
  plan: string;
  transcript: string;
  contact_name: string;
}

export interface TraceView {
  msg_id: string;
  event: StatusEvent | null;
  reply: StatusEvent | null;
  trace: TraceRecord | null;
}

export interface ReplayRes {
  plan?: string;
  reply?: string[];
  latency_ms?: number;
}

export interface StatsRes {
  hours: number;
  counts: Record<string, number>;
  rates: Record<string, number | null>;
  latency: { p50: number | null; p95: number | null; n: number };
  focus: { entered: number; exited: number };
  counters: { reply: number; emoji: number; typo: number };
  usage: {
    calls: number;
    prompt: number;
    completion: number;
    cached: number;
    cache_write: number;
    cache_hit_rate: number | null;
  };
}

export interface EmojiItem {
  md5: string;
  desc: string;
  scene: string;
  count: number;
  sendable: boolean;
}

export interface Moment {
  id: number;
  chat: string;
  t_start: number;
  t_end: number;
  summary: string;
  cues: string;
}

export interface ConfigItem {
  name: string;
  type: 'bool' | 'int' | 'float' | 'str';
  value: string | number | boolean;
  secret: boolean;
  isset: boolean;
  default: unknown;
  hot: boolean;
}

export interface ConfigRes {
  groups: { group: string; items: ConfigItem[] }[];
  dotenv: string;
}

export interface PersonaRes {
  chat: string;
  fields: string[];
  global: Record<string, string | number | string[]>;
  overrides: Record<string, string>;
}

export interface LoginQr {
  uuid?: string;
  url?: string;
  expires_in?: number;
  png?: string;
}

export interface LoginPoll {
  status: string;
  nickname?: string;
  wxid?: string;
  error?: string;
}
