""" 全局出站队列：串行发送 + 最小间隔限流 + 拟人打字延迟。

所有主动发送都必须经过它，插件和预处理不应直接调用 wechat.send_text，否则会绕开限流。
单消费者天然串行，因此不需要锁。
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from loguru import logger

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
    # 这条回复针对的入站消息。出站要等 relay 收下之后才有自己的 msg_id，
    # 在那之前面板只能靠 reply_to 把「排队中/输入中」挂到对应的入站气泡上
    reply_to: str = ''
    chat_name: str = ''


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
            reply_to=None, chat_name: str = '') -> None:
        # 非阻塞入队，避免拖住 SDK 的顺序事件循环
        # ponytail: 无界队列，个人 bot 量级够用；刷屏再换成有界 + 丢弃策略
        bus.publish(Status.QUEUED, chat_id=receiver, chat_name=chat_name,
                    reply_to=reply_to, origin='bot', detail=f'{len(text)} 字')
        self._queue.put_nowait(Outbound(receiver, text, sender,
                                        reply_to=_key(reply_to), chat_name=chat_name))

    def put_emoji(self, receiver: str, emoji: Emoji, sender: str = '',
                  reply_to=None, chat_name: str = '') -> None:
        """ 发表情。同样过队列——不然它会绕过限速和打字延迟。 """
        bus.publish(Status.QUEUED, chat_id=receiver, chat_name=chat_name,
                    reply_to=reply_to, origin='bot', detail='表情')
        self._queue.put_nowait(Outbound(receiver, '', sender, emoji,
                                        reply_to=_key(reply_to), chat_name=chat_name))

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
            if item.text and self._typing_delay:
                delay = self._typing_delay(item.text)
                bus.publish(Status.TYPING, chat_id=item.receiver, chat_name=item.chat_name,
                            reply_to=item.reply_to, origin='bot',
                            detail=f'拟人延迟 {delay:.1f}s')
                await asyncio.sleep(delay)
            await asyncio.sleep(max(0.0, next_at - loop.time()))
            next_at = loop.time() + self._min_interval
            try:
                await self._send(item)
            except Exception as e:  # 单条失败不能拖垮 worker
                logger.error(f'发送失败 {item.receiver}: {e}')
