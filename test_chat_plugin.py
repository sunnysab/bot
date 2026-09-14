""" 插件自检：两段式回复、必要性门控、模板注入、回复轨迹，以及复读机不跟媒体占位。 """

import base64
import json
import tempfile
import time
import types
import unittest
from pathlib import Path

from context import ChatWindow, MessageContext
from habits import Habit, HabitStore
from moments import Moment, MomentStore
from necessity import NECESSITY_THRESHOLD
from persona import Persona
from plugin import ChatPlugin, RepeatPlugin, ReplyPolicy
from profiles import Profile, ProfileStore
from reply_trace import TraceLogger

PLANNER_MARK = '决策者'          # planner.txt 里独有的字眼，用来区分两次调用


class FakeAi:
    """ Planner 和 Expressor 共用，用 prompt 里的标记区分。 """

    def __init__(self, reply=('收到',), plan='{"speak": true, "intent": "附和一句", "emotion": "无奈"}'):
        self.reply = list(reply) if reply else None
        self.plan = plan
        self.calls = []

    async def chat(self, prompt, message):
        self.calls.append((prompt, message))
        if PLANNER_MARK in prompt:
            return [self.plan] if self.plan else None
        return self.reply

    @property
    def planner_calls(self):
        return [call for call in self.calls if PLANNER_MARK in call[0]]

    @property
    def reply_calls(self):
        return [call for call in self.calls if PLANNER_MARK not in call[0]]


def make_msg(msg_type=1, is_chatroom=False, is_at_me=False, media=b'', content=''):
    msg = types.SimpleNamespace(msg_type=msg_type, is_chatroom=is_chatroom, is_at_me=is_at_me,
                                content=content, sender_name='Alice', reply_target='456@chatroom')

    async def get_media_bytes():
        return media

    msg.get_media_bytes = get_media_bytes
    return msg


def sample_history():
    window = ChatWindow()
    window.append('Alice', '在吗', 1)
    return window


def make_ctx(msg, history=None):
    history = history if history is not None else sample_history()
    return MessageContext(msg=msg, history=history, self_name='Bot', contact='测试群')


def history_with(others: int, now: float) -> ChatWindow:
    """ 构造近段时间有 others 条别人消息的窗口。 """
    window = ChatWindow()
    for i in range(others):
        window.append('Alice', f'msg{i}', int(now - i))
    return window


class TestChatPlugin(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ai = FakeAi()
        self.plugin = ChatPlugin(self.ai, context_length=10)

    def proactive(self, content='这个怎么弄啊'):
        """ 只有「没人点名的热聊群」才会走决策者。 """
        return make_ctx(make_msg(is_chatroom=True, content=content), history=history_with(12, now=time.time()))

    async def test_group_plain_message_is_silent(self):
        reply = await self.plugin.handle(make_ctx(make_msg(is_chatroom=True, content='大家好啊')))
        self.assertIsNone(reply)
        self.assertEqual(self.ai.calls, [])

    async def test_active_group_question_goes_through_planner(self):
        reply = await self.plugin.handle(self.proactive())
        self.assertEqual(reply.texts, ['收到'])
        self.assertEqual(len(self.ai.planner_calls), 1)
        self.assertEqual(len(self.ai.reply_calls), 1)

    async def test_direct_address_bypasses_planner(self):
        """ 被 @ / 叫名字 / 私聊是必须回应的，不多花一次调用。 """
        for msg in (make_msg(is_chatroom=True, is_at_me=True),
                    make_msg(is_chatroom=True, content='Bot 在吗'),
                    make_msg(is_chatroom=False)):
            self.ai.calls.clear()
            reply = await self.plugin.handle(make_ctx(msg))
            self.assertEqual(reply.texts, ['收到'])
            self.assertEqual(self.ai.planner_calls, [])
            self.assertEqual(len(self.ai.reply_calls), 1)

    async def test_plan_is_passed_to_expressor(self):
        await self.plugin.handle(self.proactive())
        message = self.ai.reply_calls[0][1]
        self.assertIn('【本轮】', message)
        self.assertIn('【决策】', message)
        self.assertIn('想表达：附和一句', message)
        self.assertIn('情绪：无奈', message)
        self.assertIn('msg11', message)         # 决策之后跟着原始聊天记录

    async def test_planner_is_told_it_is_not_the_bot(self):
        await self.plugin.handle(self.proactive())
        self.assertIn('不是 Bot 本人', self.ai.planner_calls[0][0])

    async def test_silent_plan_skips_expressor(self):
        self.plugin.ai = FakeAi(plan='{"speak": false, "analysis": "接不上话"}')
        reply = await self.plugin.handle(self.proactive())
        self.assertEqual(reply.texts, [])
        self.assertTrue(reply.stop)
        self.assertEqual(len(self.plugin.ai.planner_calls), 1)
        self.assertEqual(self.plugin.ai.reply_calls, [])   # 不再多跑一次表达

    async def test_planner_disabled_falls_back_to_single_call(self):
        plugin = ChatPlugin(self.ai, context_length=10, enable_planner=False)
        reply = await plugin.handle(self.proactive())
        self.assertEqual(reply.texts, ['收到'])
        self.assertEqual(len(self.ai.calls), 1)
        self.assertIn('你的名字是', self.ai.calls[0][0])

    async def test_planner_can_use_its_own_model(self):
        """ 决策者可以走本地便宜模型，表达者仍用主模型。 """
        planner = FakeAi()
        plugin = ChatPlugin(self.ai, context_length=10, planner_ai=planner)
        await plugin.handle(self.proactive())
        self.assertEqual(len(planner.calls), 1)
        self.assertIn(PLANNER_MARK, planner.calls[0][0])
        self.assertEqual(len(self.ai.calls), 1)
        self.assertNotIn(PLANNER_MARK, self.ai.calls[0][0])

    async def test_planner_failure_does_not_mute_the_bot(self):
        """ 决策者挂了就退回单段，不能因此变吧。 """
        self.plugin.ai = FakeAi(plan=None)
        reply = await self.plugin.handle(self.proactive())
        self.assertEqual(reply.texts, ['收到'])
        self.assertNotIn('【决策】', self.plugin.ai.reply_calls[0][1])

    async def test_silent_model_does_not_reply(self):
        self.plugin.ai = FakeAi(reply=None)
        reply = await self.plugin.handle(make_ctx(make_msg(is_chatroom=False)))
        self.assertEqual(reply.texts, [])
        self.assertTrue(reply.stop)

    async def test_alternate_style_uses_own_expression(self):
        import random
        persona = Persona(alternates=('用1-2个字回复',), alternate_probability=1.0)
        plugin = ChatPlugin(self.ai, context_length=10, persona=persona, rng=random.Random(0))
        await plugin.handle(make_ctx(make_msg(is_chatroom=False)))
        # 临时风格是「这一轮」的事，放请求里，不进 system prompt
        self.assertIn('【临时风格】用1-2个字回复', self.ai.reply_calls[0][1])
        self.assertNotIn('用1-2个字回复', self.ai.reply_calls[0][0])

    async def test_personality_and_style_come_from_config(self):
        persona = Persona(personality='是一只猫', reply_style='只会喵')
        plugin = ChatPlugin(self.ai, context_length=10, persona=persona)
        await plugin.handle(make_ctx(make_msg(is_chatroom=False)))
        prompt = self.ai.reply_calls[0][0]
        self.assertIn('你的名字是Bot。是一只猫', prompt)
        self.assertIn('只会喵', prompt)

    async def test_image_is_sent_as_content_block(self):
        image = b'\xff\xd8fake-jpeg'
        reply = await self.plugin.handle(make_ctx(make_msg(msg_type=3, is_chatroom=False, media=image)))
        self.assertEqual(reply.texts, ['收到'])
        content = self.ai.reply_calls[0][1]
        self.assertIsInstance(content, list)
        self.assertEqual(content[0]['type'], 'text')
        self.assertEqual(content[1]['type'], 'image_url')
        expected = 'data:image/jpeg;base64,' + base64.b64encode(image).decode()
        self.assertEqual(content[1]['image_url']['url'], expected)

    async def test_profiles_are_injected_into_request(self):
        store = ProfileStore(people={'wxid_x': Profile('wxid_x', names=('Alice',), phrases=('蛙趣',))})
        self.plugin.profiles = store
        await self.plugin.handle(self.proactive())
        self.assertIn('【群友画像】', self.ai.planner_calls[0][1])
        self.assertIn('口头禅：蛙趣', self.ai.planner_calls[0][1])

    async def test_habits_injected_only_on_hit(self):
        self.plugin.habits = HabitStore(habits=(Habit('蛙趣', 126, ('蛙趣 你的ID和小红书一样的吧',)),))

        await self.plugin.handle(self.proactive('蛙趣，真的假的'))
        self.assertIn('你的ID和小红书', self.ai.planner_calls[0][1])
        self.assertIn('你的ID和小红书', self.ai.reply_calls[0][1])   # 表达者也要知道群里怎么说

        await self.plugin.handle(self.proactive('平平无奇的一句话'))
        self.assertNotIn('群里的说法', self.ai.planner_calls[1][1])

    async def test_memories_injected_on_keyword_hit(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MomentStore(Path(tmp) / 'm.db')
            store.add(Moment('456@chatroom', 1, 10, '上次大家在群里讨论过明天要不要上班请假', ('提到请假时',)))
            self.plugin.memories = store

            await self.plugin.handle(self.proactive('明天上班好烦'))
            self.assertIn('【记忆', self.ai.planner_calls[0][1])
            self.assertIn('要不要上班请假', self.ai.planner_calls[0][1])

            await self.plugin.handle(self.proactive('吃了没'))
            self.assertNotIn('【记忆', self.ai.planner_calls[1][1])
            store.close()

    async def test_system_prompt_is_stable_across_turns(self):
        """ 会变的东西（画像/群用语/临时风格）不能进 system prompt。

        实测：这样并不提高 KV 缓存命中（前缀匹配本来就能命中静态前缀，128 对 128）。
        但 system 只装人设和规则、请求只装本轮数据，职责更清楚，也不会因为
        某个供应商改成「整段 system 才缓存」就失效。
        """
        self.plugin.profiles = ProfileStore(people={'wxid_x': Profile('wxid_x', names=('Alice',), phrases=('蛙趣',))})
        self.plugin.habits = HabitStore(habits=(Habit('蛙趣', 126, ('蛙趣 你的ID和小红书一样的吧',)),))

        await self.plugin.handle(self.proactive('蛙趣，真的假的'))
        await self.plugin.handle(self.proactive('平平无奇的一句话'))

        self.assertEqual(self.ai.planner_calls[0][0], self.ai.planner_calls[1][0])
        self.assertEqual(self.ai.reply_calls[0][0], self.ai.reply_calls[1][0])
        self.assertNotIn('你的ID和小红书', self.ai.planner_calls[0][0])
        self.assertNotIn('口头禅', self.ai.reply_calls[0][0])

    async def test_model_failure_does_not_break_the_round(self):
        """ 一次 API 抽风不能把消息处理链拖死。 """
        class Boom:
            async def chat(self, prompt, message):
                raise RuntimeError('api down')

        plugin = ChatPlugin(Boom(), context_length=10)
        reply = await plugin.handle(make_ctx(make_msg(is_chatroom=False)))
        self.assertEqual(reply.texts, [])
        self.assertTrue(reply.stop)

    async def test_tracer_records_the_round(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'trace.jsonl'
            self.plugin.tracer = TraceLogger(path)
            await self.plugin.handle(self.proactive())
            record = json.loads(path.read_text(encoding='utf-8').strip())

        self.assertEqual(record['trigger'], 'proactive')
        self.assertEqual(record['reply'], ['收到'])
        self.assertEqual(record['sender'], 'Alice')
        self.assertIn('msg11', record['transcript'])
        self.assertIn('想表达：附和一句', record['plan'])
        self.assertGreaterEqual(record['latency_ms'], 0)

    async def test_tracer_records_direct_address_without_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'trace.jsonl'
            self.plugin.tracer = TraceLogger(path)
            await self.plugin.handle(make_ctx(make_msg(is_chatroom=False)))
            record = json.loads(path.read_text(encoding='utf-8').strip())

        self.assertEqual(record['trigger'], 'private')
        self.assertEqual(record['plan'], '')      # 没走决策者

    async def test_tracer_records_silent_turns(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'trace.jsonl'
            self.plugin.ai = FakeAi(plan='{"speak": false, "analysis": "接不上"}')
            self.plugin.tracer = TraceLogger(path)
            await self.plugin.handle(self.proactive())
            record = json.loads(path.read_text(encoding='utf-8').strip())

        self.assertEqual(record['reply'], [])   # 决策者选择不发言，也要留痕
        self.assertEqual(record['plan'], '')


class TestReplyPolicy(unittest.TestCase):
    def setUp(self):
        self.policy = ReplyPolicy(threshold=NECESSITY_THRESHOLD, activity_window=180.0)

    def ctx(self, is_chatroom=True, is_at_me=False, content='', history=None):
        history = history if history is not None else ChatWindow()
        return make_ctx(make_msg(is_chatroom=is_chatroom, is_at_me=is_at_me, content=content), history=history)

    def test_private_triggers(self):
        self.assertIsNotNone(self.policy.evaluate(self.ctx(is_chatroom=False), now=1000.0))

    def test_at_and_mention_trigger(self):
        self.assertEqual(self.policy.evaluate(self.ctx(is_at_me=True), now=1000.0).kind, 'at')
        self.assertEqual(self.policy.evaluate(self.ctx(content='Bot 在吗'), now=1000.0).kind, 'mention')

    def test_quiet_group_is_silent(self):
        self.assertIsNone(self.policy.evaluate(self.ctx(content='大家好啊'), now=1000.0))

    def test_active_group_question_triggers(self):
        history = history_with(12, now=1000.0)
        result = self.policy.evaluate(self.ctx(content='这个怎么弄啊', history=history), now=1000.0)
        self.assertIsNotNone(result)
        self.assertEqual(result.kind, 'proactive')

    def test_self_chatter_suppresses_trigger(self):
        history = ChatWindow()
        for i in range(6):
            history.append('Bot', f'我说的{i}', int(1000 - i), is_self=True)
        for i in range(4):
            history.append(f'用户{i}', '别人说的', int(1000 - i))
        # 活跃度只数别人的消息；存在感 6/10 扣满，把分数压回阈值下
        self.assertIsNone(self.policy.evaluate(self.ctx(content='这个怎么弄啊', history=history), now=1000.0))


class TestRepeatPlugin(unittest.IsolatedAsyncioTestCase):
    async def test_media_markers_are_not_repeated(self):
        window = ChatWindow()
        window.append('Alice', '[表情]', 1)
        window.append('Bob', '[表情]', 2)
        window.append('Alice', '在吗', 3)
        reply = await RepeatPlugin().handle(make_ctx(make_msg(), history=window))
        self.assertIsNone(reply)

    async def test_repeats_and_stops(self):
        window = ChatWindow()
        window.append('Alice', '牛啊', 1)
        window.append('Bob', '牛啊', 2)
        reply = await RepeatPlugin().handle(make_ctx(make_msg(), history=window))
        self.assertEqual(reply.texts, ['牛啊'])
        self.assertTrue(reply.stop)


if __name__ == '__main__':
    unittest.main()
