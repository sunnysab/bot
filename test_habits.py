""" 群用语自检：加载、命中匹配、渲染。 """

import json
import tempfile
import unittest
from pathlib import Path

from habits import Habit, HabitStore


class TestHabitStore(unittest.TestCase):
    def setUp(self):
        self.store = HabitStore(habits=(
            Habit('笑死', 371, ('笑死，你们说女生有二十多个',)),
            Habit('我服了', 240, ('我服了，洗完澡打算吹头',)),
            Habit('555', 566, ('怎么 sunny 哥哥秒接电话 555',)),
        ))

    def test_match_only_on_hit(self):
        self.assertEqual([h.phrase for h in self.store.match('这画面笑死我了')], ['笑死'])
        self.assertEqual(self.store.match('今天天气不错'), [])

    def test_longer_phrase_wins(self):
        store = HabitStore(habits=(Habit('我', 1), Habit('我服了', 1)))
        self.assertEqual(store.match('我服了')[0].phrase, '我服了')

    def test_render_is_empty_without_hit(self):
        self.assertEqual(self.store.render('平静的一句话'), '')
        block = self.store.render('我服了，又加班')
        self.assertIn('我服了', block)
        self.assertIn('我服了，洗完澡打算吹头', block)

    def test_match_limit(self):
        self.assertEqual(len(self.store.match('笑死 我服了 555', limit=2)), 2)

    def test_load_missing_file_returns_none(self):
        self.assertIsNone(HabitStore.load('/no/such/file.json'))

    def test_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'habits.json'
            path.write_text(json.dumps({'version': 1, 'habits': [
                {'phrase': '蛙趣', 'count': 126, 'samples': ['蛙趣 你的ID和小红书一样的吧']}]}), encoding='utf-8')
            store = HabitStore.load(path)
            self.assertEqual(len(store), 1)
            self.assertEqual(store.match('蛙趣，真的假的')[0].count, 126)


if __name__ == '__main__':
    unittest.main()
