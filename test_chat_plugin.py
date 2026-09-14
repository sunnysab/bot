""" 插件自检：两段式回复、必要性门控、模板注入、回复轨迹，以及复读机不跟媒体占位。 """

import base64
import json
import tempfile
import time
import types
import unittest
from pathlib import Path

from context import ChatWindow, MessageContext
from emojis import Emoji
from habits import Habit, HabitStore
from moments import Moment, MomentStore
from necessity import NECESSITY_THRESHOLD
from persona import Persona
from plugin import ChatPlugin, RepeatPlugin, ReplyPolicy, clean_reply
from profiles import Profile, ProfileStore
from reply_trace import TraceLogger
from tools import AssistantTurn, ToolCall

PLANNER_MARK = '决策者'          # planner.txt 里独有的字眼，用来区分两次调用


class FakeAi:
    """ Planner 和 Expressor 共用，用 prompt 里的标记区分。 """

    supports_tools = False      # 不支持工具的假后端；工具路径另外用 ToolAi 测

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


def _call(name, **arguments):
    return ToolCall(call_id=f'{name}-1', name=name, arguments=arguments)


def _emoji(md5='a' * 32, desc='无语', scene='别人说了很离谱的话', count=0):
    return Emoji(md5=md5, desc=desc, scene=scene, cdnurl='http://cdn/x', aeskey='k', count=count)


class ToolAi:
    """ 会用工具的假决策者：turns 是每轮依次返回的东西。 """

    supports_tools = True

    def __init__(self, turns=(), reply=('收到',)):
        self.turns = list(turns)
        self.reply = list(reply) if reply else None
        self.rounds = []            # 每次 chat_tools 收到的 messages
        self.tools_seen = []        # 每次暴露的工具名
        self.reply_calls = []

    async def chat_tools(self, prompt, messages, tools):
        self.rounds.append([dict(item) for item in messages])
        self.tools_seen.append([item['function']['name'] for item in tools])
        if not self.turns:
            return AssistantTurn()
        return self.turns.pop(0)

    async def chat(self, prompt, message):
        self.reply_calls.append((prompt, message))
        return self.reply


class FakeEmojiSearch:
    """ 假的表情搜索：关键词到结果的映射，没配的关键词返回空。 """

    def __init__(self, table=None):
        self.table = table or {}
        self.queries = []

    async def __call__(self, keyword):
        self.queries.append(keyword)
        return list(self.table.get(keyword, []))


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


def proactive_ctx(content='这个怎么弄啊'):
    """ 只有「没人点名的热聊群」才会走决策者。 """
    return make_ctx(make_msg(is_chatroom=True, content=content), history=history_with(12, now=time.time()))


class TestChatPlugin(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ai = FakeAi()
        self.plugin = ChatPlugin(self.ai, context_length=10)

    def proactive(self, content='这个怎么弄啊'):
        return proactive_ctx(content)

    async def test_group_plain_message_is_silent(self):
        reply = await self.plugin.handle(make_ctx(make_msg(is_chatroom=True, content='大家好啊')))
        self.assertIsNone(reply)
        self.assertEqual(self.ai.calls, [])

    async def test_active_group_question_goes_through_planner(self):
        reply = await self.plugin.handle(proactive_ctx())
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
        await self.plugin.handle(proactive_ctx())
        message = self.ai.reply_calls[0][1]
        self.assertIn('【本轮】', message)
        self.assertIn('【决策】', message)
        self.assertIn('想表达：附和一句', message)
        self.assertIn('情绪：无奈', message)
        self.assertIn('msg11', message)         # 决策之后跟着原始聊天记录

    async def test_planner_is_told_it_is_not_the_bot(self):
        await self.plugin.handle(proactive_ctx())
        self.assertIn('不是 Bot 本人', self.ai.planner_calls[0][0])

    async def test_silent_plan_skips_expressor(self):
        self.plugin.ai = FakeAi(plan='{"speak": false, "analysis": "接不上话"}')
        reply = await self.plugin.handle(proactive_ctx())
        self.assertEqual(reply.texts, [])
        self.assertTrue(reply.stop)
        self.assertEqual(len(self.plugin.ai.planner_calls), 1)
        self.assertEqual(self.plugin.ai.reply_calls, [])   # 不再多跑一次表达

    async def test_planner_disabled_falls_back_to_single_call(self):
        plugin = ChatPlugin(self.ai, context_length=10, enable_planner=False)
        reply = await plugin.handle(proactive_ctx())
        self.assertEqual(reply.texts, ['收到'])
        self.assertEqual(len(self.ai.calls), 1)
        self.assertIn('你的名字是', self.ai.calls[0][0])

    async def test_planner_can_use_its_own_model(self):
        """ 决策者可以走本地便宜模型，表达者仍用主模型。 """
        planner = FakeAi()
        plugin = ChatPlugin(self.ai, context_length=10, planner_ai=planner)
        await plugin.handle(proactive_ctx())
        self.assertEqual(len(planner.calls), 1)
        self.assertIn(PLANNER_MARK, planner.calls[0][0])
        self.assertEqual(len(self.ai.calls), 1)
        self.assertNotIn(PLANNER_MARK, self.ai.calls[0][0])

    async def test_planner_failure_does_not_mute_the_bot(self):
        """ 决策者挂了就退回单段，不能因此变吧。 """
        self.plugin.ai = FakeAi(plan=None)
        reply = await self.plugin.handle(proactive_ctx())
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
        self.assertIn('你的名字是Bot', prompt)
        self.assertIn('是一只猫', prompt)
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
        await self.plugin.handle(proactive_ctx())
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

    async def test_alias_counts_as_being_called(self):
        """ 群里叫「雨姐」也算在叫它：直接回应，不走决策者。 """
        plugin = ChatPlugin(self.ai, context_length=10, policy=ReplyPolicy(aliases=('雨姐',)))
        reply = await plugin.handle(make_ctx(make_msg(is_chatroom=True, content='雨姐在吗')))
        self.assertEqual(reply.texts, ['收到'])
        self.assertEqual(self.ai.planner_calls, [])

    async def test_aliases_are_told_to_both_models(self):
        persona = Persona(aliases=('雨姐', '烟姐'))
        plugin = ChatPlugin(self.ai, context_length=10, persona=persona)

        await plugin.handle(make_ctx(make_msg(is_chatroom=False)))
        self.assertIn('也有人叫你雨姐、烟姐', self.ai.reply_calls[0][0])

        await plugin.handle(proactive_ctx())
        self.assertIn('雨姐', self.ai.planner_calls[0][0])

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
            await self.plugin.handle(proactive_ctx())
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
            await self.plugin.handle(proactive_ctx())
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


class TestIdleBackoffInPlugin(unittest.IsolatedAsyncioTestCase):
    """ 连着几轮没开口就别再每条都花一次决策模型调用。 """

    def silent_plugin(self):
        self.ai = FakeAi(plan='{"speak": false, "analysis": "没什么可说的"}')
        return ChatPlugin(self.ai, context_length=10)

    async def test_silent_rounds_stop_calling_the_planner(self):
        plugin = self.silent_plugin()
        for _ in range(2):
            self.assertEqual((await plugin.handle(proactive_ctx())).texts, [])
        self.assertEqual(len(self.ai.planner_calls), 2)
        # 第三轮起直接被门控拦下，模型那一步都不走
        self.assertIsNone(await plugin.handle(proactive_ctx()))
        self.assertEqual(len(self.ai.planner_calls), 2)

    async def test_a_question_still_gets_scored_first(self):
        """ 退避是加在「过线之后」，不是绕过打分。 """
        plugin = self.silent_plugin()
        for _ in range(3):
            await plugin.handle(proactive_ctx())
        # 冷清群里说话，本来就不够分，跟退避无关
        quiet = make_ctx(make_msg(is_chatroom=True, content='今天天气不错'), history=history_with(1, now=time.time()))
        self.assertIsNone(await plugin.handle(quiet))

    async def test_at_mention_is_never_backed_off(self):
        plugin = self.silent_plugin()
        for _ in range(3):
            await plugin.handle(proactive_ctx())
        reply = await plugin.handle(make_ctx(make_msg(is_chatroom=True, is_at_me=True)))
        self.assertEqual(reply.texts, ['收到'])
        self.assertEqual(len(self.ai.reply_calls), 1)

    async def test_replying_clears_the_backoff(self):
        plugin = self.silent_plugin()
        for _ in range(2):
            await plugin.handle(proactive_ctx())
        self.assertIsNone(await plugin.handle(proactive_ctx()))     # 退避中，模型都不调
        planned = len(self.ai.planner_calls)
        # 被点名回了一句 = 群里还需要它，退避清零
        await plugin.handle(make_ctx(make_msg(is_chatroom=True, is_at_me=True)))
        await plugin.handle(proactive_ctx())
        self.assertEqual(len(self.ai.planner_calls), planned + 1)


class TestToolLoop(unittest.IsolatedAsyncioTestCase):
    """ 决策者用工具：搜表情 → 选一张 → （顺带）说要说话。 """

    def plugin(self, turns, search=None, **kwargs):
        self.ai = ToolAi(turns)
        self.search = FakeEmojiSearch(search or {})
        return ChatPlugin(self.ai, context_length=10, emoji_search=self.search, **kwargs)

    async def test_speak_only_is_one_round(self):
        plugin = self.plugin([AssistantTurn(content='在吐槽加班',
                                            tool_calls=(_call('speak', intent='附和一句', emotion='无奈'),))])
        reply = await plugin.handle(proactive_ctx())
        self.assertEqual(reply.texts, ['收到'])
        self.assertIsNone(reply.emoji)
        self.assertEqual(len(self.ai.rounds), 1)
        # 决策进决策块，表达者能看见
        self.assertIn('想表达：附和一句', self.ai.reply_calls[0][1])

    async def test_search_then_send_sends_only_the_sticker(self):
        plugin = self.plugin(
            [AssistantTurn(tool_calls=(_call('search_emoji', keyword='无语'),)),
             AssistantTurn(tool_calls=(_call('send_emoji', index=2),))],
            search={'无语': [_emoji(md5='1' * 32, desc='无语'),
                            _emoji(md5='2' * 32, desc='翻白眼', count=30)]})
        reply = await plugin.handle(proactive_ctx())
        self.assertEqual(self.search.queries, ['无语'])
        self.assertEqual(reply.emoji.md5, '2' * 32)
        # 只发表情不说话：不叫表达者
        self.assertEqual(reply.texts, [])
        self.assertEqual(self.ai.reply_calls, [])

    async def test_sticker_and_text_in_one_turn(self):
        plugin = self.plugin(
            [AssistantTurn(tool_calls=(_call('search_emoji', keyword='狗头'),)),
             AssistantTurn(tool_calls=(_call('send_emoji', index=1),
                                       _call('speak', intent='补一句')))],
            search={'狗头': [_emoji(desc='狗头')]})
        reply = await plugin.handle(proactive_ctx())
        self.assertEqual(reply.texts, ['收到'])
        self.assertEqual(reply.emoji.desc, '狗头')
        # 表达者要知道同时发了表情，免得文字重复那个情绪
        self.assertIn('同时会发一张表情：狗头', self.ai.reply_calls[0][1])

    async def test_no_tool_call_means_silent(self):
        plugin = self.plugin([AssistantTurn(content='接不上话')])
        reply = await plugin.handle(proactive_ctx())
        self.assertEqual(reply.texts, [])
        self.assertTrue(reply.stop)
        self.assertEqual(self.ai.reply_calls, [])

    async def test_search_result_is_shown_as_text(self):
        plugin = self.plugin(
            [AssistantTurn(tool_calls=(_call('search_emoji', keyword='无语'),)),
             AssistantTurn()],
            search={'无语': [_emoji(desc='无语', scene='别人说了很离谱的话', count=30)]})
        await plugin.handle(proactive_ctx())
        # 第二轮里模型看得到候选：序号 + 情绪 + 场景 + 热度
        transcript = json.dumps(self.ai.rounds[1], ensure_ascii=False)
        self.assertIn('无语｜别人说了很离谱的话', transcript)
        self.assertIn('群里常用', transcript)

    async def test_tool_protocol_is_kept_between_rounds(self):
        """ function calling 要求 assistant 的 tool_calls 后面必须跟同 id 的 tool 结果。 """
        plugin = self.plugin(
            [AssistantTurn(tool_calls=(_call('search_emoji', keyword='无语'),)),
             AssistantTurn(tool_calls=(_call('send_emoji', index=1),))],
            search={'无语': [_emoji()]})
        await plugin.handle(proactive_ctx())
        second = self.ai.rounds[1]
        self.assertEqual([item['role'] for item in second], ['user', 'assistant', 'tool'])
        self.assertEqual(second[2]['tool_call_id'], second[1]['tool_calls'][0]['id'])

    async def test_send_without_search_is_refused(self):
        plugin = self.plugin([AssistantTurn(tool_calls=(_call('send_emoji', index=1),))])
        reply = await plugin.handle(proactive_ctx())
        self.assertIsNone(reply.emoji)
        # 第 1 轮的工具结果（在下一轮的消息里）告诉模型要先搜
        self.assertIn('还没搜过', json.dumps(self.ai.rounds[1], ensure_ascii=False))

    async def test_index_out_of_range_is_refused(self):
        plugin = self.plugin(
            [AssistantTurn(tool_calls=(_call('search_emoji', keyword='无语'),)),
             AssistantTurn(tool_calls=(_call('send_emoji', index=9),))],
            search={'无语': [_emoji()]})
        reply = await plugin.handle(proactive_ctx())
        self.assertIsNone(reply.emoji)
        self.assertIn('没有第 9 张', json.dumps(self.ai.rounds[2], ensure_ascii=False))
        # 越界也不能整轮算废：模型还有一轮机会改
        self.assertEqual(len(self.ai.rounds), 3)

    async def test_empty_search_tells_the_model_to_try_another_word(self):
        plugin = self.plugin([AssistantTurn(tool_calls=(_call('search_emoji', keyword='不存在的词'),)),
                              AssistantTurn()])
        await plugin.handle(proactive_ctx())
        self.assertIn('没搜到', json.dumps(self.ai.rounds[1], ensure_ascii=False))

    async def test_loop_stops_at_max_rounds(self):
        endless = AssistantTurn(tool_calls=(_call('search_emoji', keyword='无语'),))
        plugin = self.plugin([endless, endless, endless, endless], search={'无语': [_emoji()]})
        reply = await plugin.handle(proactive_ctx())
        self.assertEqual(len(self.ai.rounds), 3)      # planner_rounds 默认 3
        self.assertTrue(reply.stop)

    async def test_send_tool_is_hidden_without_a_searcher(self):
        ai = ToolAi([AssistantTurn()])
        plugin = ChatPlugin(ai, context_length=10)      # 没传 emoji_search
        await plugin.handle(proactive_ctx())
        self.assertEqual(ai.tools_seen[0], ['search_emoji', 'speak'])

    async def test_backend_without_tool_support_falls_back_to_json(self):
        """ 不支持工具的后端要回到单轮 JSON 决策，功能不能少。 """
        plugin = ChatPlugin(FakeAi(), context_length=10, emoji_search=FakeEmojiSearch())
        reply = await plugin.handle(proactive_ctx())
        self.assertEqual(reply.texts, ['收到'])

    async def test_prompt_switches_between_tool_and_json_mode(self):
        """ 提示词的输出格式必须跟走哪条路一致，不能两个都写。 """
        plugin = self.plugin([])
        ctx = proactive_ctx()
        tool_prompt = await plugin._planner_prompt(ctx, ('search_emoji', 'send_emoji', 'speak'))
        json_prompt = await plugin._planner_prompt(ctx)

        self.assertIn('什么工具都不调，就等于这一轮不发言', tool_prompt)
        self.assertIn('search_emoji', tool_prompt)
        self.assertNotIn('只输出一个 JSON', tool_prompt)

        self.assertIn('只输出一个 JSON', json_prompt)
        self.assertNotIn('search_emoji', json_prompt)

        # {% if %} 独占一行不能留下空行，白烧 token
        for text in (tool_prompt, json_prompt):
            self.assertNotIn('\n\n\n', text)

    async def test_emoji_tools_are_left_out_of_the_prompt_when_unavailable(self):
        """ 没接表情搜索时，提示词里不能提搜表情——不然模型会去调一个不存在的工具。 """
        plugin = ChatPlugin(ToolAi([]), context_length=10)
        prompt = await plugin._planner_prompt(proactive_ctx(), ('speak',))
        self.assertIn('speak', prompt)
        self.assertNotIn('search_emoji', prompt)
        self.assertNotIn('send_emoji', prompt)


class TestCleanReply(unittest.TestCase):
    """ 模型顺手带的包装不能漏到群里。 """

    def test_strips_nickname_prefix(self):
        self.assertEqual(clean_reply('Bot：在的', names=('Bot',)), '在的')
        self.assertEqual(clean_reply('Bot: 在的', names=('Bot',)), '在的')
        self.assertEqual(clean_reply('雨姐：在的', names=('雨墨蝉烟', '雨姐')), '在的')

    def test_prefix_strip_needs_a_separator(self):
        """ 按字符集剥（str.lstrip）和按裸前缀剥都会误伤，必须要求分隔符。 """
        self.assertEqual(clean_reply('Bother', names=('Bot',)), 'Bother')
        self.assertEqual(clean_reply('雨姐们都在吗', names=('雨姐',)), '雨姐们都在吗')
        # 整条消息就只有名字（后面什么都不跟）就当空
        self.assertEqual(clean_reply('Bot', names=('Bot',)), '')
        self.assertEqual(clean_reply('Bot ', names=('Bot',)), '')

    def test_strips_wrapping_quotes(self):
        self.assertEqual(clean_reply('「在的」'), '在的')
        self.assertEqual(clean_reply('“在的”'), '在的')
        self.assertEqual(clean_reply('《在的》'), '在的')
        # 叠了几层
        self.assertEqual(clean_reply('“回复：在的”'), '在的')

    def test_keeps_parentheses_because_they_are_content(self):
        self.assertEqual(clean_reply('（目移）'), '（目移）')
        self.assertEqual(clean_reply('在的（笑）'), '在的（笑）')

    def test_strips_markdown_and_blank_lines(self):
        self.assertEqual(clean_reply('**在的**'), '在的')
        self.assertEqual(clean_reply('`在的`'), '在的')
        self.assertEqual(clean_reply('\n\n在的\n\n'), '在的')
        self.assertEqual(clean_reply('在的\n\n 好  \n'), '在的\n好')

    def test_empty_stays_empty(self):
        for text in ('', '   ', '\n\n', 'Bot：', '「」'):
            self.assertEqual(clean_reply(text, names=('Bot',)), '', repr(text))

    def test_does_not_touch_body(self):
        self.assertEqual(clean_reply('在的，你说「那个」是什么意思'), '在的，你说「那个」是什么意思')


class TestCleanReplyInPlugin(unittest.IsolatedAsyncioTestCase):
    async def test_plugin_cleans_before_sending(self):
        ai = FakeAi(reply=('Bot：「收到」',))
        plugin = ChatPlugin(ai, context_length=10)
        reply = await plugin.handle(make_ctx(make_msg(is_at_me=True)))
        self.assertEqual(reply.texts, ['收到'])

    async def test_all_empty_becomes_stop(self):
        ai = FakeAi(reply=('Bot：',))
        plugin = ChatPlugin(ai, context_length=10)
        reply = await plugin.handle(make_ctx(make_msg(is_at_me=True)))
        self.assertTrue(reply.stop)
        self.assertEqual(reply.texts, [])

    async def test_alias_prefix_is_also_stripped(self):
        ai = FakeAi(reply=('雨姐：收到',))
        plugin = ChatPlugin(ai, context_length=10, persona=Persona(aliases=('雨姐',)))
        reply = await plugin.handle(make_ctx(make_msg(is_at_me=True)))
        self.assertEqual(reply.texts, ['收到'])


if __name__ == '__main__':
    unittest.main()
