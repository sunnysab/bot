""" 管理后台：本地 HTTP 服务 + 一个页面。设计见 notes/管理后台设计.md。

三条原则：

1. **消息正文的事实源是 daemon**，不在这里另存一份。这里只做两件事——
   把 daemon 的消息读出来、把 events.py 里的状态贴上去。
2. **绝不拖累聊天。** 启动整段 try/except；任何一路出错只打日志 + 返回 500。
3. **msg_id 一律字符串。** daemon 的 server_id 是 64 位整数，实测 985 条里 958 条
   超过 JS 安全整数 2^53（最大 9202842881785760589）。当数字过 JSON 会被 JS 悄悄
   改掉，然后状态就永远贴不上，而且不报错。

轮询而不是 SSE：单用户本地工具，1 秒拉一次，没有线程泄漏和背压问题，
靠 seq 游标还天生去重（见设计文档第四节）。

只绑 127.0.0.1，不做鉴权（已拍板）。代价是 /api/send 能发消息、/api/restart
能重启进程，本机任何进程都能打——接受。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sqlite3
import threading
import time
from dataclasses import MISSING, replace
from dataclasses import fields as dataclass_fields
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from dotenv import load_dotenv
from jinja2 import StrictUndefined, Template, TemplateSyntaxError, UndefinedError
from loguru import logger

from ai import usage
from config import DOTENV_PATH, Config, update_dotenv
from context import SYSTEM_KINDS, MessageKind
from events import Status, bus
from media import render, render_referred, tag
from message import parse_reference_message
from persona import OVERRIDE_FIELDS
from reply_trace import find as find_trace
from reply_trace import latencies as trace_latencies
from runtime import HOT_FIELDS, RESTART_FIELDS, ReplyScope

# 面板：web/ 是 React 源码，web/dist 是 vite 的产物。HTTP 服务只读产物，构建由
# `cd web && npm run build` 负责——所以这里不再有「页面文件」这个概念
WEB_DIR = Path(__file__).parent / 'web'
DIST = WEB_DIR / 'dist'
ASSETS = DIST / 'assets'
BUILD_HINT = ('面板还没构建（web/dist 不存在）。\n\n'
              '    cd web && npm install && npm run build\n\n'
              '开发期也可以只跑前端：cd web && npm run dev（它会自己把 /api 转发过来）\n')
SIDECAR = Path(__file__).parent / 'admin.db'
# 可以在面板上改的提示词。模板都不长，整体替换比行编辑简单也可控
PROMPTS = {'planner': Path(__file__).parent / 'planner.txt',
           'replyer': Path(__file__).parent / 'replyer.txt',
           'agent': Path(__file__).parent / 'agent.txt'}
DEFAULT_PORT = 8099          # 8080 在本机被 iptv 占了，别用
CHATS_LIMIT = 50
MESSAGES_LIMIT = 300         # v1 一次拉满，daemon 的 get_messages 还不能翻页
RPC_TIMEOUT = 15.0
LOGIN_TIMEOUT = 60.0          # 取二维码要现走一次短链握手 + getloginqrcode，比普通 RPC 慢得多
MEDIA_TIMEOUT = 60.0         # 图片要现下原图（实测 0.5~1.3s），比普通 RPC 慢得多
MEDIA_CONCURRENCY = 3        # 面板同时向 daemon 要几个附件：daemon 那边原图是一张一张下的，
                             # 这里限的是「几个 HTTP 线程被慢 RPC 占着」，别把面板的其它请求挤掉
LOG_TAIL_BYTES = 256 * 1024  # 只看日志尾巴，不把整份读进内存

# 语音转码的派生目录。**原始附件不在这里**——下载和保管都是 daemon 的事，
# bot 只按 msg_id 要字节；这里只放 SILK → WAV 的结果（浏览器放不了 SILK）
DERIVED_MEDIA_DIR = Path(__file__).parent / 'media_cache' / 'derived'
# 浏览器要的 content-type。daemon 只回扩展名，这里转一下；认不出的一律当二进制
MEDIA_TYPES = {'.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png',
               '.gif': 'image/gif', '.webp': 'image/webp', '.bmp': 'image/bmp',
               '.mp4': 'video/mp4', '.wav': 'audio/wav', '.mp3': 'audio/mpeg',
               '.amr': 'audio/amr', '.silk': 'audio/silk'}
# vite 产物里可能出现的东西。认不出的一律当二进制发下去，浏览器自己会拒
ASSET_TYPES = {'.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8',
               '.map': 'application/json', '.svg': 'image/svg+xml', '.woff2': 'font/woff2',
               **MEDIA_TYPES}

# 不回显的字段：表单里留空 = 保持原值
SECRET_FIELDS = frozenset({'deepseek_key', 'planner_key'})

# 面板上的分组。没列进来的字段会被自检抓出来（新加配置不能忘了放进面板）
CONFIG_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ('模型', ('deepseek_key', 'deepseek_model', 'deepseek_url', 'describe_model', 'request_timeout')),
    ('门控（值不值得让模型看一眼）', (
        'reply_necessity_threshold', 'reply_activity_window',
        'backoff_start_after', 'backoff_base_seconds', 'backoff_max_seconds',
        'backoff_bypass_messages')),
    ('专注模式', ('enable_focus', 'focus_enter_activity', 'focus_idle_seconds', 'focus_max_seconds')),
    ('回复行为', ('reply_context_length', 'agent_max_replies', 'focus_score_bonus',
                'quote_replies', 'quote_min_activity', 'quote_window_seconds',
                'reply_min_gap', 'reply_rate_window', 'reply_rate_max')),
    ('人格与表达', ('personality', 'reply_style', 'behavior_style', 'reply_styles', 'style_probability')),
    ('决策者', ('reply_mode', 'enable_planner', 'planner_url', 'planner_key', 'planner_model', 'planner_temperature',
                'planner_max_tokens', 'planner_thinking', 'planner_json_schema', 'enable_tools',
                'planner_rounds')),
    ('出站', ('send_interval', 'typo_rate', 'dry_run')),
    ('图片内联', ('image_inline', 'image_shrink_threshold', 'image_shrink_target',
                'image_fetch_timeout', 'image_cache_dir')),
    ('观测', ('trace_path',)),
    ('改了要重启进程', ('daemon_host', 'daemon_port', 'daemon_unix', 'admin_port', 'log_path', 'log_level',
                    'describe_media', 'media_cache_path', 'describe_concurrency', 'describe_timeout',
                    'describe_max_tokens', 'describe_image_max_tokens', 'describe_history_limit',
                    'profile_path', 'habit_path',
                    'moment_path', 'emoji_path', 'scope_path', 'moment_batch', 'persona_path')),
)

# 面板能改的数值项之范围。越界当场拒——比如上下文填 0 会在模型调用时才炸
RANGES: dict[str, tuple[float, float]] = {
    'agent_max_replies': (1, 10),
    'focus_score_bonus': (0, 100),
    'quote_min_activity': (0, 50),
    'quote_window_seconds': (10, 3600),
    'reply_min_gap': (0.0, 600.0),
    'reply_rate_window': (10.0, 3600.0),
    'reply_rate_max': (1, 50),
    'reply_context_length': (1, 100),      # 上限对齐 ChatWindow.max_history，再大也读不到
    'reply_necessity_threshold': (0, 200),
    'reply_activity_window': (10, 3600),
    'style_probability': (0.0, 1.0),
    'typo_rate': (0.0, 1.0),
    'send_interval': (0.0, 60.0),
    'request_timeout': (1.0, 600.0),
}


_FIELDS = {field.name: field for field in dataclass_fields(Config)}


def _type_name(ftype) -> str:
    return {bool: 'bool', int: 'int', float: 'float', str: 'str'}.get(ftype, 'str')


def _parse(field, raw):
    """ 表单送来的都是字符串（也可能是 JSON 的 bool），按字段类型转。

    留空 = 恢复默认值（面板里把一个输入框清空的直觉就是「回默认」）。
    """
    if field.type is bool:
        return raw if isinstance(raw, bool) else str(raw).strip().lower() not in ('0', 'false', 'no', '')
    text = str(raw).strip()
    if not text:
        if field.default is not MISSING:
            return field.default
        raise ValueError(f'{field.name} 不能为空')
    try:
        return field.type(text)
    except ValueError as e:
        raise ValueError(f'{field.name} 要 {_type_name(field.type)}，收到 {text!r}') from e


def _dotenv_value(value) -> str:
    return ('1' if value else '0') if isinstance(value, bool) else str(value)


def _readable(changed: dict) -> dict:
    """ 给日志和响应看的变更集；密钥不回显内容。 """
    return {name: ('（已更新）' if name in SECRET_FIELDS else str(value))
            for name, value in changed.items()}


def _validate_prompt(text: str) -> None:
    """ 拿假上下文渲染一遍，语法错和变量名拼错都在这儿拦下。

    不验的后果很阴：热重载之后每一轮渲染都抛，而消息处理链把异常吞掉，
    最后表现成「机器人突然不说话」——查起来要翻半天。
    StrictUndefined：默认的 Undefined 会把拼错的变量名渲染成空串，验不出来。
    """
    context = {'behavior_style': '', 'self_name': '', 'contact': '', 'is_group': True,
               'aliases': (), 'tools': (), 'identity': '', 'reply_style': ''}
    try:
        Template(text, undefined=StrictUndefined).render(**context)
    except (TemplateSyntaxError, UndefinedError) as e:
        raise ValueError(f'模板有问题：{e}') from e


def _ratio(part: int, whole: int) -> float | None:
    return None if not whole else round(part / whole, 4)


def _percentile(values: list[int], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))]


def _rates(counts: dict[str, int]) -> dict:
    """ 把状态计数变成面板直接能看的比率。公式就写在这儿，不藏在 UI 里。 """
    received = counts.get(Status.RECEIVED, 0)
    gated = counts.get(Status.GATED, 0) + counts.get(Status.BACKOFF, 0)
    deciding = counts.get(Status.DECIDING, 0)
    failed = (counts.get(Status.PLANNER_FAILED, 0) + counts.get(Status.EXPRESS_FAILED, 0)
              + counts.get(Status.FAILED, 0))
    return {
        'gate_pass': _ratio(received - gated, received),      # 门控放行率
        'planner_silent': _ratio(counts.get(Status.PLANNER_SILENT, 0), deciding),
        'reply': _ratio(counts.get(Status.SENT, 0), received),
        'failed': _ratio(failed, received),
    }


def _quote(content: str, kind) -> dict | None:
    """ 引用消息（appmsg type=57）拆成结构化的块，面板才能画成引用样式。

    取不到就返回 None——调用方保留 render() 给的平铺文本。
    """
    if kind is not MessageKind.APPMSG or tag(content, 'type') != '57':
        return None
    try:
        parsed = parse_reference_message(content)
    except Exception:  # noqa: BLE001 —— XML 不规整时退回平铺文本就好
        return None
    name = tag(content, 'displayname') or parsed['referred_message_sender']
    # 被引的那条可能是图片 / 表情 / 链接：走同一个渲染，否则面板上会显示一段 XML
    return {'content': parsed['content'], 'name': name, 'text': render_referred(parsed)}


def _silk_to_wav(data: bytes, msg_id: str) -> bytes:
    """ 微信语音是 SILK，浏览器放不了，转成 24k 单声道 WAV 并缓存。

    输入是字节（附件归 daemon 管），转码结果落 bot 自己的派生目录。
    pilk 是可选依赖，缺了就让面板回退到文字（外面会包成 500，前端 onerror 接住）。
    """
    wav = DERIVED_MEDIA_DIR / f'{msg_id}.wav'
    if wav.exists():
        return wav.read_bytes()
    DERIVED_MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    silk = DERIVED_MEDIA_DIR / f'{msg_id}.silk'
    silk.write_bytes(data)
    try:
        import pilk  # 只有真要看语音时才需要
        pilk.silk_to_wav(str(silk), str(wav), rate=24000)
        return wav.read_bytes()
    finally:
        silk.unlink(missing_ok=True)


class DecisionLog:
    """ 状态元数据的窄表（`admin.db`）。**只存元数据，不存正文**——正文的事实源是 daemon。

    为什么需要它：放行率 / 沉默率这类统计需要分母，而 `reply_trace` 只记
    「走到模型的那一轮」——门控拦下的、出站失败的都没有。

    append-only（一条事件一行，不按 msg_id 覆盖）：入站消息后面会跟着好几条状态，
    覆盖掉就数不出「收到多少条」了。实测量级：每天 ~400 条入站 × 几次状态 = 几千行。
    ponytail: 同步写，个人量级够用（~ms 级，和 TraceLogger 一致）；卡了再改后台队列批量写。
    """

    def __init__(self, path: str | Path):
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        self._conn.execute('''CREATE TABLE IF NOT EXISTS decision(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            msg_id TEXT NOT NULL, chat_id TEXT, status TEXT NOT NULL, detail TEXT,
            score INTEGER, trigger TEXT, latency_ms INTEGER, ts REAL, origin TEXT)''')
        self._conn.execute('CREATE INDEX IF NOT EXISTS idx_decision_ts ON decision(ts)')
        self._conn.execute('CREATE INDEX IF NOT EXISTS idx_decision_msg ON decision(msg_id)')
        self._conn.commit()

    def write(self, event) -> None:
        """ 总线订户。出站早期的事件（queued/typing）没有身份，不入库——它们是临时的。 """
        if not event.msg_id:
            return
        try:
            with self._lock:
                self._conn.execute(
                    'INSERT INTO decision(msg_id, chat_id, status, detail, score, trigger,'
                    ' latency_ms, ts, origin) VALUES (?,?,?,?,?,?,?,?,?)',
                    (event.msg_id, event.chat_id, event.status, event.detail, event.score,
                     event.trigger, event.latency_ms, event.ts, event.origin))
                self._conn.commit()
        except sqlite3.Error as e:
            logger.warning(f'状态落库失败: {e}')

    def counts(self, since: float) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                'SELECT status, COUNT(*) FROM decision WHERE ts >= ? GROUP BY status',
                (since,)).fetchall()
        return {status: n for status, n in rows}

    def close(self) -> None:
        self._conn.close()


class Admin:
    def __init__(self, runtime, host: str = '127.0.0.1', port: int = DEFAULT_PORT,
                 sidecar: str | Path = SIDECAR):
        self.runtime = runtime
        self.host = host
        self.port = port
        self.dotenv = DOTENV_PATH      # 自检里会换成临时文件
        self.decisions = DecisionLog(sidecar)
        # 面板一次刷一堆图：daemon 那边原图是串行下的（一次一张），这里限的是并发
        # 占用的 HTTP 线程数，免得附件把面板的其它请求一起拖住
        self._media_gate = threading.Semaphore(MEDIA_CONCURRENCY)
        self.server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @classmethod
    def start_safely(cls, runtime, host: str = '127.0.0.1',
                     port: int = DEFAULT_PORT) -> Admin | None:
        """ 起后台。失败只打日志返回 None——管理后台绝不能把 bot 拖下水。 """
        try:
            panel = cls(runtime, host, port)
            panel.start()
            return panel
        except Exception as e:  # noqa: BLE001
            logger.warning(f'管理后台启动失败（{host}:{port}），bot 继续跑: {e}')
            return None

    def start(self) -> None:
        handler = type('_BoundHandler', (_Handler,), {'admin': self})
        self.server = ThreadingHTTPServer((self.host, self.port), handler)
        bus.subscribe(self.decisions.write)      # 绑端口成功了才订，免得白记
        self._thread = threading.Thread(target=self.server.serve_forever,
                                        name='admin', daemon=True)
        self._thread.start()
        logger.info(f'管理后台: http://{self.host}:{self.port}')

    def stop(self) -> None:
        bus.unsubscribe(self.decisions.write)
        self.decisions.close()
        if self.server:
            self.server.shutdown()
            self.server.server_close()

    def call(self, coro, timeout: float = RPC_TIMEOUT):
        """ 从 HTTP 线程切回 bot 的事件循环执行一个协程。 """
        loop = self.runtime.loop
        if loop is None:
            raise RuntimeError('bot 的事件循环还没起来')
        return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout)

    @property
    def _wechat(self):
        return self.runtime.bot.wechat

    # ---------- 只读数据 ----------

    def state(self) -> dict:
        groups: dict[str, int] = {}
        for event in bus.since(0):
            group = Status.group(event.status)
            groups[group] = groups.get(group, 0) + 1
        bot = self.runtime.bot
        return {
            'wxid': getattr(self._wechat, 'wxid', ''),
            'nickname': getattr(self._wechat, 'nickname', ''),
            'dry_run': bool(getattr(bot, 'dry_run', False)),
            'seq': bus.latest_seq(),
            'groups': groups,
            'scope': self.runtime.reply_scope.to_dict(),
        }

    def health(self) -> dict:
        """ 健康状态卡。daemon 那项要走一次 RPC，可能慢——所以单开一个接口。 """
        runtime = self.runtime
        now = time.time()
        daemon: dict = {'ok': False}
        try:
            status = self.call(self._wechat.status(), timeout=5.0)
            daemon = {'ok': True, 'logged_in': bool(status.get('logged_in')),
                     'wxid': status.get('wxid', ''), 'nickname': status.get('nickname', ''),
                     'uin': status.get('uin'), 'status': status.get('status')}
        except Exception as e:  # noqa: BLE001 —— daemon 挂了也要能把面板打开
            # 带上类型名：`str(TimeoutError())` 是空串，面板就会显示“daemon 取不到状态”却什么都
            # 说不出来——那正是最难查的一种失败
            daemon['error'] = f'{type(e).__name__}: {e}'

        focus = [{'chat': chat, 'rounds': session.rounds, 'reason': session.reason,
                  'idle_left': max(0.0, runtime.focus.idle - (now - session.last_message)),
                  'max_left': max(0.0, runtime.focus.maximum - (now - session.started))}
                 for chat, session in runtime.focus.sessions.items()]
        bot = runtime.bot
        return {
            'daemon': daemon,
            'focus': focus,
            'focus_counts': {'entered': runtime.focus.entered, 'exited': runtime.focus.exited},
            'backoff': {chat: round(left, 1) for chat, left in runtime.backoff.pending(now).items()},
            'speech': runtime.speech.counts(now),
            'outbox': bot.outbox.depth(),
            'window': bot.history.sizes(),
            'emojis': {'total': len(runtime.emojis), 'sendable': runtime.emojis.sendable_count,
                       'scene': runtime.emojis.scene_count},
            'moments': len(runtime.memories),
            'counters': {'reply': bot.reply_count, 'emoji': bot.emoji_count,
                         'typo': bot.typo_count},
            'uptime': round(now - _STARTED_AT, 1),
        }

    # ---------- 登录 ----------

    def login_qr(self) -> dict:
        """ 面板扫码登录第一步：取二维码。png 转 base64，面板直接塞进 data: URL。 """
        qr = self.call(self._wechat.login_qr(), timeout=LOGIN_TIMEOUT)
        return {'uuid': qr.uuid, 'url': qr.url, 'expires_in': qr.expires_in,
                'png': base64.b64encode(qr.png).decode() if qr.png else ''}

    def login_poll(self) -> dict:
        """ 轮询一次扫码状态。daemon 报「没有进行中的登录」（重启 / 流程结束）不是故障。 """
        try:
            return self.call(self._wechat.login_qr_poll())
        except Exception as e:  # noqa: BLE001
            return {'status': 'idle', 'error': str(e)}

    def login_cancel(self) -> dict:
        try:
            return self.call(self._wechat.login_qr_cancel())
        except Exception as e:  # noqa: BLE001
            return {'status': 'idle', 'error': str(e)}

    def logout(self) -> dict:
        """ 退出登录：daemon 注销并停长链。bot 本身不重启，等下次扫码。 """
        return self.call(self._wechat.logout())

    def chats(self) -> list[dict]:
        out = []
        # 启动竞态专用短超时：daemon 还没 load_context 时这个 RPC 会一直挂到 15s，
        # 让首次打开的面板白屏 15 秒。正常一次也就几百毫秒
        for chat in self.call(self._wechat.conversations(limit=CHATS_LIMIT), timeout=5.0):
            peer = getattr(chat, 'peer', '')
            last = getattr(chat, 'last_message', None) or {}
            name = getattr(chat, 'display_name', '') or peer
            # 预览也得走 render()：last_message 的 clean_text 对媒体/系统消息就是原始 XML，
            # 以前直接显示 `<?xml version="1.0"?><msg><img …>`，列表里没法看
            preview, _ = render(last.get('msg_type', 0), last.get('content', ''),
                                last.get('clean_text') or '')
            out.append({
                'chat_id': peer,
                'name': name,
                'is_group': bool(getattr(chat, 'is_chatroom', False)),
                'count': getattr(chat, 'msg_count', 0),
                'updated_at': getattr(chat, 'updated_at', 0),
                'last': preview[:60],
                # 静音和专注都按**显示名**记（插件链就是按显示名分发的）
                'allowed': self.runtime.reply_scope.allows(name),
                'focus': name in self.runtime.focus.sessions,
            })
        return out

    def messages(self, chat: str, limit: int = MESSAGES_LIMIT,
                 before: int = 0, after: int = 0) -> dict:
        """ 会话历史（分页）。`before`/`after` 是 daemon 的 `messages.id` 游标。

        返回 {'messages': [...], 'has_more': bool}。has_more 靠「满页 = 可能还有」近似，
        daemon 的 get_messages 不把 has_more 透给 SDK——测到不够再给 SDK 加。
        """
        if not chat:
            return {'messages': [], 'has_more': False}
        raw = self.call(self._wechat.messages(chat, limit=limit, before_id=before, after_id=after,
                                              exclude_system=False))
        # daemon 的 sender_display_name 查不到时会拿 wxid 当名字，那不是名字。
        # 只对真正拿不到的那几个查（查到的会缓存），不是每条都查
        unknown = [m.sender for m in raw
                   if not m.is_self and m.sender and m.sender_display_name == m.sender]
        # 拍一拍也走 10002，被拍的那一方同样只有一个 wxid，面板要显示名字
        patted = [p.patted_user for p in (getattr(m, 'pat', None) for m in raw)
                  if p is not None and p.patted_user]
        names = self.call(self._wechat.sender_names(chat, [*unknown, *patted])) \
            if unknown or patted else {}
        rows = [row for row in (self._row(msg, names) for msg in raw) if row]
        return {'messages': rows, 'has_more': len(raw) >= limit}

    def _row(self, msg, names: dict[str, str] | None = None) -> dict | None:
        kind = MessageKind.of(msg.msg_type)
        pat = getattr(msg, 'pat', None)
        # 设备同步(51)/系统消息不该进聊天记录。daemon 的 exclude_system 只排 10000/10002，
        # 所以取的时候不排、在这里自己滤；拍一拍虽然也是 10002，但那是社交信号，留下
        if (kind in SYSTEM_KINDS and pat is None) or kind is MessageKind.OTHER:
            return None
        if pat is not None:
            text, media = self._render_pat(msg, names), None
        else:
            text, media = render(msg.msg_type, msg.content, msg.text)
        if not text:
            return None
        if media is not None and not media.description and media.md5 and self.runtime.media_cache:
            cached = self.runtime.media_cache.get(media.md5)
            if cached:
                media = replace(media, description=cached)
                text = media.placeholder

        # 引用拆成结构化的块，面板才能画成引用样式而不是一整句平铺文本
        quote = _quote(msg.content, kind)
        if quote:
            text = quote['content']

        own = bus.status_of(msg.msg_id)
        reply = bus.progress_of(msg.msg_id)
        return {
            'cursor': (msg.raw_data or {}).get('id'),      # daemon 自增 id，翻页用
            'msg_id': str(msg.msg_id),                     # 身份键，字符串
            'ts': msg.create_time,
            'sender': ('我' if msg.is_self else '') or (names or {}).get(msg.sender) \
                      or msg.sender_display_name or msg.sender,
            'outgoing': bool(msg.is_self),
            'text': text,
            # 原始载荷（XML / 带 wxid 前缀的正文）：详情抽屉里要看它，渲染层丢掉的信息全在这
            'xml': msg.content,
            'status': own.status if own else '',
            'status_detail': own.detail if own else '',
            'score': own.score if own else 0,
            'reply_status': reply.status if reply else '',
            'reply_detail': reply.detail if reply else '',
            'dry_text': reply.text if reply and reply.status == Status.DRY_RUN else '',
            # 附件：有就带上，面板按 kind 决定画 <img> / <audio> / 还是只留文字
            'media': None if media is None else {
                'kind': media.kind, 'md5': media.md5, 'description': media.description or '',
                'duration': media.duration, 'size': media.size},
            'quote': quote,
        }

    def _render_pat(self, msg, names: dict[str, str] | None) -> str:
        """ 拍一拍 → 面板里的一行（与 `Bot._render_pat` 同一口径）。 """
        pat = msg.pat
        if pat.patted_user == self.runtime.bot.wxid:
            patted = '你'
        else:
            patted = (names or {}).get(pat.patted_user) or pat.patted_user
        return f'拍了拍 {patted}{pat.suffix or ""}'

    def media(self, msg_id: str) -> tuple[bytes, str]:
        """ 取一条消息的附件字节，返回 (data, content-type)。

        图片由 daemon 取**原图**（CDN 那个 blob 只是缩略图，原图只能走 getmsgimg），
        其它附件按 CDN 那条路；下载和保管都在 daemon（磁盘缓存 `img_<id>.<ext>` /
        `download_<id>.<ext>`），bot 只按 msg_id 要字节，不关心它存在哪。
        """
        if not msg_id.isdigit():
            raise ValueError(f'msg_id 必须是 daemon 的整数 id：{msg_id!r}')
        with self._media_gate:
            data, ext = self.call(self._wechat.media_bytes_by_id(int(msg_id)), timeout=MEDIA_TIMEOUT)
        ext = ext.lower()
        if ext == 'silk':  # 微信语音是 SILK，浏览器放不了，转成 WAV 再吐
            data, ext = _silk_to_wav(data, msg_id), 'wav'
        return data, MEDIA_TYPES.get(f'.{ext}', 'application/octet-stream')

    def log_tail(self, tail: int = 200, only: str = '') -> list[str]:
        """ 日志尾巴。loguru 一直在写，面板里看得到就不必 SSH。 """
        path = Path(self.runtime.config.log_path)
        if not path.exists():
            return []
        with path.open('rb') as file:
            file.seek(0, 2)
            size = file.tell()
            file.seek(max(0, size - LOG_TAIL_BYTES))
            text = file.read().decode('utf-8', errors='replace')
        lines = text.splitlines()[-max(1, tail):]
        if only:
            keys = [word.strip().upper() for word in only.split(',') if word.strip()]
            lines = [line for line in lines if any(f'| {key}' in line or f'| {key:<8}' in line
                                                   for key in keys)]
        return lines

    def trace(self, msg_id: str) -> dict:
        """ 「为什么没回」抽屉：门控理由（总线）+ 这一轮的模型轨迹（trace 文件）。 """
        own = bus.status_of(msg_id)
        reply = bus.progress_of(msg_id)
        return {
            'msg_id': str(msg_id),
            'event': own.to_dict() if own else None,
            'reply': reply.to_dict() if reply else None,
            'trace': find_trace(self.runtime.config.trace_path, msg_id),
        }

    # ---------- 统计 ----------

    def stats(self, hours: float = 24.0) -> dict:
        """ 统计看板。计数的分母来自 sidecar，被点名延迟来自 trace 文件。 """
        since = time.time() - max(0.1, hours) * 3600
        counts = self.decisions.counts(since)
        latencies = trace_latencies(self.runtime.config.trace_path)
        bot = self.runtime.bot
        return {
            'hours': hours,
            'counts': counts,
            'rates': _rates(counts),
            'latency': {'p50': _percentile(latencies, 0.5), 'p95': _percentile(latencies, 0.95),
                        'n': len(latencies)},
            'focus': {'entered': self.runtime.focus.entered, 'exited': self.runtime.focus.exited},
            'counters': {'reply': bot.reply_count, 'emoji': bot.emoji_count, 'typo': bot.typo_count},
            'usage': usage.snapshot(),
        }

    # ---------- 提示词 / 重放 / 资料 ----------

    def read_prompts(self) -> dict:
        return {name: path.read_text(encoding='utf-8') for name, path in PROMPTS.items()}

    def save_prompt(self, name: str, text: str) -> dict:
        """ 校验 → 原子写 → 热重载（ChatPlugin 在 __init__ 里读模板，重建就生效）。 """
        path = PROMPTS.get(name)
        if path is None:
            raise ValueError(f'没有这个提示词：{name}')
        _validate_prompt(text)
        tmp = path.parent / f'{path.name}.tmp'
        tmp.write_text(text, encoding='utf-8')
        os.replace(tmp, path)
        self.runtime.apply(self.runtime.config)
        logger.warning(f'管理后台改了提示词 {path.name}（{len(text)} 字节），已热重载')
        return {'saved': name, 'bytes': len(text), 'lines': text.count('\n') + 1}

    # ---------- 分会话人设 ----------

    def read_persona(self, chat: str = '') -> dict:
        """ 该会话的人设：全局那份当参照，overrides 是本会话盖上去的（空 = 全部继承）。 """
        persona = self.runtime.persona
        return {
            'chat': chat,
            'fields': list(OVERRIDE_FIELDS),
            'global': {'personality': persona.personality, 'reply_style': persona.reply_style,
                       'behavior_style': persona.behavior_style,
                       'reply_styles': '|'.join(persona.alternates),
                       'style_probability': persona.alternate_probability,
                       'aliases': list(persona.aliases)},
            'overrides': self.runtime.personas.get(chat) if chat else {},
        }

    def write_persona(self, chat: str, overrides: dict) -> dict:
        """ 整份替换这个会话的覆盖；空值字段 = 继承全局。下一轮直接生效（不重建插件）。 """
        saved = self.runtime.personas.set(chat, overrides or {})
        logger.warning(f'管理后台改了分会话人设 {chat!r}：{sorted(saved) or "（清空，回全局）"}')
        return {'chat': chat, 'overrides': saved}

    def replay(self, msg_id: str, stage: str = 'all') -> dict:
        """ 用当前提示词重跑一轮。会真的花 token（面板上要确认）。 """
        record = find_trace(self.runtime.config.trace_path, msg_id)
        if not record:
            raise ValueError('这一轮没有 trace——被门控拦下的没走到模型，重放不了')
        return self.call(self.runtime.chat_plugin.replay(
            record.get('transcript', ''), record.get('plan', ''),
            self_name=getattr(self._wechat, 'nickname', ''),
            contact=record.get('contact_name', ''), stage=stage))

    def emojis(self, limit: int = 300) -> list[dict]:
        return [{'md5': item.md5, 'desc': item.desc, 'scene': item.scene,
                 'count': item.count, 'sendable': item.sendable}
                for item in self.runtime.emojis.items()[:limit]]

    def delete_emoji(self, md5: str) -> dict:
        removed = self.runtime.emojis.remove(md5)
        if removed:
            self.runtime.bot.save_emojis()
        return {'removed': removed}

    def moments(self, chat: str = '', limit: int = 100) -> list[dict]:
        return self.runtime.memories.list(chat, limit)

    def delete_moment(self, moment_id: int) -> dict:
        return {'removed': self.runtime.memories.delete(int(moment_id))}

    # ---------- 动作 ----------

    def set_dry_run(self, on: bool) -> dict:
        """ 演习开关。两个对象的开关都要改：Bot 决定写不写历史 / 发不发事件，
        Wechat 决定真发还是只打日志。 """
        self.runtime.bot.dry_run = bool(on)
        self._wechat.dry_run = bool(on)
        self.runtime.config = replace(self.runtime.config, dry_run=bool(on))
        logger.warning(f'管理后台{"打开" if on else "关闭"}了演习模式')
        return {'dry_run': bool(on)}

    def send(self, chat: str, text: str) -> dict:
        """ 手动发一条。走 OutboundQueue，不绕开限速和拟人延迟。 """
        if not (chat and text.strip()):
            raise ValueError('chat 和 text 都不能为空')
        bot = self.runtime.bot
        self.call(self._send_manual(chat, text.strip()))
        return {'queued': (chat, text.strip()), 'outbox': bot.outbox.depth()}

    async def _send_manual(self, chat: str, text: str) -> None:
        sender = await self._wechat.get_display_name(self._wechat.wxid, chat)
        # 走 Bot.enqueue：和机器人自己回复一样先写历史，模型才知道「我」说过这句
        self.runtime.bot.enqueue(chat, text, sender=sender, chat_name=chat)

    def set_focus(self, chat: str, on: bool) -> dict:
        """ 手动进/出专注。手动进出不进冷却（FocusMode.enter/exit 本来就支持）。 """
        if not chat:
            raise ValueError('chat 不能为空')
        if on:
            self.runtime.focus.enter(chat, time.time(), '管理后台手动')
        else:
            self.runtime.focus.exit(chat, time.time(), '管理后台手动', cooldown=False)
        return {'focus': sorted(self.runtime.focus.sessions)}

    def set_allowed(self, chat: str, on: bool) -> dict:
        """ 面板上的「允许回复」开关：改名单 + 落盘 + 立刻重装插件链。 """
        if not chat:
            raise ValueError('chat 不能为空')
        allowed = self.runtime.reply_scope.set_allowed(chat, bool(on))
        self.runtime.install_scope()
        return {'allowed': allowed}

    def set_scope_mode(self, mode: str) -> dict:
        """ 'all' = 除静音的之外都回；'allow' = 只回勾选的。 """
        selected = self.runtime.reply_scope.set_mode(mode)
        self.runtime.install_scope()
        return {'mode': selected, **self.runtime.reply_scope.to_dict()}

    # ---------- 配置 ----------

    def read_config(self) -> dict:
        """ 面板要的分组表单。密钥不回显，只告诉面板「设过没有」。 """
        config = self.runtime.config
        groups = []
        for title, names in CONFIG_GROUPS:
            items = []
            for name in names:
                field = _FIELDS[name]
                value = getattr(config, name)
                items.append({
                    'name': name,
                    'type': _type_name(field.type),
                    'value': '' if name in SECRET_FIELDS else value,
                    'secret': name in SECRET_FIELDS,
                    'isset': bool(value) if name in SECRET_FIELDS else True,
                    'default': field.default if field.default is not MISSING else None,
                    'hot': name in HOT_FIELDS,
                })
            groups.append({'group': title, 'items': items})
        return {'groups': groups, 'dotenv': str(self.dotenv)}

    def write_config(self, updates: dict) -> dict:
        """ 校验 → 写回 .env → 热字段**立刻生效**。

        校验不过一个字都不写（一个笔误不能让 bot 下次起不来）。热字段当场 apply，
        面板不用再点一次「热重载」；只有要整进程重启的字段才留在 stale 里。
        """
        updates = {str(key).strip().lower(): value for key, value in updates.items()}
        unknown = sorted(set(updates) - set(_FIELDS))
        if unknown:
            raise ValueError(f'不认识的配置项：{", ".join(unknown)}')

        current = {f.name: getattr(self.runtime.config, f.name) for f in dataclass_fields(Config)}
        candidate = dict(current)
        for name, raw in updates.items():
            if name in SECRET_FIELDS and not str(raw).strip():
                continue                       # 留空 = 保持原值
            candidate[name] = _parse(_FIELDS[name], raw)
            low, high = RANGES.get(name, (None, None))
            if low is not None and not low <= candidate[name] <= high:
                raise ValueError(f'{name} 要在 {low}~{high} 之间，收到 {candidate[name]}')

        try:
            config = Config(**candidate)       # 先验一遍再落盘
        except (TypeError, ValueError) as e:
            raise ValueError(f'配置不合法：{e}') from e

        changed = {name: candidate[name] for name in updates if candidate[name] != current[name]}
        if not changed:
            return {'changed': {}, 'stale': []}
        update_dotenv(self.dotenv, {name.upper(): _dotenv_value(value)
                                   for name, value in changed.items()})
        stale = self.runtime.apply(config)
        logger.warning(f'管理后台改了配置 {_readable(changed)}；'
                       f'需要重启进程的：{stale or "（无）"}')
        return {'changed': _readable(changed), 'stale': stale}

    def reload_config(self) -> dict:
        """ 重新读 .env 并热重载。

        override=True：面板上改的就是最终值。跟启动时的「真实环境变量优先」相反，
        但只能这样——不然在面板上改了不生效。
        """
        before = self.runtime.config
        load_dotenv(self.dotenv, override=True)
        config = Config.from_env()
        changed = {f.name: getattr(config, f.name) for f in dataclass_fields(Config)
                   if getattr(config, f.name) != getattr(before, f.name)
                   and f.name not in SECRET_FIELDS}
        stale = self.runtime.apply(config)
        logger.warning(f'管理后台热重载：改了 {_readable(changed) or "（无）"}；'
                       f'需要重启进程的：{stale or "（无）"}')
        return {'changed': _readable(changed), 'stale': stale}

    def restart(self) -> dict:
        """ 整进程重启。先把要停的停掉，再从循环退出后的回调里 execv。 """
        self.runtime.restart_process()
        return {'restarting': True}

    # ---------- HTTP ----------

    def handle_get(self, handler: BaseHTTPRequestHandler) -> None:
        url = urlparse(handler.path)
        query = parse_qs(url.query)
        route = url.path
        try:
            if route in ('/', '/index.html'):
                return self._page(handler)
            if route.startswith('/assets/'):
                return self._asset(handler, route)
            if route == '/api/state':
                return self._json(handler, self.state())
            if route == '/api/health':
                return self._json(handler, self.health())
            if route == '/api/login':
                return self._json(handler, self.login_poll())
            if route == '/api/chats':
                return self._json(handler, {'chats': self.chats()})
            if route == '/api/messages':
                chat = (query.get('chat') or [''])[0]
                limit = int((query.get('limit') or [MESSAGES_LIMIT])[0])
                before = int((query.get('before') or ['0'])[0])
                after = int((query.get('after') or ['0'])[0])
                return self._json(handler, self.messages(chat, limit, before, after))
            if route == '/api/events':
                since = int((query.get('since') or ['0'])[0])
                return self._json(handler, {'seq': bus.latest_seq(),
                                            'events': [e.to_dict() for e in bus.since(since)]})
            if route == '/api/log':
                tail = int((query.get('tail') or ['200'])[0])
                only = (query.get('only') or [''])[0]
                return self._json(handler, {'lines': self.log_tail(tail, only)})
            if route == '/api/stats':
                hours = float((query.get('hours') or ['24'])[0])
                return self._json(handler, self.stats(hours))
            if route == '/api/prompts':
                return self._json(handler, self.read_prompts())
            if route == '/api/persona':
                return self._json(handler, self.read_persona((query.get('chat') or [''])[0]))
            if route == '/api/emojis':
                return self._json(handler, {'emojis': self.emojis()})
            if route == '/api/moments':
                chat = (query.get('chat') or [''])[0]
                return self._json(handler, {'moments': self.moments(chat)})
            if route.startswith('/api/trace/'):
                return self._json(handler, self.trace(unquote(route[len('/api/trace/'):])))
            if route == '/api/media':
                data, content_type = self.media((query.get('msg_id') or [''])[0])
                return self._send_bytes(handler, data, content_type,
                                       cache='private, max-age=300')
            if route == '/api/config':
                return self._json(handler, self.read_config())
            self._json(handler, {'error': 'not found'}, 404)
        except Exception as e:  # noqa: BLE001 —— 后台出错不能冒泡到 bot
            logger.warning(f'管理后台 GET {route} 出错: {e}')
            self._json(handler, {'error': str(e)}, 500)

    def handle_post(self, handler: BaseHTTPRequestHandler) -> None:
        url = urlparse(handler.path)
        route = url.path
        try:
            length = int(handler.headers.get('Content-Length') or 0)
            payload = json.loads(handler.rfile.read(length) or b'{}')
        except ValueError as e:
            return self._json(handler, {'error': f'请求体不是 JSON: {e}'}, 400)

        try:
            if route == '/api/dry_run':
                return self._json(handler, self.set_dry_run(bool(payload.get('on'))))
            if route == '/api/login':
                return self._json(handler, self.login_qr())
            if route == '/api/login_cancel':
                return self._json(handler, self.login_cancel())
            if route == '/api/logout':
                return self._json(handler, self.logout())
            if route == '/api/send':
                return self._json(handler, self.send(payload.get('chat', ''), payload.get('text', '')))
            if route == '/api/focus':
                return self._json(handler, self.set_focus(payload.get('chat', ''),
                                                          bool(payload.get('on'))))
            if route == '/api/allow':
                return self._json(handler, self.set_allowed(payload.get('chat', ''),
                                                            bool(payload.get('on'))))
            if route == '/api/scope':
                return self._json(handler, self.set_scope_mode(payload.get('mode', '')))
            if route == '/api/config':
                return self._json(handler, self.write_config(payload.get('updates') or {}))
            if route == '/api/reload':
                return self._json(handler, self.reload_config())
            if route == '/api/restart':
                return self._json(handler, self.restart())
            if route == '/api/prompts':
                return self._json(handler, self.save_prompt(payload.get('name', ''),
                                                            payload.get('text', '')))
            if route == '/api/persona':
                return self._json(handler, self.write_persona(payload.get('chat', ''),
                                                              payload.get('overrides') or {}))
            if route == '/api/emoji_delete':
                return self._json(handler, self.delete_emoji(payload.get('md5', '')))
            if route == '/api/moment_delete':
                return self._json(handler, self.delete_moment(payload.get('id', 0)))
            if route.startswith('/api/replay/'):
                msg_id = unquote(route[len('/api/replay/'):])
                return self._json(handler, self.replay(msg_id, payload.get('stage', 'all')))
            self._json(handler, {'error': 'not found'}, 404)
        except ValueError as e:
            self._json(handler, {'error': str(e)}, 400)
        except Exception as e:  # noqa: BLE001
            logger.warning(f'管理后台 POST {route} 出错: {e}')
            self._json(handler, {'error': str(e)}, 500)

    def _page(self, handler) -> None:
        """SPA 的壳。产物没构建就说清楚该跑什么——空白页最难查"""
        page = DIST / 'index.html'
        if not page.exists():
            return self._send(handler, BUILD_HINT, 'text/plain; charset=utf-8', 503)
        self._send(handler, page.read_text(encoding='utf-8'), 'text/html; charset=utf-8')

    @staticmethod
    def _asset(handler, route: str) -> None:
        """vite 的产物：文件名带内容 hash，所以可以长期缓存。

        路径解析后必须仍然落在 dist/assets 里——不然 `/assets/../../.env` 就把仓库读穿了。
        """
        path = (ASSETS / route[len('/assets/'):]).resolve()
        if not path.is_relative_to(ASSETS.resolve()) or not path.is_file():
            return Admin._json(handler, {'error': 'not found'}, 404)
        content_type = ASSET_TYPES.get(path.suffix.lower(), 'application/octet-stream')
        Admin._send_bytes(handler, path.read_bytes(), content_type,
                          cache='public, max-age=31536000, immutable')

    @staticmethod
    def _send(handler, body: str, content_type: str, code: int = 200) -> None:
        Admin._send_bytes(handler, body.encode('utf-8'), content_type, code)

    @staticmethod
    def _send_bytes(handler, data: bytes, content_type: str, code: int = 200,
                    cache: str = 'no-store') -> None:
        handler.send_response(code)
        handler.send_header('Content-Type', content_type)
        handler.send_header('Content-Length', str(len(data)))
        # 默认不缓存：页面和 /api/* 都是「看着会变」的东西，缓存一小时会让面板显示旧数据/旧代码。
        # 附件例外（一模几百 KB，滚回去别重下），但**不能**按内容寻址看：同一个 msg_id 给什么
        # 字节是会变的——原图被服务端回收后就退回缩略图。所以只缓存 5 分钟，让这种切换
        # 几分钟内自愈，而不是拖一小时。vite 带 hash 的产物是唯一能长期缓存的东西
        handler.send_header('Cache-Control', cache)
        handler.end_headers()
        handler.wfile.write(data)

    @staticmethod
    def _json(handler, payload, code: int = 200) -> None:
        Admin._send(handler, json.dumps(payload, ensure_ascii=False, default=str),
                    'application/json', code)


class _Handler(BaseHTTPRequestHandler):
    admin: Admin
    server_version = 'bot-admin/0.1'

    def do_GET(self) -> None:  # noqa: N802 —— BaseHTTPRequestHandler 的命名
        self.admin.handle_get(self)

    def do_POST(self) -> None:  # noqa: N802
        self.admin.handle_post(self)

    def log_message(self, fmt: str, *args) -> None:
        logger.debug(f'admin {self.address_string()} {fmt % args}')


_STARTED_AT = time.time()


def _self_check() -> None:
    """ 不连 daemon 的自检：拿假 runtime 把「渲染 + 贴状态 + 路由」这条链走通。 """
    import tempfile
    import types

    from persona import Persona
    from personas import PersonaStore

    bus.reset()
    now = time.time()

    class FakeFocus:
        idle, maximum = 300.0, 1800.0
        entered, exited = 2, 1

        def __init__(self):
            self.sessions = {'测试群': types.SimpleNamespace(
                rounds=3, reason='活跃度 16 条', started=now - 100, last_message=now - 10)}

        def enter(self, chat, _now, reason=''):
            self.sessions[chat] = types.SimpleNamespace(rounds=0, reason=reason,
                                                        started=_now, last_message=_now)

        def exit(self, chat, _now, reason, cooldown=True):  # noqa: ARG002
            self.sessions.pop(chat, None)

    class FakeBackoff:
        def pending(self, _now):
            return {'测试群': 12.0}

    class FakeSpeech:
        def counts(self, _now):
            return {'测试群': 2}

    class FakeEmojis:
        def __len__(self): return 10
        sendable_count, scene_count = 4, 2

    class FakeMemories:
        def __len__(self): return 7

    class FakeOutbox:
        def depth(self): return 0

    class FakeHistory:
        def sizes(self): return {'456@chatroom': 30}

    # 名单文件是上一轮自检留下的就清掉，不然结果取决于跑过几次
    (Path(tempfile.gettempdir()) / 'bot-admin-selfcheck-scope.json').unlink(missing_ok=True)
    (Path(tempfile.gettempdir()) / 'bot-admin-selfcheck-personas.json').unlink(missing_ok=True)
    runtime = types.SimpleNamespace(
        loop=None, media_cache=None,
        config=Config(log_path='/no/such/bot.log', trace_path='/no/such/trace.jsonl'),
        persona=Persona(),
        personas=PersonaStore(Path(tempfile.gettempdir()) / 'bot-admin-selfcheck-personas.json'),
        reply_scope=ReplyScope(Path(tempfile.gettempdir()) / 'bot-admin-selfcheck-scope.json',
                               seed=('研究生摆烂群',)),
        focus=FakeFocus(), backoff=FakeBackoff(), speech=FakeSpeech(), emojis=FakeEmojis(), memories=FakeMemories(),
        bot=types.SimpleNamespace(
            dry_run=False, typo_rate=0.08, reply_count=3, emoji_count=1, typo_count=0,
            wxid='wxid_bot',
            outbox=FakeOutbox(), history=FakeHistory(),
            is_muted=lambda name: name == '测试群',
            wechat=types.SimpleNamespace(wxid='wxid_bot', nickname='Bot', dry_run=False)))

    admin = Admin(runtime, sidecar=Path(tempfile.gettempdir()) / 'bot-admin-selfcheck.db')
    admin.decisions._conn.execute('DELETE FROM decision')      # 干净起点

    # apply 的替身：真 apply 在 test_runtime.py 里测；这里只记下落盘后生效的配置
    applied: list[Config] = []

    def fake_apply(cfg: Config) -> list[str]:
        stale = [f.name for f in dataclass_fields(Config)
                 if f.name in RESTART_FIELDS and getattr(cfg, f.name) != getattr(runtime.config, f.name)]
        runtime.config = cfg
        applied.append(cfg)
        return stale

    runtime.apply = fake_apply

    def msg(msg_type, content, clean='', is_self=False, msg_id=9202842881785760589):
        return types.SimpleNamespace(
            msg_id=msg_id, msg_type=msg_type, content=content, clean_text=clean,
            text=clean or content, create_time=1700000000, is_chatroom=True,
            chatroom_sender='wxid_a' if not is_self else 'wxid_bot',
            sender_display_name='张三', sender='wxid_a', is_self=is_self,
            raw_data={'id': 12345})

    # 普通文本：状态从总线上贴过来；64 位 msg_id 必须是字符串
    bus.publish(Status.GATED, msg_id=9202842881785760589, detail='门控 45/60', score=45)
    row = admin._row(msg(1, '这个怎么弄啊', '这个怎么弄啊'))
    assert row['msg_id'] == '9202842881785760589' and isinstance(row['msg_id'], str), row
    assert row['status'] == Status.GATED and row['score'] == 45
    assert row['cursor'] == 12345 and not row['outgoing'] and row['sender'] == '张三'

    # 回复进度挂在被回复的那条上
    bus.publish(Status.SENT, chat_id='g', reply_to=9202842881785760589, origin='bot', msg_id=7)
    assert admin._row(msg(1, 'hi'))['reply_status'] == Status.SENT

    # 自己的消息
    own = admin._row(msg(1, '我说一句', is_self=True))
    assert own['outgoing'] and own['sender'] == '我'

    # 51（设备同步）绝不能出现在聊天记录里
    assert admin._row(msg(51, '<msg><op id="5"/></msg>')) is None
    assert admin._row(msg(10000, '系统消息')) is None
    assert admin._row(msg(34, 'x')) is not None          # 语音有占位符，要显示

    # 拍一拍也是 10002，但是社交信号：要留下，而且把 wxid 渲染成人话
    patted = msg(10002, '<sysmsg type="pat">')
    patted.pat = types.SimpleNamespace(patted_user='wxid_other', suffix='的脑袋')
    assert admin._row(patted, {'wxid_other': '李四'})['text'] == '拍了拍 李四的脑袋'
    own_pat = msg(10002, '<sysmsg type="pat">', is_self=True)
    own_pat.pat = types.SimpleNamespace(patted_user='wxid_bot', suffix='')
    assert admin._row(own_pat, {})['text'] == '拍了拍 你'

    # 表情 / 图片走占位符；有缓存描述时要补上
    emoji = admin._row(msg(47, '<msg><emoji md5="m" desc="ChEKB2RlZmF1bHQSBuWVp+WVpw==" /></msg>'))
    assert emoji['text'] == '[表情：啧啧]', emoji

    assert emoji['media'] == {'kind': 'emoji', 'md5': 'm', 'description': '啧啧',
                              'duration': 0, 'size': 0}, emoji
    assert admin._row(msg(1, 'hi'))['media'] is None

    class FakeCache:
        def get(self, md5):
            return '一只橘猫' if md5 == 'f9bf' else None

    runtime.media_cache = FakeCache()
    image = admin._row(msg(3, '<msg><img md5="f9bf" /></msg>'))
    assert image['text'] == '[图片：一只橘猫]', image
    assert image['media'] == {'kind': 'image', 'md5': 'f9bf', 'description': '一只橘猫',
                              'duration': 0, 'size': 0}, image

    # 引用：拆成「回复内容 + 引用块」，而不是一整句平铺文本
    quoted = admin._row(msg(49,
        '<msg><appmsg><title>直接用这个点进去对比</title><type>57</type>'
        '<fromusername>wxid_sender</fromusername><refermsg><type>49</type>'
        '<chatusr>wxid_kf7zbyjjxs8r22</chatusr><displayname>zzq</displayname>'
        '<content>&lt;msg&gt;&lt;appmsg&gt;&lt;title&gt;和我一起测测&lt;/title&gt;'
        '&lt;/appmsg&gt;&lt;/msg&gt;</content></refermsg></appmsg></msg>'))
    # 被引的那条走同一个渲染：这里是条链接（内层 appmsg 没有 type），
    # 以前直接把 refermsg 正文当文本，图片/表情的引用就会往面板上押一段 XML
    assert quoted['text'] == '直接用这个点进去对比', quoted
    assert quoted['quote'] == {'content': '直接用这个点进去对比', 'name': 'zzq',
                               'text': '[链接] 和我一起测测'}, quoted

    # 详情抽屉要能看原始 XML：行里带上没被渲染丢掉的载荷
    assert row['xml'] == '这个怎么弄啊'
    assert quoted['xml'].startswith('<msg><appmsg>'), quoted['xml']
    # 普通链接（type != 57）不走引用块
    link = admin._row(msg(49, '<msg><appmsg><title>一篇文章</title><type>5</type></appmsg></msg>'))
    assert link['quote'] is None, link

    # 附件接口：daemon 下载并保管 → bot 只要字节 → 带 content-type 吐给浏览器
    async def fake_media_bytes(msg_id):
        return b'\xff\xd8\xff\x00', 'jpg'

    runtime.bot.wechat.media_bytes_by_id = fake_media_bytes
    admin.call = lambda coro, timeout=0: asyncio.run(coro)  # 没有事件循环，就地跑
    assert admin.media('12345') == (b'\xff\xd8\xff\x00', 'image/jpeg')
    try:
        admin.media('12abc')
        raise AssertionError('非数字 msg_id 应该被拒')
    except ValueError:
        pass

    # 登录卡：二维码出 base64（面板直接塞 data: URL），轮询拿不到进行中的登录不能报 500
    qr = types.SimpleNamespace(uuid='u1', url='https://weixin.qq.com/x', expires_in=300, png=b'\x89PNG')

    async def fake_login_qr():
        return qr

    async def fake_login_poll():
        raise RuntimeError('没有进行中的扫码登录（先调 login_qr_start）')

    runtime.bot.wechat.login_qr = fake_login_qr
    runtime.bot.wechat.login_qr_poll = fake_login_poll
    box = admin.login_qr()
    assert box == {'uuid': 'u1', 'url': 'https://weixin.qq.com/x', 'expires_in': 300,
                   'png': base64.b64encode(b'\x89PNG').decode()}, box
    assert admin.login_poll()['status'] == 'idle', admin.login_poll()

    # 「为什么没回」抽屉：没有 trace 也不能炸
    drawer = admin.trace(9202842881785760589)
    assert drawer['event']['status'] == Status.GATED and drawer['trace'] is None

    # 日志尾巴：文件不存在返回空
    assert admin.log_tail() == []

    # 健康卡：daemon 拿不到也要能出结果（loop 是 None，会抛 → 被兜住）
    health = admin.health()
    assert health['daemon']['ok'] is False and 'error' in health['daemon']
    assert health['focus'][0]['chat'] == '测试群' and health['backoff'] == {'测试群': 12.0}
    assert health['emojis']['sendable'] == 4 and health['moments'] == 7

    # 允许回复 / 专注开关：键都是显示名。底层的 mute/unmute 在 test_bot.py / test_runtime.py 里测
    installs: list[int] = []
    runtime.install_scope = lambda: installs.append(1)
    assert admin.set_focus('另一个群', True) == {'focus': ['另一个群', '测试群']}
    assert admin.set_focus('另一个群', False) == {'focus': ['测试群']}
    # 默认 mode='all'：关掉一个 = 进 blocked；没表过态的会话跟模式走
    assert admin.set_allowed('测试群', False) == {'allowed': False}
    assert not runtime.reply_scope.allows('测试群')
    assert runtime.reply_scope.allows('没表过态的群')
    scope = runtime.reply_scope.to_dict()
    assert scope['mode'] == 'all' and scope['allowed'] == [], scope
    assert '测试群' in scope['blocked'] and '研究生摆烂群' in scope['blocked'], scope
    # 换成白名单：没表过态的一律不回，表过态的按表态来
    assert admin.set_scope_mode('allow')['mode'] == 'allow'
    assert not runtime.reply_scope.allows('没表过态的群')
    assert not runtime.reply_scope.allows('测试群')
    assert admin.set_allowed('测试群', True)['allowed'] is True
    assert runtime.reply_scope.allows('测试群')          # 显式允许在白名单模式下也回
    assert installs and len(installs) == 3, installs      # 每次改都重装插件链
    try:
        admin.set_scope_mode('everyone')
        raise AssertionError('未知模式应该被拒')
    except ValueError:
        pass
    try:
        admin.set_focus('', True)
        raise AssertionError('空 chat 应该被拒绝')
    except ValueError:
        pass

    state = admin.state()
    assert state['nickname'] == 'Bot' and state['dry_run'] is False and state['seq'] > 0

    # 配置面板：每个 Config 字段都得在某个分组里，不能漏（新加配置忘了入面板会被抓）
    grouped = {name for _, names in CONFIG_GROUPS for name in names}
    ungrouped = [f.name for f in dataclass_fields(Config) if f.name not in grouped]
    assert not ungrouped, f'这些配置字段没进面板分组: {ungrouped}'

    flat = {item['name']: item for group in admin.read_config()['groups'] for item in group['items']}
    assert flat['deepseek_key']['secret'] and flat['deepseek_key']['value'] == ''
    assert flat['deepseek_key']['isset'] is False        # Config() 默认 key 是空
    assert flat['typo_rate']['hot'] and not flat['daemon_port']['hot']

    # 写回 .env：先校验 → 原子写 → 保留注释 → 报出要重启的字段
    with tempfile.TemporaryDirectory() as tmp:
        admin.dotenv = Path(tmp) / '.env'
        admin.dotenv.write_text('# 注释\nTYPO_RATE=0.08\n', encoding='utf-8')
        out = admin.write_config({'typo_rate': '0', 'daemon_port': '9999'})
        assert out['changed'] == {'typo_rate': '0.0', 'daemon_port': '9999'}, out
        assert out['stale'] == ['daemon_port'], out
        written = admin.dotenv.read_text(encoding='utf-8')
        assert '# 注释' in written and 'TYPO_RATE=0' in written and 'DAEMON_PORT=9999' in written

        # 坏值：一个字都不能写（否则下次启动直接起不来）
        try:
            admin.write_config({'typo_rate': 'abc'})
            raise AssertionError('坏值应该被拒')
        except ValueError as e:
            assert 'typo_rate' in str(e), e
        assert admin.dotenv.read_text(encoding='utf-8') == written

        try:
            admin.write_config({'没这个': '1'})
            raise AssertionError('未知键应该被拒')
        except ValueError:
            pass

        # 密钥留空 = 保持原值（不回显的东西就不该被覆盖）
        assert admin.write_config({'deepseek_key': ''})['changed'] == {}
        assert 'DEEPSEEK_KEY' not in admin.dotenv.read_text(encoding='utf-8')

        # 布尔在 .env 里存 1/0
        admin.write_config({'enable_focus': False})
        assert 'ENABLE_FOCUS=0' in admin.dotenv.read_text(encoding='utf-8')

        # 热重载：重读 .env 并交给 runtime.apply
        applied.clear()
        admin.dotenv.write_text('TYPO_RATE=0.25\n', encoding='utf-8')
        result = admin.reload_config()
        assert applied and applied[0].typo_rate == 0.25, applied
        assert result['changed']['typo_rate'] == '0.25', result

        # 保存即生效：write_config 自己调 apply，面板不用再点一次「热重载」
        applied.clear()
        admin.write_config({'agent_max_replies': '2'})
        assert applied and applied[0].agent_max_replies == 2, applied
        # 越界的数值当场拒，且一个字都不写
        try:
            admin.write_config({'reply_context_length': '0'})
            raise AssertionError('越界值应该被拒')
        except ValueError as e:
            assert 'reply_context_length' in str(e), e

    # reload_config 用 Config.from_env() 重建，把自检的两个哨兵路径带回了默认值——
    # 下面统计卡的断言依赖它们指向不存在的文件，钉回去
    runtime.config = replace(runtime.config, trace_path='/no/such/trace.jsonl',
                             log_path='/no/such/bot.log')

    # 状态落库（统计卡的分母）+ 比率公式
    bus.subscribe(admin.decisions.write)
    bus.publish(Status.RECEIVED, msg_id=1, chat_id='g')
    bus.publish(Status.GATED, msg_id=1, chat_id='g', score=45)
    bus.publish(Status.RECEIVED, msg_id=2, chat_id='g')
    bus.publish(Status.DECIDING, msg_id=2, chat_id='g')
    bus.publish(Status.PLANNER_SILENT, msg_id=2, chat_id='g')
    bus.publish(Status.QUEUED, reply_to=2, origin='bot')      # 没身份：不入库
    bus.unsubscribe(admin.decisions.write)
    counts = admin.decisions.counts(0)
    # 逐条对比：接口上的数字必须就是这些事件本身（P3 的验收条件）
    assert counts == {Status.RECEIVED: 2, Status.GATED: 1,
                      Status.DECIDING: 1, Status.PLANNER_SILENT: 1}, counts
    assert Status.QUEUED not in counts
    rates = _rates(counts)
    assert rates['gate_pass'] == 0.5, rates          # 2 条入站，1 条被门控拦下
    assert rates['planner_silent'] == 1.0, rates     # 1 次决策，1 次选择不发言
    assert rates['reply'] == 0.0 and rates['failed'] == 0.0
    assert _ratio(1, 0) is None                       # 没分母时不能说「0%」
    assert _percentile([], 0.5) is None
    assert _percentile([10], 0.95) == 10
    assert _percentile(list(range(1, 101)), 0.5) == 51

    # 统计接口：trace 文件不存在时延迟那一项是「没有」而不是 0
    board = admin.stats(hours=1)
    assert board['latency'] == {'p50': None, 'p95': None, 'n': 0}, board['latency']
    assert board['counts'][Status.RECEIVED] == 2
    assert board['focus']['entered'] == 2                     # 前面 set_focus 进了两次

    # dataclass 字段自检：Config 里每个字段都得在 HOT 或 RESTART 里，不能漏
    known = HOT_FIELDS | RESTART_FIELDS
    missing = [f.name for f in dataclass_fields(Config) if f.name not in known]
    assert not missing, f'这些配置字段没归类（热重载还是重启）: {missing}'
    assert not (HOT_FIELDS & RESTART_FIELDS), HOT_FIELDS & RESTART_FIELDS
    # 提示词校验：语法错 / 变量名拼错都要拦下来（不拦的话热重载后每轮都抛，
    # 而消息链把异常吞掉，表现成「机器人突然不说话」）
    _validate_prompt('你好 {{ self_name }}，{{ behavior_style }}')
    for bad in ('{% if tools %}没有 endif', '{{ behaviour_style }}', '{% for x in %}'):
        try:
            _validate_prompt(bad)
            raise AssertionError(f'应该被拒: {bad}')
        except ValueError:
            pass

    # 分会话人设：全局当参照，写入的覆盖只作用在该会话
    view = admin.read_persona('某群')
    assert view['overrides'] == {} and view['global']['reply_style'], view
    assert list(view['fields']) == list(OVERRIDE_FIELDS)
    admin.write_persona('某群', {'personality': '是只猫', 'aliases': '阿猫'})
    assert admin.read_persona('某群')['overrides']['personality'] == '是只猫'
    assert admin.read_persona('另一个群')['overrides'] == {}
    # 清空 = 回全局
    admin.write_persona('某群', {})
    assert admin.read_persona('某群')['overrides'] == {}
    # 未知字段 / 越界概率当场拒
    for bad in ({'nope': 1}, {'style_probability': 5}):
        try:
            admin.write_persona('某群', bad)
            raise AssertionError(f'应该被拒: {bad}')
        except ValueError:
            pass

    # 提示词面板要能改 agent 模式那份（默认走的就是它）
    assert set(PROMPTS) == {'planner', 'replyer', 'agent'}, PROMPTS

    # 面板的状态表必须覆盖每一个 Status，否则 chip 会显示裸状态名
    assert 'src/main.tsx' in (WEB_DIR / 'index.html').read_text(encoding='utf-8'), \
        'web/index.html 没引用 src/main.tsx'
    status_ts = (WEB_DIR / 'src' / 'status.ts').read_text(encoding='utf-8')
    missing = [s for name, s in vars(Status).items()
               if name.isupper() and not name.startswith('_') and isinstance(s, str)
               and f'{s}:' not in status_ts]
    assert not missing, f'web/src/status.ts 的 LABEL/GROUP 没覆盖这些状态: {missing}'

    print('admin self-check ok')


if __name__ == '__main__':
    _self_check()
