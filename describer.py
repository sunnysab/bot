""" 媒体（图片 / 表情）语义描述：按 md5 去重 + sqlite 持久化缓存。

图片和缺 desc 的表情没有可读语义，只能靠 vision 描述。为避免重复付费，
结果按 md5 缓存到 sqlite，同一张媒体（尤其是被反复使用的表情包）只描述一次，跨重启生效。
"""

import asyncio
import sqlite3
import threading
import time
from dataclasses import replace

from loguru import logger

from ai import IMAGE_DESCRIBE_PROMPT, IMAGE_PROMPT_VERSION, AiProvider
from context import SingleRecord
from emojis import EMOJI_DESCRIBE_PROMPT, Emoji, EmojiLibrary, parse_description
from media import Media, add_handle

# 失败后的冷却期。取不到字节（消息过期 / daemon 说缺参数）短期不会变好，挡久一点；
# 限流、超时、空回复这些是临时的，过几分钟值得再试
FAIL_TTL_GONE = 24 * 60 * 60.0
FAIL_TTL_RETRY = 300.0


class MediaUnavailable(Exception):
    """ 拿不到这张图的字节（消息太老、daemon 报缺 msg_id/url/path）。 """


class MediaCache:
    """ md5 -> 描述 的持久化缓存。 """

    def __init__(self, path: str):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        # check_same_thread=False 只是关掉了 sqlite 的报错，不提供互斥：
        # describer 在事件循环里写，管理后台的 HTTP 线程在读，得自己上锁
        self._lock = threading.Lock()
        self._conn.execute(
            'CREATE TABLE IF NOT EXISTS media ('
            'md5 TEXT PRIMARY KEY, kind TEXT, description TEXT, model TEXT, updated_at INTEGER)')
        # 提示词换版时让老描述自然失效：老行没写 version，按版本查就 miss，用到时重描述
        columns = {row[1] for row in self._conn.execute('PRAGMA table_info(media)').fetchall()}
        if 'version' not in columns:
            self._conn.execute('ALTER TABLE media ADD COLUMN version TEXT DEFAULT ""')
        # 负缓存：这张图上次以失败告终，blocked_until 之前不再下载 / 调模型
        if 'blocked_until' not in columns:
            self._conn.execute('ALTER TABLE media ADD COLUMN blocked_until INTEGER DEFAULT 0')
        self._conn.commit()

    def get(self, md5: str, version: str | None = None) -> str | None:
        """ 取描述。version=None 表示不管版本（后台浏览用），给了版本就只认那一版。 """
        with self._lock:
            if version is None:
                row = self._conn.execute('SELECT description FROM media WHERE md5 = ?', (md5,)).fetchone()
            else:
                row = self._conn.execute('SELECT description FROM media WHERE md5 = ? AND version = ?',
                                         (md5, version)).fetchone()
        return row[0] if row else None

    def put(self, md5: str, kind: str, description: str, model: str, version: str = '') -> None:
        with self._lock:
            self._conn.execute(
                'INSERT OR REPLACE INTO media (md5, kind, description, model, updated_at, version) '
                'VALUES (?, ?, ?, ?, ?, ?)',
                (md5, kind, description, model, int(time.time()), version))
            self._conn.commit()

    def blocked(self, md5: str, version: str, now: float) -> bool:
        """ 这张图上次失败且还没到重试时间：别再下载 / 调模型。 """
        with self._lock:
            row = self._conn.execute('SELECT blocked_until FROM media WHERE md5 = ? AND version = ?',
                                     (md5, version)).fetchone()
        return bool(row and row[0] and row[0] > now)

    def note_failure(self, md5: str, kind: str, version: str, until: float) -> None:
        """ 记一次失败：`until` 之前不重试。

        否则消息太老、取不到字节的图，**每次重启都会再试一遍**（单张就要等满
        describe_timeout），白占着识图的并发。UPSERT 而不是 REPLACE：已经有描述的
        行不能被这次失败清掉。
        """
        with self._lock:
            self._conn.execute(
                'INSERT INTO media (md5, kind, version, blocked_until, updated_at) VALUES (?, ?, ?, ?, ?) '
                'ON CONFLICT(md5) DO UPDATE SET blocked_until = excluded.blocked_until',
                (md5, kind, version, int(until), int(time.time())))
            self._conn.commit()

    def items(self) -> list[tuple[str, str, str, int]]:
        """ 全部描述：[(md5, 种类, 描述, 写入时间)]。给媒体名录/面板浏览用。 """
        with self._lock:
            rows = self._conn.execute('SELECT md5, kind, description, updated_at FROM media').fetchall()
        return [(r[0], r[1], r[2] or '', r[3] or 0) for r in rows]

    def close(self) -> None:
        self._conn.close()


class MediaDescriber:
    """ 后台把媒体描述回写到历史记录，不阻塞消息处理。 """

    def __init__(self, ai: AiProvider, cache: MediaCache, model: str, concurrency: int = 2,
                 timeout: float = 30.0, max_tokens: int = 64, image_max_tokens: int | None = None):
        """ :param concurrency: 同时进行的识图上限，防止刷图打爆接口
        :param timeout: 单张媒体的下载+识图超时（秒），超时保留占位符
        :param max_tokens: 表情描述的最大 token 数，限制慢模型的输出长度
        :param image_max_tokens: 图片描述的最大 token 数，不给就和 max_tokens 一样。
            图片要抄文字 + 逐项列东西，比表情的一句话多
        """
        self._ai = ai
        self._cache = cache
        self._model = model
        self._timeout = timeout
        self._max_tokens = max_tokens
        self._image_max_tokens = image_max_tokens
        self._sem = asyncio.Semaphore(concurrency)
        self._inflight: dict[str, asyncio.Task] = {}
        self._tasks: set[asyncio.Task] = set()
        # 按记录索引的识图任务，给「决策前等一等」用（bot.describe_wait）
        self._pending: dict[int, asyncio.Task] = {}
        self._warned: set[str] = set()

    @staticmethod
    def _key(record: SingleRecord) -> int:
        """ 等待用的键：svrid 优先；没身份（自己发的 / 测试造的）就用对象 id。 """
        return record.msg_id or id(record)

    def schedule(self, record: SingleRecord) -> None:
        """ 只处理需要 vision 的媒体（无 desc 且有下载入口）。 """
        media = record.media
        if media is None or not media.needs_description or record.download is None:
            return
        task = asyncio.create_task(self._apply(media, record))
        self._tasks.add(task)
        key = self._key(record)
        self._pending[key] = task
        task.add_done_callback(lambda done: (self._tasks.discard(done), self._pending.pop(key, None)))

    def pending(self, record: SingleRecord) -> bool:
        """ 这条记录的识图还在跑吗。 """
        return self._key(record) in self._pending

    async def wait(self, record: SingleRecord, timeout: float) -> bool:
        """ 最多等 timeout 秒，等这条记录的识图落地。超时返回 False，但**不取消**它——
        后台跑完照样写回记录，下一轮看得到。
        """
        task = self._pending.get(self._key(record))
        if task is None:
            return True
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout)
            return True
        except TimeoutError:
            return False

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
        version = self._version(media)
        description = self._cache.get(media.md5, version)
        if description is None and self._cache.blocked(media.md5, version, time.time()):
            logger.debug(f'跳过（上次失败，还没到重试时间）md5={media.md5}')
            return
        try:
            description = description or await self._describe_once(media, record.download)
        except MediaUnavailable as e:
            self._retry_later(media, version, FAIL_TTL_GONE)
            self._warn_once(f'{media.kind}:gone', f'取不到媒体字节 md5={media.md5}: {e}')
            return
        except Exception as e:
            self._retry_later(media, version, FAIL_TTL_RETRY)
            self._warn_once(f'{media.kind}:{e}', f'描述媒体失败 md5={media.md5}: {e}')
            return
        if not description:
            self._retry_later(media, version, FAIL_TTL_RETRY)
            return
        label = '图片' if media.kind == 'image' else '表情'
        # handle 不能丢：内联图片靠它把附件和这行对上
        record.text = add_handle(f'[{label}：{description}]', record.handle)

    @staticmethod
    def _version(media: Media) -> str:
        return IMAGE_PROMPT_VERSION if media.kind == 'image' else ''

    def _retry_later(self, media: Media, version: str, ttl: float) -> None:
        """ 记负缓存：ttl 秒内不再碰这张图。 """
        self._cache.note_failure(media.md5, media.kind, version, time.time() + ttl)

    def _warn_once(self, key: str, message: str) -> None:
        """ 同一类失败只警告一次。

        刷图时同一类失败会连着来（下载超时 / 接口拒图），每张一条 warning 会把日志
        冲掉，真正要看的东西反而找不着。
        """
        if key in self._warned:
            logger.debug(message)
            return
        self._warned.add(key)
        logger.warning(f'{message}（同类失败后续只记 debug）')

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
                    # 超时也要记一笔：否则下一轮/下次重启又会把这张图从头等一遍
                    logger.warning(f'描述媒体超时 md5={media.md5}')
                    self._retry_later(media, self._version(media), FAIL_TTL_RETRY)
                    return None

        task = asyncio.create_task(fetch())
        self._inflight[media.md5] = task
        try:
            return await task
        finally:
            self._inflight.pop(media.md5, None)

    async def _download_and_describe(self, media: Media, download) -> str | None:
        try:
            image = await download()
        except Exception as e:  # noqa: BLE001 —— 取字节失败和模型失败要分开算冷却期
            raise MediaUnavailable(str(e)) from e
        if not image:
            raise MediaUnavailable('下载结果为空')
        if media.kind == 'image':
            description = await self._ai.describe(image, max_tokens=self._image_max_tokens or self._max_tokens,
                                                  prompt=IMAGE_DESCRIBE_PROMPT, model=self._model)
        else:
            description = await self._ai.describe(image, max_tokens=self._max_tokens, model=self._model)
        if description:
            self._cache.put(media.md5, media.kind, description, self._model, self._version(media))
        return description


class EmojiDescriber:
    """ 后台给偷来的表情补「情绪|使用场景」。

    微信的 desc 属性只给一个标签（「摊倒」）——够搜标签，不够搜场合。
    看真图写一句「什么情况下发它」，模型才能拿当前局面去对，
    也能用「想吐槽」「离谱」这类**不在标签里**的词搜到它。

    只补本群真用过的那些（有热度），不然群里每张野生表情都要花一次识图。
    """

    def __init__(self, ai: AiProvider, library: EmojiLibrary, model: str, scene_limit: int = 60,
                 concurrency: int = 1, timeout: float = 30.0, max_tokens: int = 80,
                 on_update=None):
        """ :param scene_limit: 最多补多少张——本群最常用的几十张搞懂就够了
        :param on_update: 补成功后回调，一般用来落盘
        """
        self._ai = ai
        self._library = library
        self._model = model
        self.scene_limit = scene_limit
        self._timeout = timeout
        self._max_tokens = max_tokens
        # 公开属性：main 里建好 describer 之后才拿得到 bot.save_emojis
        self.on_update = on_update
        self._sem = asyncio.Semaphore(concurrency)
        self._tasks: set[asyncio.Task] = set()

    def needs(self, emoji: Emoji | None) -> bool:
        """ 有热度（本群的梗）、还没写过场景、且没超过总量上限。 """
        return (emoji is not None and bool(emoji.md5) and emoji.count > 0
                and not emoji.scene and self._library.scene_count < self.scene_limit)

    def schedule(self, emoji: Emoji | None, download) -> None:
        """ download 为空（没图可下）就不做——宁可没场景，也不要编一个。 """
        if download is None or not self.needs(emoji):
            return
        task = asyncio.create_task(self._apply(emoji, download))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def join(self) -> None:
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def _apply(self, emoji: Emoji, download) -> None:
        # 场景只用来帮模型挑表情，出任何问题都不该影响聊天
        try:
            async with self._sem:
                image = await asyncio.wait_for(download(), self._timeout)
                if not image:
                    return
                text = await asyncio.wait_for(
                    self._ai.describe(image, max_tokens=self._max_tokens,
                                      prompt=EMOJI_DESCRIBE_PROMPT, model=self._model), self._timeout)
        except Exception as e:
            logger.warning(f'补表情场景失败 md5={emoji.md5}: {e}')
            return
        current = self._library.get(emoji.md5)
        desc, scene = parse_description(text or '')
        # 没按「情绪|场景」来就别记，宁可没场景
        if not scene or current is None:
            return
        # 微信给的标签比模型写的准，只填空缺
        self._library.record(replace(current, desc=current.desc or desc, scene=scene))
        logger.debug(f'表情场景 {emoji.md5} -> {desc}|{scene}')
        if self.on_update:
            self.on_update()


if __name__ == '__main__':
    class FakeAi:
        def __init__(self, reply='累到不行|加班到很晚、只想趴着的时候'):
            self.reply = reply
            self.prompts: list[str] = []

        async def describe(self, image, max_tokens=64, prompt='', model=None):
            self.prompts.append(prompt)
            return self.reply

    async def run():
        ai = FakeAi()
        library = EmojiLibrary()
        saved: list[int] = []
        describer = EmojiDescriber(ai, library, model='m', on_update=lambda: saved.append(1))

        # 只有本群用过的（有热度）才值得花一次识图
        assert not describer.needs(Emoji(md5='c' * 32, desc='摊倒'))
        hot = Emoji(md5='h' * 32, desc='摊倒', count=30)
        assert describer.needs(hot) and not describer.needs(None)
        library.record(hot)

        async def image():
            return b'png'

        describer.schedule(hot, None)               # 没图可下就不做
        assert not describer._tasks

        describer.schedule(hot, image)
        await describer.join()
        stored = library.get(hot.md5)
        assert stored.scene == '加班到很晚、只想趴着的时候'
        assert stored.desc == '摊倒'                 # 微信的标签不被模型覆盖
        assert saved == [1] and '使用场景' in ai.prompts[0]
        assert not describer.needs(stored)           # 补过的不再排

        # 没按格式返回 → 不记
        plain = EmojiLibrary()
        plain.record(hot)
        bad = EmojiDescriber(FakeAi('就是一张图'), plain, model='m')
        bad.schedule(hot, image)
        await bad.join()
        assert plain.get(hot.md5).scene == ''

        # 下载抛异常不能把消息链拖死
        broken = EmojiLibrary()
        broken.record(hot)

        async def boom():
            raise RuntimeError('daemon 挂了')

        safe = EmojiDescriber(FakeAi(), broken, model='m')
        safe.schedule(hot, boom)
        await safe.join()
        assert broken.get(hot.md5).scene == ''

        # 上限：够数了就不再补
        capped = EmojiLibrary()
        for i in range(3):
            capped.record(Emoji(md5=f'{i:032x}', desc='x', scene='y', cdnurl='u'))
        guard = EmojiDescriber(FakeAi(), capped, model='m', scene_limit=3)
        assert capped.scene_count == 3
        assert not guard.needs(Emoji(md5='z' * 32, count=5))

        await describer.stop()

    asyncio.run(run())
    print('emoji describer self-check ok')
