""" 全局出站队列：串行发送 + 最小间隔限流 + 拟人打字延迟。

所有主动发送都必须经过它，插件和预处理不应直接调用 wechat.send_text，否则会绕开限流。
单消费者天然串行，因此不需要锁。
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from loguru import logger

import otel

if TYPE_CHECKING:
    from context import SingleRecord

from emojis import Emoji
from events import Status, bus


def _key(value) -> str:
    return '' if value in (None, '') else str(value)


@dataclass(slots=True)
class Outbound:
    receiver: str
    text: str = ''
    sender: str = ''
    emoji: Emoji | None = None
    # 这条回复针对的入站消息。出站要等 daemon 收下之后才有自己的 msg_id，
    # 在那之前面板只能靠 reply_to 把「排队中/输入中」挂到对应的入站气泡上
    reply_to: str = ''
    chat_name: str = ''
    # 这条出站对应历史里的哪条记录。撤回成功后要抹掉它，否则模型下一轮
    # 还看得见自己已经撤回的话（见 Bot.revoke）
    record: SingleRecord | None = None
    # 引用回复的目标（被引消息的 64 位 svrId）；0 = 不引用
    ref_svrid: int = 0
    # 要 @ 的群成员 wxid，多个用逗号分隔；空 = 不 @
    at_list: str = ''
    # 入队那一刻的 trace context。发送发生在几秒后（打字延迟），那时 contextvars
    # 早换了，只能显式带着它，才能把出站挂在触发它的那一轮下面
    trace_ctx: object | None = None


class OutboundQueue:
    def __init__(self, send: Callable[[Outbound], Awaitable[None]], min_interval: float = 1.0,
                 typing_delay: Callable[[str], float] | None = None):
        """ :param send: 实际发送函数，由队列串行调用
        :param min_interval: 全局最小发送间隔（秒）
        :param typing_delay: 拟人打字延迟，入参为文本，返回秒数
        """
        self._send = send
        self._min_interval = min_interval
        self._typing_delay = typing_delay
        self._queue: asyncio.Queue[Outbound] = asyncio.Queue()
        self._task: asyncio.Task | None = None

    def put(self, receiver: str, text: str, sender: str = '',
            reply_to=None, chat_name: str = '', record=None, ref_svrid: int = 0,
            at_list: str = '', trace_ctx: object | None = None) -> None:
        # 非阻塞入队，避免拖住 SDK 的顺序事件循环
        # ponytail: 无界队列，个人 bot 量级够用；刷屏再换成有界 + 丢弃策略
        bus.publish(Status.QUEUED, chat_id=receiver, chat_name=chat_name,
                    reply_to=reply_to, origin='bot', detail=f'{len(text)} 字')
        self._queue.put_nowait(Outbound(receiver, text, sender,
                                        reply_to=_key(reply_to), chat_name=chat_name, record=record,
                                        ref_svrid=ref_svrid or 0, at_list=at_list,
                                        trace_ctx=trace_ctx))

    def put_emoji(self, receiver: str, emoji: Emoji, sender: str = '',
                  reply_to=None, chat_name: str = '', record=None,
                  trace_ctx: object | None = None) -> None:
        """ 发表情。同样过队列——不然它会绕过限速和打字延迟。 """
        bus.publish(Status.QUEUED, chat_id=receiver, chat_name=chat_name,
                    reply_to=reply_to, origin='bot', detail='表情')
        self._queue.put_nowait(Outbound(receiver, '', sender, emoji,
                                        reply_to=_key(reply_to), chat_name=chat_name, record=record,
                                        trace_ctx=trace_ctx))

    async def start(self) -> None:
        self._task = asyncio.create_task(self._worker(), name='outbound')

    def set_interval(self, seconds: float) -> None:
        """ 热重载改限速（防风控）；队列里已经排好的不受影响。 """
        self._min_interval = seconds

    def depth(self) -> int:
        """ 还堆着多少条没发。管理后台的健康卡要显示。 """
        return self._queue.qsize()

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _worker(self) -> None:
        loop = asyncio.get_running_loop()
        next_at = 0.0
        while True:
            item = await self._queue.get()
            attrs = {'wxbot.chat_id': item.receiver, 'wxbot.chat_name': item.chat_name}
            # 等待（打字延迟 + 最小间隔）和真正发送分开算：感知延迟的大头常常在前者
            with otel.span('outbound.wait', {**attrs, 'wxbot.queue_depth': self._queue.qsize()},
                           parent=item.trace_ctx):
                if item.text and self._typing_delay:
                    # 队列里还有积压 = 已经落后群里的节奏了。这时还逐条按真人打字速度磨，
                    # 回复会迟到几分钟，群里话题早翻篇了（实测 2~3 分钟，见 notes）。
                    # 按积压深度等比砍，清空后恢复满速拟人。
                    delay = self._typing_delay(item.text) / (1 + self._queue.qsize())
                    bus.publish(Status.TYPING, chat_id=item.receiver, chat_name=item.chat_name,
                                reply_to=item.reply_to, origin='bot',
                                detail=f'拟人延迟 {delay:.1f}s')
                    await asyncio.sleep(delay)
                await asyncio.sleep(max(0.0, next_at - loop.time()))
            next_at = loop.time() + self._min_interval
            with otel.span('outbound.send', attrs, parent=item.trace_ctx):
                try:
                    await self._send(item)
                except Exception as e:  # 单条失败不能拖垮 worker
                    otel.fail(str(e))
                    logger.error(f'发送失败 {item.receiver}: {e}')
