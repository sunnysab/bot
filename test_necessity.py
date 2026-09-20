""" 必要性打分自检：@/提名/私聊直通，群聊按分数过线。 """

import unittest

from necessity import NECESSITY_THRESHOLD, NecessityInput, SpeechLimiter, score_necessity


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

    def test_request_passes_in_active_group(self):
        self.assertGreaterEqual(score('谁能帮我看看', activity=10).score, NECESSITY_THRESHOLD)

    def test_presence_penalty_suppresses_chatterbox(self):
        noisy = score('这个怎么弄啊', activity=10, self_replies=6, recent_messages=10)
        self.assertLess(noisy.score, NECESSITY_THRESHOLD)

    def test_media_placeholder_gains_nothing(self):
        # 媒体占位不算内容分，但热聊群的活跃度压力照旧（图片本身也值得评一句）
        self.assertEqual(score('[图片：一只橘猫]', activity=20).score,
                         score('一句平平无奇的话', activity=20).score)
        self.assertLess(score('[图片：一只橘猫]', activity=20).score, NECESSITY_THRESHOLD)

    def test_active_group_plain_chatter_stays_below_threshold(self):
        # 封顶 40 < 阈值 60：群热本身不再够格，闲话要说过线得有内容分
        self.assertLess(score('今天真冷', activity=10).score, NECESSITY_THRESHOLD)
        # 同一句闲话在更热的群里也一样（压力已封顶）
        self.assertLess(score('今天真冷', activity=30).score, NECESSITY_THRESHOLD)

    def test_score_never_negative(self):
        self.assertGreaterEqual(score('哈哈哈哈', activity=0, self_replies=9, recent_messages=10).score, 0)


class TestSpeechLimiter(unittest.TestCase):
    def test_min_gap_then_window(self):
        lim = SpeechLimiter(window=300.0, maximum=3, min_gap=45.0)
        self.assertEqual(lim.refusal('g', 0.0), '')           # 没说过
        lim.note('g', 0.0)
        self.assertIn('刚说过', lim.refusal('g', 10.0))
        self.assertEqual(lim.refusal('g', 45.0), '')          # 过了 min_gap
        lim.note('g', 45.0)
        lim.note('g', 100.0)
        self.assertIn('3 条', lim.refusal('g', 150.0))        # 窗口里说满了
        self.assertEqual(lim.refusal('g', 301.0), '')         # 第一条滑出窗口

    def test_each_contact_has_its_own_budget(self):
        lim = SpeechLimiter(window=300.0, maximum=1, min_gap=0.0)
        lim.note('a', 0.0)
        self.assertIn('已说', lim.refusal('a', 1.0))
        self.assertEqual(lim.refusal('b', 1.0), '')


if __name__ == '__main__':
    unittest.main()
