""" MediaDescriber 自检：持久化缓存、并发合流、超时、回写历史记录。 """

import asyncio
import time
import unittest

from ai import IMAGE_PROMPT_VERSION
from context import SingleRecord
from describer import MediaCache, MediaDescriber
from media import Media


class FakeAi:
    def __init__(self, description='一只橘猫', delay=0.0):
        self.description = description
        self.delay = delay
        self.calls = 0
        self.last_max_tokens = None

    async def describe(self, image, max_tokens=64, prompt='', model=None):
        self.calls += 1
        self.last_max_tokens = max_tokens
        self.last_prompt = prompt
        await asyncio.sleep(self.delay)
        return self.description


class FakeMsg:
    def __init__(self, data=b'raw'):
        self.data = data
        self.downloads = 0

    async def get_media_bytes(self):
        self.downloads += 1
        return self.data


def make_record(md5='md5-1', kind='image', description=None, data=b'raw', t=1):
    """ 构造一条带媒体下载入口的历史记录。

    `t=1` = 两天前的老消息（默认，按已失效处理）；传 `int(time.time())` 就是刚发的。
    """
    msg = FakeMsg(data)
    media = Media(kind, md5, description)
    record = SingleRecord('Alice', media.placeholder, t, media=media,
                          download=msg.get_media_bytes if media.needs_description else None)
    return record, msg


class TestMediaDescriber(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ai = FakeAi()
        self.cache = MediaCache(':memory:')
        self.describer = MediaDescriber(self.ai, self.cache, model='test', concurrency=2, timeout=1.0)

    async def asyncTearDown(self):
        await self.describer.stop()

    async def test_describes_and_writes_back(self):
        record, _ = make_record('md5-1')
        self.describer.schedule(record)
        await self.describer.join()

        self.assertEqual(record.text, '[图片：一只橘猫]')
        self.assertEqual(self.cache.get('md5-1'), '一只橘猫')
        self.assertEqual(self.ai.last_max_tokens, 64)

    async def test_cache_hit_skips_model(self):
        self.cache.put('md5-2', 'image', '缓存里的猫', 'test', version=IMAGE_PROMPT_VERSION)
        record, msg = make_record('md5-2')
        self.describer.schedule(record)
        await self.describer.join()

        self.assertEqual(record.text, '[图片：缓存里的猫]')
        self.assertEqual(self.ai.calls, 0)
        self.assertEqual(msg.downloads, 0)

    async def test_image_uses_ocr_prompt_and_old_version_misses(self):
        # 老描述（15 字版，没写 version）不算命中 → 用新提示词重新描述
        self.cache.put('md5-7', 'image', '自助餐菜单丰盛令人期待', 'test')
        record, _ = make_record('md5-7')
        self.describer.schedule(record)
        await self.describer.join()

        self.assertEqual(self.ai.calls, 1)
        self.assertIn('原样抄下来', self.ai.last_prompt)
        self.assertEqual(self.cache.get('md5-7', IMAGE_PROMPT_VERSION), '一只橘猫')

    async def test_same_md5_is_described_once(self):
        pairs = [make_record('md5-3') for _ in range(3)]
        records = [record for record, _ in pairs]
        msgs = [msg for _, msg in pairs]
        for record in records:
            self.describer.schedule(record)
        await self.describer.join()

        self.assertEqual(self.ai.calls, 1)
        self.assertEqual(sum(msg.downloads for msg in msgs), 1)
        self.assertTrue(all(r.text == '[图片：一只橘猫]' for r in records))

    async def test_emoji_with_description_is_skipped(self):
        record, _ = make_record('md5-4', kind='emoji', description='摊倒')
        self.describer.schedule(record)
        await self.describer.join()

        self.assertEqual(self.ai.calls, 0)
        self.assertEqual(record.text, '[表情：摊倒]')

    async def test_download_failure_keeps_placeholder_and_caches_the_failure(self):
        """ 老消息取不到字节：保留占位符，并记 24 小时负缓存——图早被服务端回收了，
        不该每次重启都从头等一遍 describe_timeout。 """
        calls = []

        async def broken():
            calls.append(1)
            raise RuntimeError("缺少 'msg_id', 'url', 'path' 或 'filename' 参数")

        first = SingleRecord('Alice', '[图片]', 1, media=Media('image', 'md5-5'), download=broken)
        self.describer.schedule(first)
        await self.describer.join()
        self.assertEqual(first.text, '[图片]')
        self.assertEqual(len(calls), 1)

        now = time.time()
        self.assertTrue(self.cache.blocked('md5-5', IMAGE_PROMPT_VERSION, now + 23 * 3600))
        self.assertFalse(self.cache.blocked('md5-5', IMAGE_PROMPT_VERSION, now + 25 * 3600))

        second = SingleRecord('Alice', '[图片]', 2, media=Media('image', 'md5-5'), download=broken)
        self.describer.schedule(second)
        await self.describer.join()
        self.assertEqual(len(calls), 1)                 # 负缓存挡在下载之前
        self.assertEqual(second.text, '[图片]')

    async def test_fresh_image_failure_is_a_short_cooldown_not_a_day(self):
        """ 刚发的图取不到字节是这一侧的问题（RPC 报错、daemon 刚重启），不能当
        「图过期」静默一天：短冷却，而且下一轮还会再试。 """
        calls = []

        async def broken():
            calls.append(1)
            raise RuntimeError('[404] 未知操作: get_media_bytes')

        record = SingleRecord('Alice', '[图片]', int(time.time()),
                              media=Media('image', 'md5-13'), download=broken)
        self.describer.schedule(record)
        await self.describer.join()
        self.assertEqual(record.text, '[图片]')

        now = time.time()
        self.assertTrue(self.cache.blocked('md5-13', IMAGE_PROMPT_VERSION, now + 60))
        self.assertFalse(self.cache.blocked('md5-13', IMAGE_PROMPT_VERSION, now + 3600))

        # 同一张图在这 5 分钟里还是挡着：短冷却也是冷却
        again = SingleRecord('Alice', '[图片]', int(time.time()),
                             media=Media('image', 'md5-13'), download=broken)
        self.describer.schedule(again)
        await self.describer.join()
        self.assertEqual(len(calls), 1)

        # 冷却期一过（缓存里那条负缓存已经过期）就该真的重试，而不是等满一天
        self.cache.note_failure('md5-13', 'image', IMAGE_PROMPT_VERSION, now - 1)
        later = SingleRecord('Alice', '[图片]', int(time.time()),
                             media=Media('image', 'md5-13'), download=broken)
        self.describer.schedule(later)
        await self.describer.join()
        self.assertEqual(len(calls), 2)

    async def test_timeout_keeps_placeholder_and_caches_the_failure(self):
        ai = FakeAi(delay=0.2)
        cache = MediaCache(':memory:')
        describer = MediaDescriber(ai, cache, model='test', timeout=0.02)
        try:
            record, _ = make_record('md5-6')
            describer.schedule(record)
            await describer.join()
            self.assertEqual(record.text, '[图片]')
            self.assertTrue(cache.blocked('md5-6', IMAGE_PROMPT_VERSION, time.time()))

            other, _ = make_record('md5-6')
            describer.schedule(other)
            await describer.join()
            self.assertEqual(ai.calls, 1)               # 超时进负缓存，没有第二次调用
        finally:
            await describer.stop()

    async def test_description_wins_over_negative_cache(self):
        """ 已经有描述的图不该被历史失败记录挡住。 """
        self.cache.put('md5-12', 'image', '缓存里的猫', 'test', version=IMAGE_PROMPT_VERSION)
        self.cache.note_failure('md5-12', 'image', IMAGE_PROMPT_VERSION, time.time() + 3600)
        record, msg = make_record('md5-12')
        self.describer.schedule(record)
        await self.describer.join()

        self.assertEqual(record.text, '[图片：缓存里的猫]')
        self.assertEqual(msg.downloads, 0)

    async def test_wait_returns_when_description_ready(self):
        """ describe_wait 用：等到了就返回 True，记录已经写回描述。 """
        ai = FakeAi(delay=0.05)
        describer = MediaDescriber(ai, MediaCache(':memory:'), model='test', timeout=1.0)
        try:
            record, _ = make_record('md5-8')
            self.assertFalse(describer.pending(record))        # 还没 schedule
            describer.schedule(record)
            self.assertTrue(describer.pending(record))
            self.assertTrue(await describer.wait(record, 1.0))
            self.assertEqual(record.text, '[图片：一只橘猫]')
            self.assertFalse(describer.pending(record))        # 完成后不再挂着
        finally:
            await describer.stop()

    async def test_wait_timeout_leaves_describe_running(self):
        """ 等到点就走，但**不取消**识图：它跑完照样写回，下一轮看得到。 """
        ai = FakeAi(delay=0.05)
        describer = MediaDescriber(ai, MediaCache(':memory:'), model='test', timeout=1.0)
        try:
            record, _ = make_record('md5-9')
            describer.schedule(record)
            self.assertFalse(await describer.wait(record, 0.001))
            await describer.join()
            self.assertEqual(record.text, '[图片：一只橘猫]')
        finally:
            await describer.stop()


if __name__ == '__main__':
    unittest.main()
