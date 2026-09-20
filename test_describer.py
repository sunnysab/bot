""" MediaDescriber 自检：持久化缓存、并发合流、超时、回写历史记录。 """

import asyncio
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

    async def describe(self, image, max_tokens=64, prompt=''):
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


def make_record(md5='md5-1', kind='image', description=None, data=b'raw'):
    """ 构造一条带媒体下载入口的历史记录。 """
    msg = FakeMsg(data)
    media = Media(kind, md5, description)
    record = SingleRecord('Alice', media.placeholder, 1, media=media,
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

    async def test_download_failure_keeps_placeholder(self):
        record = SingleRecord('Alice', '[图片]', 1, media=Media('image', 'md5-5'))

        async def broken():
            raise RuntimeError('daemon down')

        record.download = broken
        self.describer.schedule(record)
        await self.describer.join()

        self.assertEqual(record.text, '[图片]')

    async def test_timeout_keeps_placeholder(self):
        ai = FakeAi(delay=0.2)
        describer = MediaDescriber(ai, MediaCache(':memory:'), model='test', timeout=0.02)
        try:
            record, _ = make_record('md5-6')
            describer.schedule(record)
            await describer.join()
            self.assertEqual(record.text, '[图片]')
        finally:
            await describer.stop()


if __name__ == '__main__':
    unittest.main()
