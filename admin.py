""" 管理后台：本地 HTTP 服务 + 一个页面。设计见 notes/管理后台设计.md。

三条原则：

1. **消息正文的事实源是 relay**，不在这里另存一份。这里只做两件事——
   把 relay 的消息读出来、把 events.py 里的状态贴上去。
2. **绝不拖累聊天。** 启动整段 try/except；任何一路出错只打日志 + 返回 500。
3. **msg_id 一律字符串。** relay 的 server_id 是 64 位整数，实测 985 条里 958 条
   超过 JS 安全整数 2^51 附近的那个坎，当数字过 JSON 会被 JS 悄悄改掉，
   然后状态就永远贴不上，而且不报错。

轮询而不是 SSE：单用户本地工具，1 秒拉一次，没有线程泄漏和背压问题，
靠 seq 游标还天生去重（见设计文档第四节）。
"""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from loguru import logger

from context import SYSTEM_KINDS, MessageKind
from events import Status, bus
from media import render

PAGE = Path(__file__).parent / 'admin.html'
DEFAULT_PORT = 8099          # 8080 在本机被 iptv 占了，别用
CHATS_LIMIT = 50
MESSAGES_LIMIT = 300         # v1 一次拉满，relay 的 get_messages 还不能翻页
RPC_TIMEOUT = 15.0


class Admin:
    def __init__(self, bot, wechat, cache=None, loop=None,
                 host: str = '127.0.0.1', port: int = DEFAULT_PORT):
        """ :param bot: 用来读 dry_run 之类的运行态
        :param wechat: relay 适配层，读会话列表和历史
        :param cache: MediaCache，把 md5 换成图片/表情的描述
        :param loop: bot 的事件循环，RPC 要切回去跑
        """
        self.bot = bot
        self.wechat = wechat
        self.cache = cache
        self.loop = loop
        self.host = host
        self.port = port
        self.server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ---------- 启动 / 停止 ----------

    def start(self) -> None:
        handler = type('_BoundHandler', (_Handler,), {'admin': self})
        self.server = ThreadingHTTPServer((self.host, self.port), handler)
        self._thread = threading.Thread(target=self.server.serve_forever,
                                        name='admin', daemon=True)
        self._thread.start()
        logger.info(f'管理后台: http://{self.host}:{self.port}')

    def stop(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()

    def call(self, coro, timeout: float = RPC_TIMEOUT):
        """ 从 HTTP 线程切回 bot 的事件循环执行一个协程。 """
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    # ---------- 数据 ----------

    def state(self) -> dict:
        groups: dict[str, int] = {}
        for event in bus.since(0):
            group = Status.group(event.status)
            groups[group] = groups.get(group, 0) + 1
        return {
            'wxid': getattr(self.wechat, 'wxid', ''),
            'nickname': getattr(self.wechat, 'nickname', ''),
            'dry_run': bool(getattr(self.bot, 'dry_run', False)),
            'seq': bus.latest_seq(),
            'groups': groups,
        }

    def chats(self) -> list[dict]:
        out = []
        for chat in self.call(self.wechat.conversations(limit=CHATS_LIMIT)):
            peer = getattr(chat, 'peer', '')
            # 公众号 / filehelper / weixin 这些不进列表，和 get_recent_sessions 一个规矩
            if not (peer.startswith('wxid_') or peer.endswith('@chatroom')):
                continue
            last = getattr(chat, 'last_message', None) or {}
            out.append({
                'chat_id': peer,
                'name': getattr(chat, 'display_name', '') or peer,
                'is_group': bool(getattr(chat, 'is_chatroom', False)),
                'count': getattr(chat, 'msg_count', 0),
                'updated_at': getattr(chat, 'updated_at', 0),
                'last': (last.get('clean_text') or last.get('content') or '')[:60],
            })
        return out

    def messages(self, chat: str, limit: int = MESSAGES_LIMIT) -> list[dict]:
        if not chat:
            return []
        raw = self.call(self.wechat.messages(chat, limit=limit))
        # relay 的 sender_display_name 查不到时会拿 wxid 当名字，那不是名字。
        # 只对真正拿不到的那几个查（查到的会缓存），不是每条都查
        unknown = [m.sender for m in raw
                   if not m.is_self and m.sender and m.sender_display_name == m.sender]
        names = self.call(self.wechat.sender_names(chat, unknown)) if unknown else {}
        return [row for row in (self._row(msg, names) for msg in raw) if row]

    def _row(self, msg, names: dict[str, str] | None = None) -> dict | None:
        kind = MessageKind.of(msg.msg_type)
        # 设备同步(51)/系统消息不该进聊天记录，而 relay 的 exclude_system 只排
        # 10000/10002——实测 51 有 34 条挂在群里，必须自己滤掉
        if kind in SYSTEM_KINDS or kind is MessageKind.OTHER:
            return None
        text, media = render(msg.msg_type, msg.content, msg.text)
        if not text:
            return None
        if media is not None and not media.description and media.md5 and self.cache:
            cached = self.cache.get(media.md5)
            if cached:
                media = replace(media, description=cached)
                text = media.placeholder

        own = bus.status_of(msg.msg_id)
        reply = bus.progress_of(msg.msg_id)
        return {
            'cursor': (msg.raw_data or {}).get('id'),      # relay 自增 id，翻页用
            'msg_id': str(msg.msg_id),                     # 身份键，字符串
            'ts': msg.create_time,
            'sender': ('我' if msg.is_self else '') or (names or {}).get(msg.sender) \
                      or msg.sender_display_name or msg.sender,
            'outgoing': bool(msg.is_self),
            'text': text,
            'status': own.status if own else '',
            'status_detail': own.detail if own else '',
            'score': own.score if own else 0,
            'reply_status': reply.status if reply else '',
            'reply_detail': reply.detail if reply else '',
            'dry_text': reply.text if reply and reply.status == Status.DRY_RUN else '',
        }

    # ---------- HTTP ----------

    def handle_get(self, handler: BaseHTTPRequestHandler) -> None:
        url = urlparse(handler.path)
        query = parse_qs(url.query)
        route = url.path
        try:
            if route in ('/', '/index.html'):
                return self._send(handler, PAGE.read_text(encoding='utf-8'),
                                  'text/html; charset=utf-8')
            if route == '/api/state':
                return self._json(handler, self.state())
            if route == '/api/chats':
                return self._json(handler, {'chats': self.chats()})
            if route == '/api/messages':
                chat = (query.get('chat') or [''])[0]
                limit = int((query.get('limit') or [MESSAGES_LIMIT])[0])
                return self._json(handler, {'messages': self.messages(chat, limit)})
            if route == '/api/events':
                since = int((query.get('since') or ['0'])[0])
                return self._json(handler, {'seq': bus.latest_seq(),
                                            'events': [e.to_dict() for e in bus.since(since)]})
            self._json(handler, {'error': 'not found'}, 404)
        except Exception as e:  # noqa: BLE001 —— 后台出错不能冒泡到 bot
            logger.warning(f'管理后台 {route} 出错: {e}')
            self._json(handler, {'error': str(e)}, 500)

    @staticmethod
    def _send(handler, body: str, content_type: str, code: int = 200) -> None:
        data = body.encode('utf-8')
        handler.send_response(code)
        handler.send_header('Content-Type', content_type)
        handler.send_header('Content-Length', str(len(data)))
        handler.send_header('Cache-Control', 'no-store')
        handler.end_headers()
        handler.wfile.write(data)

    def _json(self, handler, payload, code: int = 200) -> None:
        self._send(handler, json.dumps(payload, ensure_ascii=False), 'application/json', code)


class _Handler(BaseHTTPRequestHandler):
    admin: Admin
    server_version = 'bot-admin/0.1'

    def do_GET(self) -> None:  # noqa: N802 —— BaseHTTPRequestHandler 的命名
        self.admin.handle_get(self)

    def log_message(self, fmt: str, *args) -> None:
        logger.debug(f'admin {self.address_string()} {fmt % args}')


def start(bot, wechat, cache=None, loop=None, host: str = '127.0.0.1',
          port: int = DEFAULT_PORT) -> Admin | None:
    """ 起后台。失败只打日志返回 None——管理后台绝不能把 bot 拖下水。 """
    try:
        admin = Admin(bot, wechat, cache, loop or asyncio.get_running_loop(), host, port)
        admin.start()
        return admin
    except Exception as e:  # noqa: BLE001
        logger.warning(f'管理后台启动失败（{host}:{port}），bot 继续跑: {e}')
        return None


def _self_check() -> None:
    """ 不连 relay 的自检：拿假消息把「渲染 + 贴状态」这条链走通。 """
    import types

    bot = types.SimpleNamespace(dry_run=False)
    admin = Admin(bot, types.SimpleNamespace(wxid='wxid_bot', nickname='Bot'), cache=None)
    bus.reset()

    def msg(msg_type, content, clean='', is_self=False):
        return types.SimpleNamespace(
            msg_id=9202842881785760589, msg_type=msg_type, content=content, clean_text=clean,
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

    # 表情 / 图片走占位符
    emoji = admin._row(msg(47, '<msg><emoji md5="m" desc="ChEKB2RlZmF1bHQSBuWVp+WVpw==" /></msg>'))
    assert emoji['text'] == '[表情：啧啧]', emoji

    # 有缓存描述时，图片占位符要补上描述
    class FakeCache:
        def get(self, md5):
            return '一只橘猫' if md5 == 'f9bf' else None

    admin.cache = FakeCache()
    image = admin._row(msg(3, '<msg><img md5="f9bf" /></msg>'))
    assert image['text'] == '[图片：一只橘猫]', image
    print('admin self-check ok')


if __name__ == '__main__':
    _self_check()
