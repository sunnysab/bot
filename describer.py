""" 媒体（图片 / 表情）语义描述：按 md5 去重 + sqlite 持久化缓存。

图片和缺 desc 的表情没有可读语义，只能靠 vision 描述。为避免重复付费，
结果按 md5 缓存到 sqlite，同一张媒体（尤其是被反复使用的表情包）只描述一次，跨重启生效。
"""

import asyncio
import sqlite3
import time

from loguru import logger

from ai import AiProvider
from context import SingleRecord
from media import Media


class MediaCache:
    """ md5 -> 描述 的持久化缓存。 """

    def __init__(self, path: str):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute(
            'CREATE TABLE IF NOT EXISTS media ('
            'md5 TEXT PRIMARY KEY, kind TEXT, description TEXT, model TEXT, updated_at INTEGER)')
        self._conn.commit()

    def get(self, md5: str) -> str | None:
        row = self._conn.execute('SELECT description FROM media WHERE md5 = ?', (md5,)).fetchone()
        return row[0] if row else None

    def put(self, md5: str, kind: str, description: str, model: str) -> None:
        self._conn.execute('INSERT OR REPLACE INTO media VALUES (?, ?, ?, ?, ?)',
                           (md5, kind, description, model, int(time.time())))
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()


class MediaDescriber:
    """ 后台把媒体描述回写到历史记录，不阻塞消息处理。 """

    def __init__(self, ai: AiProvider, cache: MediaCache, model: str, concurrency: int = 2,
                 timeout: float = 30.0, max_tokens: int = 64):
        """ :param concurrency: 同时进行的识图上限，防止刷图打爆接口
        :param timeout: 单张媒体的下载+识图超时（秒），超时保留占位符
        :param max_tokens: 描述的最大 token 数，限制慢模型的输出长度
        """
        self._ai = ai
        self._cache = cache
        self._model = model
        self._timeout = timeout
        self._max_tokens = max_tokens
        self._sem = asyncio.Semaphore(concurrency)
        self._inflight: dict[str, asyncio.Task] = {}
        self._tasks: set[asyncio.Task] = set()

    def schedule(self, record: SingleRecord) -> None:
        """ 只处理需要 vision 的媒体（无 desc 且有下载入口）。 """
        media = record.media
        if media is None or not media.needs_description or record.download is None:
            return
        task = asyncio.create_task(self._apply(media, record))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def join(self) -> None:
        """ 等待所有在途描述完成（测试用）。 """
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
        self._cache.close()

    async def _apply(self, media: Media, record: SingleRecord) -> None:
        try:
            description = self._cache.get(media.md5) or await self._describe_once(media, record.download)
        except Exception as e:
            logger.warning(f'描述媒体失败 md5={media.md5}: {e}')
            return
        if description:
            label = '图片' if media.kind == 'image' else '表情'
            record.text = f'[{label}：{description}]'

    async def _describe_once(self, media: Media, download) -> str | None:
        """ 同一 md5 的并发请求合流，只下载 / 描述一次。 """
        running = self._inflight.get(media.md5)
        if running is not None:
            return await running

        async def fetch() -> str | None:
            async with self._sem:
                try:
                    return await asyncio.wait_for(self._download_and_describe(media, download), self._timeout)
                except TimeoutError:
                    logger.warning(f'描述媒体超时 md5={media.md5}')
                    return None

        task = asyncio.create_task(fetch())
        self._inflight[media.md5] = task
        try:
            return await task
        finally:
            self._inflight.pop(media.md5, None)

    async def _download_and_describe(self, media: Media, download) -> str | None:
        image = await download()
        if not image:
            return None
        description = await self._ai.describe(image, max_tokens=self._max_tokens)
        if description:
            self._cache.put(media.md5, media.kind, description, self._model)
        return description
