""" 事件总线：把消息处理过程中的状态变化广播给管理后台。

只传**元数据**，不传正文——正文的事实源是 relay 的 messages 表，
这里只回答「这条消息现在到哪一步了、跳过的原因是什么」。
设计见 notes/管理后台设计.md。

两条硬规矩：
1. **publish 整体吞异常。** 后台坏掉绝不能影响聊天（和 reply_trace.py 一个原则）。
2. **msg_id 一律存字符串。** relay 的 server_id 是 64 位整数（实测 985 条里 958 条
   超过 JS 安全整数 2^53），当数字过 JSON 会被 JS 悄悄改掉，然后状态就永远贴不上，
   而且不报错。转字符串是这条链上唯一可靠的时刻。

纯内存、无 IO，所以可以直接在事件循环里调。
"""

from __future__ import annotations

import itertools
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass

from loguru import logger

RING = 2000  # 内存里留最近这么多条，够面板断线重连后补齐


class Status:
    """ 一条消息可能处于的状态。用常量而不是裸字符串：拼错了是隐形 bug。 """

    # 入站
    RECEIVED = 'received'
    STALE = 'stale'                        # 积压消息：只记不回
    PHONE = 'phone'                        # 手机上自己发的：只记不回
    MUTED = 'muted'                        # 该会话被 EndProcessing 静音
    UNSUPPORTED = 'unsupported'            # 不是文本/图片
    GATED = 'gated'                        # 必要性打分没过线
    BACKOFF = 'backoff'                    # 连续沉默退避中
    DECIDING = 'deciding'                  # 决策者跑着
    PLANNER_SILENT = 'planner_silent'      # 决策者选择不发言
    PLANNER_FAILED = 'planner_failed'      # 决策调用失败，直接交给表达者
    EXPRESSING = 'expressing'              # 表达者跑着
    EXPRESS_FAILED = 'express_failed'      # 表达者调用失败
    EMPTY_REPLY = 'empty_reply'            # 清洗后为空
    PLACEHOLDER_BLOCKED = 'placeholder_blocked'
    # 出站
    QUEUED = 'queued'
    TYPING = 'typing'
    SENT = 'sent'
    DRY_RUN = 'dry_run'                    # 演习：没发出去，但面板要看得见
    FAILED = 'failed'

    # 面板按这四组上色 / 过滤
    _GROUP = {
        RECEIVED: 'pending', DECIDING: 'pending', EXPRESSING: 'pending',
        QUEUED: 'pending', TYPING: 'pending',
        SENT: 'replied', PHONE: 'phone',
        STALE: 'skipped', MUTED: 'skipped', UNSUPPORTED: 'skipped',
        GATED: 'skipped', BACKOFF: 'skipped', PLANNER_SILENT: 'skipped',
        EMPTY_REPLY: 'skipped', PLACEHOLDER_BLOCKED: 'skipped',
        PLANNER_FAILED: 'warn', EXPRESS_FAILED: 'failed', FAILED: 'failed',
        DRY_RUN: 'dry_run',
    }

    @classmethod
    def group(cls, status: str) -> str:
        return cls._GROUP.get(status, 'unknown')


@dataclass(frozen=True, slots=True)
class Event:
    """ 一次状态变化。

    :param seq: 单调递增，面板轮询的游标就是它
    :param msg_id: 事件主体的身份键，**字符串**。入站用 relay 的 msg_id；
                   出站要等 relay 发出去之后才有；演习模式的出站永远是空
    :param reply_to: 这条事件属于哪条消息。出站还没拿到自己的 msg_id 时
                     （queued / typing）靠它挂到被回复的那条入站消息上
    :param origin: '' / 'bot' / 'phone'
    :param text: 只有演习模式的出站才带正文——那条回复没进历史、也没进 relay，
                 不带就彻底没人看得见
    """

    seq: int
    ts: float
    status: str
    chat_id: str = ''
    chat_name: str = ''
    msg_id: str = ''
    reply_to: str = ''
    detail: str = ''
    score: int = 0
    trigger: str = ''
    latency_ms: int = 0
    origin: str = ''
    text: str = ''

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def group(self) -> str:
        return Status.group(self.status)


def _key(value) -> str:
    """ identity 一律转字符串；None / 空都不留 'None' 这种脏值。 """
    return '' if value in (None, '') else str(value)


class EventBus:
    """ 订阅-轮询式的事件总线。

    没有推送：面板每 1 秒 `?since=<seq>` 拉一次。单用户本地工具，
    轮询没有线程泄漏和背压问题，接了游标还天生去重（见设计文档第四节）。
    """

    def __init__(self, ring: int = RING):
        self._lock = threading.Lock()
        self._seq = itertools.count(1)
        self._ring: deque[Event] = deque(maxlen=ring)
        # msg_id -> 最近一条事件，给「这条消息现在什么状态」用
        self._latest: dict[str, Event] = {}
        # 被回复的消息 msg_id -> 那个回复最近到哪一步了。
        # 出站要等 relay 收下才有自己的身份，所以回复的进度挂在被回复的那条上
        self._by_reply: dict[str, Event] = {}
        # 每条事件都要跑一遍的回调（落盘用）。同步调用，自己负责快
        self._subscribers: list = []

    def subscribe(self, fn) -> None:
        """ 订阅每一条事件（在发布线程里同步调用，别在里面做慢活）。 """
        with self._lock:
            self._subscribers.append(fn)

    def unsubscribe(self, fn) -> None:
        with self._lock:
            if fn in self._subscribers:
                self._subscribers.remove(fn)

    def publish(self, status: str, *, chat_id: str = '', chat_name: str = '',
                msg_id=None, reply_to=None, **fields) -> Event | None:
        """ 发一条事件。永不抛异常，拿不到事件返回 None。 """
        try:
            with self._lock:
                event = Event(seq=next(self._seq), ts=time.time(), status=status,
                              chat_id=chat_id, chat_name=chat_name,
                              msg_id=_key(msg_id), reply_to=_key(reply_to), **fields)
                self._ring.append(event)
                if event.msg_id:
                    self._latest[event.msg_id] = event
                if event.reply_to:
                    self._by_reply[event.reply_to] = event
                subscribers = list(self._subscribers)
            # 出了锁再通知：订户要落盘，别把发布路径锁住
            for fn in subscribers:
                try:
                    fn(event)
                except Exception as e:  # noqa: BLE001 —— 一个订户坏了不能影响其他
                    logger.warning(f'事件订阅者出错（{status}）: {e}')
            return event
        except Exception as e:  # noqa: BLE001 —— 后台绝不能把聊天带崩
            logger.warning(f'事件发布失败（{status}）: {e}')
            return None

    def since(self, seq: int = 0) -> list[Event]:
        """ 取 seq 之后的所有事件。游标太久远就只给环里剩下的。 """
        with self._lock:
            return [event for event in self._ring if event.seq > seq]

    def status_of(self, msg_id) -> Event | None:
        """ 这条消息**自己**最近的状态（有没有过门控、决策说了什么）；没有就是 None。 """
        with self._lock:
            return self._latest.get(_key(msg_id))

    def progress_of(self, msg_id) -> Event | None:
        """ 针对这条消息的**回复**进行到哪一步了（排队 / 输入中 / 已发送 / 失败）。

        和 status_of 是两个问题：前者答「要不要回」，后者答「回得怎么样」。
        """
        with self._lock:
            return self._by_reply.get(_key(msg_id))

    def latest_seq(self) -> int:
        with self._lock:
            return self._ring[-1].seq if self._ring else 0

    def reset(self) -> None:
        """ 测试用。 """
        with self._lock:
            self._ring.clear()
            self._latest.clear()
            self._by_reply.clear()


# 全局单例：埋点散在 bot / plugin / outbound 三处，注入一遍要改一堆构造签名，
# 而它本身就是横切的可观测性设施，不值得为它加参数。
bus = EventBus()


def _self_check() -> None:
    b = EventBus(ring=3)

    # 没有事件时什么都不炸
    assert b.since() == [] and b.status_of(123) is None and b.latest_seq() == 0

    # 64 位 msg_id 进总线必须是字符串
    big = 9202842881785760589
    e = b.publish(Status.RECEIVED, chat_id='g@chatroom', chat_name='某群', msg_id=big)
    assert e.msg_id == '9202842881785760589' and isinstance(e.msg_id, str), e
    assert b.status_of(big).status == Status.RECEIVED      # 用原始整数也能查到
    assert b.status_of('9202842881785760589') is b.status_of(big)

    # 同一 msg_id 的新事件盖掉旧的（面板只关心当前状态）
    b.publish(Status.GATED, msg_id=big, detail='门控 45/60', score=45)
    assert b.status_of(big).status == Status.GATED and b.status_of(big).score == 45

    # 出站早期没有自己的 msg_id：挂 reply_to，且不能污染 _latest
    q = b.publish(Status.QUEUED, chat_id='g@chatroom', reply_to=big, origin='bot')
    assert q.msg_id == '' and q.reply_to == str(big)
    assert b.status_of('') is None
    assert b.status_of(big).status == Status.GATED     # 没被出站事件盖掉

    # 「回复得怎么样」是另一个问题，走 progress_of
    assert b.progress_of(big).status == Status.QUEUED
    b.publish(Status.SENT, chat_id='g@chatroom', reply_to=big, msg_id=111, origin='bot')
    assert b.progress_of(big).status == Status.SENT
    assert b.status_of(big).status == Status.GATED     # 自己的状态没被出站盖掉
    assert b.status_of(111).status == Status.SENT      # 出站自己有身份了
    assert b.progress_of('从没被回复过') is None

    # 环满了就不留最老的，但游标语义不变
    for i in range(5):
        b.publish(Status.DECIDING, msg_id=i)
    last = b.latest_seq()
    assert len(b.since(0)) == 3 and len(b.since(last)) == 0
    assert [e.seq for e in b.since(last - 3)] == [last - 2, last - 1, last]

    # 分组映射；未知状态也不能炸
    assert Status.group(Status.SENT) == 'replied'
    assert Status.group(Status.BACKOFF) == 'skipped'
    assert Status.group('没这个状态') == 'unknown'
    for name in ('RECEIVED', 'DECIDING', 'EXPRESSING', 'QUEUED', 'TYPING', 'SENT',
                 'PHONE', 'STALE', 'MUTED', 'UNSUPPORTED', 'GATED', 'BACKOFF',
                 'PLANNER_SILENT', 'EMPTY_REPLY', 'PLACEHOLDER_BLOCKED',
                 'PLANNER_FAILED', 'EXPRESS_FAILED', 'FAILED', 'DRY_RUN'):
        assert Status.group(getattr(Status, name)) != 'unknown', name

    # 吞异常：传一个不认识的字段也得活
    assert bus.publish(Status.RECEIVED, msg_id=1, 不存在的字段=1) is None
    assert bus.publish(Status.RECEIVED, msg_id=1).status == Status.RECEIVED

    # 订户：每条都收到，且一个订户报错不影响别的
    seen: list = []
    b.subscribe(lambda e: seen.append(('first', e.status)))
    b.subscribe(lambda _e: 1 / 0)                      # 坏订户
    b.subscribe(lambda e: seen.append(('second', e.status)))
    b.publish(Status.SENT, msg_id=42)
    assert ('first', Status.SENT) in seen, seen          # 坏订户之前的不受影响
    assert ('second', Status.SENT) in seen, seen         # 坏订户之后的也活着

    import json
    assert json.loads(json.dumps(e.to_dict()))['msg_id'] == '9202842881785760589'
    print('events self-check ok')


if __name__ == '__main__':
    _self_check()
