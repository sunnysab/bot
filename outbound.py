""" 全局出站队列：串行发送 + 最小间隔限流 + 拟人打字延迟。

所有主动发送都必须经过它，插件和预处理不应直接调用 wechat.send_text，否则会绕开限流。
单消费者天然串行，因此不需要锁。
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from loguru import logger


@dataclass(slots=True)
class Outbound:
    receiver: str
    text: str
    sender: str = ''


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

    def put(self, receiver: str, text: str, sender: str = '') -> None:
        # 非阻塞入队，避免拖住 SDK 的顺序事件循环
        # ponytail: 无界队列，个人 bot 量级够用；刷屏再换成有界 + 丢弃策略
        self._queue.put_nowait(Outbound(receiver, text, sender))

    async def start(self) -> None:
        self._task = asyncio.create_task(self._worker(), name='outbound')

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _worker(self) -> None:
        loop = asyncio.get_running_loop()
        next_at = 0.0
        while True:
            item = await self._queue.get()
            if self._typing_delay:
                await asyncio.sleep(self._typing_delay(item.text))
            await asyncio.sleep(max(0.0, next_at - loop.time()))
            next_at = loop.time() + self._min_interval
            try:
                await self._send(item)
            except Exception as e:  # 单条失败不能拖垮 worker
                logger.error(f'发送失败 {item.receiver}: {e}')
