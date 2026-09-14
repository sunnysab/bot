""" MediaDescriber 自检：持久化缓存、并发合流、回写历史记录。 """

import asyncio
import unittest

from context import SingleRecord
from describer import MediaCache, MediaDescriber
from media import Media


class FakeAi:
    def __init__(self, description='一只橘猫'):
        self.description = description
        self.calls = 0

    async def describe(self, image):
        self.calls += 1
        await asyncio.sleep(0)  # 让并发合流有机会生效
        return self.description


class FakeMsg:
    def __init__(self, data=b'raw'):
        self.data = data
        self.downloads = 0

    async def get_media_bytes(self):
        self.downloads += 1
        return self.data


class TestMediaDescriber(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ai = FakeAi()
        self.cache = MediaCache(':memory:')
        self.describer = MediaDescriber(self.ai, self.cache, model='test', concurrency=2)

    async def asyncTearDown(self):
        await self.describer.stop()

    async def test_describes_and_writes_back(self):
        record = SingleRecord('Alice', '[图片]', 1)
        self.describer.schedule(Media('image', 'md5-1'), FakeMsg(), record)
        await self.describer.join()

        self.assertEqual(record.text, '[图片：一只橘猫]')
        self.assertEqual(self.cache.get('md5-1'), '一只橘猫')

    async def test_cache_hit_skips_model(self):
        self.cache.put('md5-2', 'image', '缓存里的猫', 'test')
        record = SingleRecord('Alice', '[图片]', 1)
        self.describer.schedule(Media('image', 'md5-2'), FakeMsg(), record)
        await self.describer.join()

        self.assertEqual(record.text, '[图片：缓存里的猫]')
        self.assertEqual(self.ai.calls, 0)

    async def test_same_md5_is_described_once(self):
        records = [SingleRecord('Alice', '[图片]', i) for i in range(3)]
        msg = FakeMsg()
        for record in records:
            self.describer.schedule(Media('image', 'md5-3'), msg, record)
        await self.describer.join()

        self.assertEqual(self.ai.calls, 1)
        self.assertEqual(msg.downloads, 1)
        self.assertTrue(all(r.text == '[图片：一只橘猫]' for r in records))

    async def test_emoji_with_description_is_skipped(self):
        record = SingleRecord('Alice', '[表情：摊倒]', 1)
        self.describer.schedule(Media('emoji', 'md5-4', description='摊倒'), FakeMsg(), record)
        await self.describer.join()

        self.assertEqual(self.ai.calls, 0)
        self.assertEqual(record.text, '[表情：摊倒]')

    async def test_download_failure_keeps_placeholder(self):
        class BrokenMsg:
            async def get_media_bytes(self):
                raise RuntimeError('relay down')

        record = SingleRecord('Alice', '[图片]', 1)
        self.describer.schedule(Media('image', 'md5-5'), BrokenMsg(), record)
        await self.describer.join()

        self.assertEqual(record.text, '[图片]')


if __name__ == '__main__':
    unittest.main()
