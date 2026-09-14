""" 必要性打分自检：@/提名/私聊直通，群聊按分数过线。 """

import unittest

from necessity import NECESSITY_THRESHOLD, NecessityInput, score_necessity


def score(text, **kwargs):
    return score_necessity(NecessityInput(text=text, is_group=True, has_at=False, has_mention=False, **kwargs))


class TestNecessity(unittest.TestCase):
    def test_at_mention_private_always_trigger(self):
        self.assertEqual(score_necessity(NecessityInput('在吗', True, True, False)).kind, 'at')
        self.assertEqual(score_necessity(NecessityInput('在吗', True, False, True)).kind, 'mention')
        self.assertEqual(score_necessity(NecessityInput('在吗', False, False, False)).kind, 'private')

    def test_quiet_group_plain_message_is_below_threshold(self):
        self.assertLess(score('大家好啊', activity=1).score, NECESSITY_THRESHOLD)

    def test_active_group_question_passes(self):
        self.assertGreaterEqual(score('这个怎么弄啊', activity=10).score, NECESSITY_THRESHOLD)

    def test_short_reaction_never_passes(self):
        self.assertLess(score('哈哈哈哈', activity=20).score, NECESSITY_THRESHOLD)

    def test_request_passes_in_moderately_active_group(self):
        self.assertGreaterEqual(score('谁能帮我看看', activity=8).score, NECESSITY_THRESHOLD)

    def test_presence_penalty_suppresses_chatterbox(self):
        noisy = score('这个怎么弄啊', activity=10, self_replies=6, recent_messages=10)
        self.assertLess(noisy.score, NECESSITY_THRESHOLD)

    def test_media_placeholder_gains_nothing(self):
        self.assertLess(score('[图片：一只橘猫]', activity=20).score, NECESSITY_THRESHOLD)

    def test_score_never_negative(self):
        self.assertGreaterEqual(score('哈哈哈哈', activity=0, self_replies=9, recent_messages=10).score, 0)


if __name__ == '__main__':
    unittest.main()
