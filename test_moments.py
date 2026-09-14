""" 中期记忆自检：关键词提取、摘要解析、存储召回、滑出窗口触发压缩。 """

import asyncio
import tempfile
import unittest
from pathlib import Path

from context import ChatWindow
from moments import Moment, MomentKeeper, MomentStore, extract_keywords, parse_summary, transcript


class FakeAi:
    def __init__(self, response='{"summary":"大家在聊加班","cues":["提到加班时"]}'):
        self.response = response
        self.calls = []

    async def summarize(self, prompt, text, max_tokens=400):
        self.calls.append((prompt, text))
        return self.response


class FakeTombstone:
    """ 只记 response 为 None 的 AI。 """

    async def summarize(self, prompt, text, max_tokens=400):
        return None


class TestKeywordsAndParsing(unittest.TestCase):
    def test_extract_keywords_keeps_content_words(self):
        self.assertIn('上班', extract_keywords('我明天还要去上班，烦死了'))
        self.assertEqual(extract_keywords(''), [])
        self.assertNotIn('怎么', extract_keywords('这个怎么弄啊'))

    def test_parse_summary_tolerates_fences_and_garbage(self):
        self.assertEqual(parse_summary('{"summary":"在聊加班","cues":["提到加班时"]}'),
                         ('在聊加班', ('提到加班时',)))
        self.assertEqual(parse_summary('```json\n{"summary":"x"}\n```')[0], 'x')
        self.assertEqual(parse_summary('模型胡言乱语'), ('模型胡言乱语', ()))

    def test_transcript_marks_self(self):
        from context import SingleRecord
        text = transcript([SingleRecord('Alice', '在吗', 0),
                           SingleRecord('Bot', '在的', 0, is_self=True)])
        self.assertIn('Alice: 在吗', text)
        self.assertIn('我: 在的', text)


class TestMomentStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MomentStore(Path(self.tmp.name) / 'moments.db')

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_search_ranks_by_hits_and_isolates_chats(self):
        self.store.add(Moment('g', 1, 10, '大家在聊明天上班要不要请假', ('提到请假时',)))
        self.store.add(Moment('g', 11, 20, '讨论了周末去看电影', ('提到电影时',)))
        self.store.add(Moment('other', 1, 10, '别群的上班话题', ()))

        found = self.store.search('g', extract_keywords('明天上班怎么办'))
        self.assertTrue(found)
        self.assertIn('上班', found[0].summary)
        self.assertEqual(self.store.search('g', extract_keywords('完全不相关的内容')), [])
        self.assertEqual(self.store.search('g', []), [])
        self.assertEqual([m.summary for m in self.store.search('other', ['上班'])], ['别群的上班话题'])

    def test_last_end(self):
        self.assertEqual(self.store.last_end('g'), 0)
        self.store.add(Moment('g', 1, 20, 'x'))
        self.assertEqual(self.store.last_end('g'), 20)


class TestMomentKeeper(unittest.IsolatedAsyncioTestCase):
    async def test_compresses_when_batch_is_full(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MomentStore(Path(tmp) / 'm.db')
            ai = FakeAi()
            keeper = MomentKeeper(ai, store, batch=3)
            window = ChatWindow(max_history=3, on_evict=lambda record: keeper.schedule('g', record))

            for i in range(5):  # 第 4、5 条会把前两条挤出去，但仍不足一批
                window.append('Alice', f'msg{i}', i)
            await keeper.join()
            self.assertEqual(len(store), 0)

            window.append('Alice', 'msg5', 5)  # 攒够 3 条滑出
            await keeper.join()
            self.assertEqual(len(store), 1)
            self.assertEqual(store.search('g', ['加班'])[0].summary, '大家在聊加班')
            self.assertIn('msg0', ai.calls[0][1])
            store.close()

    async def test_skips_already_remembered_history(self):
        """ 重启后重新淘汰老消息，不应该重复记。 """
        with tempfile.TemporaryDirectory() as tmp:
            store = MomentStore(Path(tmp) / 'm.db')
            store.add(Moment('g', 0, 100, '旧记忆'))
            keeper = MomentKeeper(FakeAi(), store, batch=2)
            window = ChatWindow(max_history=1, on_evict=lambda record: keeper.schedule('g', record))

            window.append('Alice', 'a', 10)
            window.append('Alice', 'b', 20)  # 挤出 t=10，比 last_end 旧
            await keeper.join()
            self.assertEqual(len(store), 1)  # 没有新增
            store.close()

    async def test_silent_ai_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MomentStore(Path(tmp) / 'm.db')
            keeper = MomentKeeper(FakeTombstone(), store, batch=1)
            window = ChatWindow(max_history=1, on_evict=lambda record: keeper.schedule('g', record))
            window.append('Alice', 'a', 10)
            window.append('Alice', 'b', 20)
            await keeper.join()
            self.assertEqual(len(store), 0)
            store.close()

    async def test_ai_failure_does_not_raise(self):
        class Boom:
            async def summarize(self, prompt, text, max_tokens=400):
                raise RuntimeError('model down')

        with tempfile.TemporaryDirectory() as tmp:
            store = MomentStore(Path(tmp) / 'm.db')
            keeper = MomentKeeper(Boom(), store, batch=1)
            window = ChatWindow(max_history=1, on_evict=lambda record: keeper.schedule('g', record))
            window.append('Alice', 'a', 10)
            window.append('Alice', 'b', 20)
            await asyncio.wait_for(keeper.join(), timeout=5)  # 不抛异常
            self.assertEqual(len(store), 0)
            store.close()


if __name__ == '__main__':
    unittest.main()
