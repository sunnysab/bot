""" 群友画像自检：昵称归一化、加载、查找、prompt 渲染。 """

import json
import tempfile
import unittest
from pathlib import Path

from profiles import Profile, ProfileStore, normalize_name


class TestNormalizeName(unittest.TestCase):
    def test_strips_parenthetical_and_spaces(self):
        self.assertEqual(normalize_name('扶风（意向做上门女婿）'), '扶风')
        self.assertEqual(normalize_name('  sunnysab  '), 'sunnysab')
        self.assertEqual(normalize_name('普通人'), '普通人')


class TestProfileStore(unittest.TestCase):
    def setUp(self):
        self.store = ProfileStore(people={
            'wxid_a': Profile('wxid_a', names=('豆浆', '孔书贤'), avg_len=6.2, short_ratio=0.286,
                              laugh_ratio=0.05, phrases=('蛙趣', '我服了'), topics=('上班',),
                              samples=('这死班我真干不动了',)),
            'wxid_b': Profile('wxid_b', names=('扶风（意向做上门女婿）', '扶风'), avg_len=12.0, short_ratio=0.05),
        })

    def test_find_exact_normalized_and_missing(self):
        self.assertEqual(self.store.find('豆浆').wxid, 'wxid_a')
        self.assertEqual(self.store.find('扶风').wxid, 'wxid_b')
        self.assertEqual(self.store.find('扶风（打螺丝）').wxid, 'wxid_b')
        self.assertIsNone(self.store.find('查无此人'))
        self.assertIsNone(self.store.find(''))

    def test_render_for_dedups(self):
        block = self.store.render_for(['豆浆', '豆浆', '扶风', '查无此人'])
        self.assertEqual(block.count('【群友】'), 2)
        self.assertIn('口头禅：蛙趣、我服了', block)
        self.assertIn('说过："这死班我真干不动了"', block)

    def test_load_missing_file_returns_none(self):
        self.assertIsNone(ProfileStore.load('/no/such/file.json'))

    def test_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'profiles.json'
            path.write_text(json.dumps({'version': 1, 'people': {
                'wxid_a': {'names': ['豆浆'], 'avg_len': 6.2, 'short_ratio': 0.286, 'phrases': ['蛙趣']}}}),
                encoding='utf-8')
            store = ProfileStore.load(path)
            self.assertEqual(len(store), 1)
            self.assertEqual(store.find('豆浆').phrases, ('蛙趣',))


if __name__ == '__main__':
    unittest.main()
