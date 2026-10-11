""" 插件自检：两段式回复、必要性门控、模板注入、回复轨迹，以及复读机不跟媒体占位。 """

import base64
import json
import tempfile
import time
import types
import unittest
from pathlib import Path

from context import ChatWindow, MessageContext, message_handle
from emojis import Emoji
from events import bus
from focus import FocusMode
from habits import Habit, HabitStore
from images import ImageStore
from media import Media
from moments import Moment, MomentStore
from necessity import NECESSITY_THRESHOLD, SpeechLimiter
from persona import Persona
from personas import PersonaStore
from plugin import (
    CALENDAR_PER_TURN,
    REVOKE_PER_TURN,
    WEB_SEARCH_PER_TURN,
    ChatPlugin,
    RepeatPlugin,
    ReplyPolicy,
    clean_reply,
)
from profiles import Profile, ProfileStore
from reply_trace import TraceLogger
from reply_trace import find as find_trace
from tools import AssistantTurn, ToolCall

PLANNER_MARK = '决策者'          # planner.txt 里独有的字眼，用来区分两次调用


# 决策者默认「说一句」；传 AssistantTurn() 表示这一轮不发言，传 None 表示决策调用失败
_REPLY = AssistantTurn(tool_calls=(ToolCall('reply-1', 'reply', {'text': '收到'}),))


class FakeAi:
    """ 假决策者：agent 模式只有一次调用（`chat_tools`），没有独立的表达者。

    `planner_calls` / `reply_calls` 都指向同一份记录——两段式删掉后，「决策者看到的」
    和「模型看到的」本来就是同一条 message（`_compose` 拼出来的那条），
    留着这两个名字是为了少改断言。
    """

    def __init__(self, turn=_REPLY):
        self.turn = turn
        self.calls = []

    async def chat_tools(self, prompt, messages, tools):
        self.calls.append((prompt, messages[0]['content']))
        return self.turn

    @property
    def planner_calls(self):
        return self.calls

    @property
    def reply_calls(self):
        return self.calls


def _call(name, **arguments):
    return ToolCall(call_id=f'{name}-1', name=name, arguments=arguments)


def _emoji(md5='a' * 32, desc='无语', scene='别人说了很离谱的话', count=0):
    return Emoji(md5=md5, desc=desc, scene=scene, cdnurl='http://cdn/x', aeskey='k', count=count)


def _reply(text):
    return AssistantTurn(tool_calls=(ToolCall('reply-1', 'reply', {'text': text}),))


class ToolAi:
    """ 会用工具的假决策者：turns 是每轮依次返回的东西。 """

    def __init__(self, turns=(), reply=('收到',)):
        self.turns = list(turns)
        self.reply = list(reply) if reply else None
        self.rounds = []            # 每次 chat_tools 收到的 messages
        self.prompts = []           # 每次 chat_tools 收到的 system prompt
        self.tools_seen = []        # 每次暴露的工具名
        self.reply_calls = []

    async def chat_tools(self, prompt, messages, tools):
        self.prompts.append(prompt)
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


def make_msg(msg_type=1, is_chatroom=False, is_at_me=False, content=''):
    return types.SimpleNamespace(msg_type=msg_type, is_chatroom=is_chatroom, is_at_me=is_at_me,
                                 # 19 位雪花号，比 JS 安全整数还大
                                 msg_id=7100000000000000123,
                                 content=content, sender_name='Alice', reply_target='456@chatroom')


def sample_history():
    window = ChatWindow()
    window.append('Alice', '在吗', 1)
    return window


def plugin_fresh_text(ctx):
    """ 直接问插件那个取正文的帮手，别绕一圈猜。 """
    from plugin import _fresh_text
    return _fresh_text(ctx)


def make_ctx(msg, history=None, forced=False, fresh=()):
    history = history if history is not None else sample_history()
    return MessageContext(msg=msg, history=history, self_name='Bot', contact='测试群', forced=forced,
                          fresh=fresh)


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

    async def test_the_model_sees_the_record_not_the_stale_placeholder(self):
        """ 图 / 表情的描述是识图跑完写回**记录**的，`msg.content` 还是分发那一刻的裸占位符。

        拿它渲染，模型最该看的那条就没有内容，只好去别处找话题——2026-09-23 那张猫表情
        被答成「这拌面看着真香」就是这么来的。所以正文一律从记录里取。
        """
        msg = make_msg(content='[表情 #nbzb5x]')
        history = sample_history()
        history.append('Alice', '[表情 #nbzb5x：猫咪瞪大双眼一脸震惊]', 2, msg_id=msg.msg_id)
        ctx = make_ctx(msg, history, fresh=(msg,))
        await self.plugin.handle(ctx)
        sent = self.plugin.ai.reply_calls[0][1]

        self.assertIn('[表情 #nbzb5x：猫咪瞪大双眼一脸震惊]', sent)
        self.assertNotIn('[表情 #nbzb5x]\n', sent)                # 没有裸占位符那一版
        self.assertNotIn('【本轮】', sent)                         # 不再单独拎一条出来
        # 群用语 / 记忆的取词也走记录（裸占位符里没有可匹配的词）
        self.assertEqual(plugin_fresh_text(ctx), '[表情 #nbzb5x：猫咪瞪大双眼一脸震惊]')

    async def test_new_messages_are_marked_in_the_transcript(self):
        """ 新消息在聊天记录里画一条分隔线：模型没有未读概念，得告诉它边界在哪。 """
        msg = make_msg(content='那就定周六')
        old_id, new_id = 7100000000000000001, msg.msg_id
        history = sample_history()
        history.append('Alice', '周末去爬山不', 1, msg_id=old_id)
        history.append('Alice', '那就定周六', 2, msg_id=new_id)
        await self.plugin.handle(make_ctx(msg, history, fresh=(msg,)))

        sent = self.plugin.ai.reply_calls[0][1]
        lines = sent.split('\n')
        divider = lines.index('----以下是新消息----')
        self.assertTrue(lines[divider + 1].endswith('Alice: 那就定周六'), lines[divider:])
        # 分隔线之前那条（更早的）不该被标成新消息
        self.assertNotIn('----以下是新消息----', lines[:divider])

    async def test_ref_candidates_only_list_recent_tagged_messages(self):
        """ ref 候选只列最近几条带标签的：满记录都是标签，模型挑错就引错人。 """
        history = sample_history()
        for i in range(6):
            history.append('Alice', f'第{i}条', i, msg_id=1000 + i)
        history.append('Bot', '我说过的', 9, is_self=True, msg_id=2000)

        listing = history.ref_candidates(limit=3)
        self.assertEqual([line.split()[1] for line in listing.splitlines()],
                         [message_handle(2000), message_handle(1005), message_handle(1004)])
        self.assertIn('我: 我说过的', listing)      # 自己刚说的话也能引
        self.assertNotIn('第3条', listing)          # 超出 limit 的不列

        # 一条标签都没有（老记录 / 空窗口）时给一句兜底，不是空字符串
        self.assertIn('别填', ChatWindow().ref_candidates())

        # 提示词里得真带上这份候选，否则 ref 规则还是让模型满记录乱挑
        prompt = await self.plugin._agent_prompt(make_ctx(make_msg(is_chatroom=True), history), ('reply',))
        self.assertIn(message_handle(2000), prompt)

    async def test_quote_feedback_names_what_it_quotes(self):
        """ 引用回执要点出被引那条是什么，否则模型看不见自己引的是表情还是照片。 """
        history = sample_history()
        history.append('Bob', '[表情 #a1b2c3：猫咪瞪大双眼一脸震惊]', 2, msg_id=99)
        ctx = make_ctx(make_msg(is_chatroom=True, content='震惊啥'), history)
        _, note = self.plugin._resolve_ref({'ref': message_handle(99)}, ctx)
        self.assertIn('猫咪瞪大双眼', note)
        self.assertIn('Bob', note)

    async def test_direct_address_also_goes_through_planner(self):
        """ 被 @ / 叫名字 / 私聊也走决策者——表情工具在那条路上，代价是多一次调用。 """
        for msg in (make_msg(is_chatroom=True, is_at_me=True),
                    make_msg(is_chatroom=True, content='Bot 在吗'),
                    make_msg(is_chatroom=False)):
            self.ai.calls.clear()
            reply = await self.plugin.handle(make_ctx(msg))
            self.assertEqual(reply.texts, ['收到'])
            self.assertEqual(len(self.ai.planner_calls), 1)
            self.assertIn('【必须回应】', self.ai.planner_calls[0][1])

    async def test_silent_plan_does_not_reply(self):
        self.plugin.ai = FakeAi(turn=AssistantTurn())
        reply = await self.plugin.handle(proactive_ctx())
        self.assertEqual(reply.texts, [])
        self.assertTrue(reply.stop)
        self.assertEqual(len(self.plugin.ai.calls), 1)     # 只调了一次，没有下一轮补话

    async def test_planner_can_use_its_own_model(self):
        """ 决策者可以走本地便宜模型：这一轮的调用只到 planner，主模型不碰。 """
        planner = FakeAi()
        plugin = ChatPlugin(self.ai, context_length=10, planner_ai=planner)
        await plugin.handle(proactive_ctx())
        self.assertEqual(len(planner.calls), 1)
        self.assertEqual(self.ai.calls, [])

    async def test_planner_failure_ends_the_round(self):
        """ 决策调用失败：这一轮就没得说（没有表达者兜底了）。 """
        self.plugin.ai = FakeAi(turn=None)
        reply = await self.plugin.handle(proactive_ctx())
        self.assertEqual(reply.texts, [])
        self.assertTrue(reply.stop)

    async def test_model_failure_does_not_reply(self):
        self.plugin.ai = FakeAi(turn=None)
        reply = await self.plugin.handle(make_ctx(make_msg(is_chatroom=False)))
        self.assertEqual(reply.texts, [])
        self.assertTrue(reply.stop)

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
        with tempfile.TemporaryDirectory() as tmp:
            plugin = ChatPlugin(self.ai, context_length=10, image_store=ImageStore(tmp))
            window = ChatWindow()
            window.append('Alice', '在吗', 1)

            async def ok():
                return image

            window.append('Alice', '[图片 #4k2p]', 2, media=Media('image', 'm'),
                          download=ok, media_id=999, handle='#4k2p')
            reply = await plugin.handle(make_ctx(make_msg(is_chatroom=False), history=window))
        self.assertEqual(reply.texts, ['收到'])
        content = self.ai.reply_calls[0][1]
        self.assertIsInstance(content, list)
        self.assertEqual(content[0]['type'], 'text')
        self.assertEqual(content[1], {'type': 'text', 'text': '[图片 #4k2p]'})
        self.assertEqual(content[2]['type'], 'image_url')
        expected = 'data:image/jpeg;base64,' + base64.b64encode(image).decode()
        self.assertEqual(content[2]['image_url']['url'], expected)

    async def test_profiles_are_injected_into_request(self):
        store = ProfileStore(people={'wxid_x': Profile('wxid_x', names=('Alice',), phrases=('蛙趣',))})
        self.plugin.profiles = store
        await self.plugin.handle(proactive_ctx())
        self.assertIn('【群友画像】', self.ai.planner_calls[0][1])
        self.assertIn('口头禅：蛙趣', self.ai.planner_calls[0][1])

    async def test_habits_injected_only_on_hit(self):
        self.plugin.policy.threshold = 0      # 这几条测的是 prompt 内容，不关门的门控
        self.plugin.habits = HabitStore(habits=(Habit('蛙趣', 126, ('蛙趣 你的ID和小红书一样的吧',)),))

        await self.plugin.handle(self.proactive('蛙趣，真的假的'))
        self.assertIn('你的ID和小红书', self.ai.planner_calls[0][1])
        self.assertIn('你的ID和小红书', self.ai.reply_calls[0][1])   # 表达者也要知道群里怎么说

        await self.plugin.handle(self.proactive('平平无奇的一句话'))
        self.assertNotIn('群里的说法', self.ai.planner_calls[1][1])

    async def test_memories_injected_on_keyword_hit(self):
        self.plugin.policy.threshold = 0      # 这几条测的是 prompt 内容，不关门的门控
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
        self.plugin.policy.threshold = 0      # 这几条测的是 prompt 内容，不关门的门控
        self.plugin.profiles = ProfileStore(people={'wxid_x': Profile('wxid_x', names=('Alice',), phrases=('蛙趣',))})
        self.plugin.habits = HabitStore(habits=(Habit('蛙趣', 126, ('蛙趣 你的ID和小红书一样的吧',)),))

        await self.plugin.handle(self.proactive('蛙趣，真的假的'))
        await self.plugin.handle(self.proactive('平平无奇的一句话'))

        self.assertEqual(self.ai.planner_calls[0][0], self.ai.planner_calls[1][0])
        self.assertEqual(self.ai.reply_calls[0][0], self.ai.reply_calls[1][0])
        self.assertNotIn('你的ID和小红书', self.ai.planner_calls[0][0])
        self.assertNotIn('口头禅', self.ai.reply_calls[0][0])

    async def test_alias_counts_as_being_called(self):
        """ 群里叫「雨姐」也算在叫它：当成必须回应，决策者那边会收到提醒。 """
        plugin = ChatPlugin(self.ai, context_length=10, policy=ReplyPolicy(aliases=('雨姐',)))
        reply = await plugin.handle(make_ctx(make_msg(is_chatroom=True, content='雨姐在吗')))
        self.assertEqual(reply.texts, ['收到'])
        self.assertIn('【必须回应】', self.ai.planner_calls[0][1])

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

    def _window(self, download, handle='#4k2p', media_id=123):
        window = ChatWindow()
        window.append('Alice', f'[图片 {handle}]', 1, media=Media('image', 'm'),
                      download=download, media_id=media_id, handle=handle)
        return window

    async def test_image_fetch_failure_degrades_to_text(self):
        """ 图片拉不下来不能把整轮回复带崩：取不到就不附，正文照发。 """
        with tempfile.TemporaryDirectory() as tmp:
            plugin = ChatPlugin(self.ai, context_length=10, image_store=ImageStore(tmp))

            async def boom():
                raise RuntimeError('下载附件失败: 消息 123 无 cdnurl')

            images = await plugin._collect_images(self._window(boom))
            self.assertEqual(images, [])
            self.assertEqual(plugin._build_message('记录', images), '记录')

    async def test_image_is_attached_when_download_works(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = ChatPlugin(self.ai, context_length=10, image_store=ImageStore(tmp))

            async def ok():
                return b'\xff\xd8\xff\xe0'

            images = await plugin._collect_images(self._window(ok))
            self.assertEqual([handle for handle, _ in images], ['#4k2p'])
            out = plugin._build_message('记录', images)
            self.assertIsInstance(out, list)
            self.assertEqual(out[0], {'type': 'text', 'text': '记录'})
            # 每张图前面配一行同样的标签，模型才能把图和记录对上
            self.assertEqual(out[1], {'type': 'text', 'text': '[图片 #4k2p]'})
            self.assertTrue(out[2]['image_url']['url'].startswith('data:image/jpeg;base64,'))

    async def test_same_image_is_attached_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = ChatPlugin(self.ai, context_length=10, image_store=ImageStore(tmp))
            window = ChatWindow()
            calls = []

            async def ok():
                calls.append(1)
                return b'\xff\xd8\xff\xe0'

            window.append('Alice', '[图片 #4k2p]', 1, media=Media('image', 'm'),
                          download=ok, media_id=123, handle='#4k2p')
            # 引用同一条（media_id 相同）不能重复附
            window.append('Bob', '这个（引用前文的消息：[图片 #4k2p]）', 2,
                          download=ok, media_id=123, handle='#4k2p')
            self.assertEqual(len(await plugin._collect_images(window)), 1)
            self.assertEqual(len(calls), 1)

    async def test_window_without_media_never_touches_the_downloader(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = ChatPlugin(self.ai, context_length=10, image_store=ImageStore(tmp))
            window = ChatWindow()
            window.append('Alice', '在吗', 1)
            self.assertEqual(await plugin._collect_images(window), [])

    async def test_tracer_records_the_round(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'trace.jsonl'
            self.plugin.tracer = TraceLogger(path)
            await self.plugin.handle(proactive_ctx())
            record = json.loads(path.read_text(encoding='utf-8').strip())
            found = find_trace(path, 7100000000000000123)      # 临时目录还在，得在 with 里找

        self.assertEqual(record['trigger'], 'proactive')
        self.assertEqual(record['reply'], ['收到'])
        self.assertEqual(record['sender'], 'Alice')
        self.assertIn('msg11', record['transcript'])
        self.assertIn('收到', record['plan'])
        self.assertGreaterEqual(record['latency_ms'], 0)
        # 「为什么没回」抽屉靠 msg_id 把气泡和这一轮对上（64 位，存成字符串）
        self.assertEqual(record['msg_id'], '7100000000000000123')
        self.assertEqual(found, record)

    async def test_tracer_records_direct_address_with_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'trace.jsonl'
            self.plugin.tracer = TraceLogger(path)
            await self.plugin.handle(make_ctx(make_msg(is_chatroom=False)))
            record = json.loads(path.read_text(encoding='utf-8').strip())

        self.assertEqual(record['trigger'], 'private')
        self.assertIn('收到', record['plan'])   # plan 是工具记录：reply({"text": "收到"})

    async def test_tracer_records_silent_turns(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'trace.jsonl'
            self.plugin.ai = FakeAi(turn=AssistantTurn())
            self.plugin.tracer = TraceLogger(path)
            await self.plugin.handle(proactive_ctx())
            record = json.loads(path.read_text(encoding='utf-8').strip())

        self.assertEqual(record['reply'], [])   # 决策者选择不发言，也要留痕
        self.assertEqual(record['plan'], '')

    async def test_replay_round_leaves_no_trace_and_no_status(self):
        """ 「试回一次」跑的是同一条链路，但不能留痕：它没真处理这条消息。 """
        seen: list[str] = []

        def record(event):
            seen.append(event.status)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'trace.jsonl'
            self.plugin.tracer = TraceLogger(path)
            ctx = proactive_ctx()
            ctx.replay = True
            bus.subscribe(record)
            try:
                reply = await self.plugin.handle(ctx)
            finally:
                bus.unsubscribe(record)

            self.assertEqual(reply.texts, ['收到'])                  # 链路照跑
            self.assertEqual(seen, [])                               # 面板上一声不吭
            self.assertFalse(path.exists())                          # 一条轨迹都不写


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

    def test_a_burst_is_judged_as_a_whole(self):
        """ 一块里好几个人说话时，门控看整块。

        以前只看挑出来的触发那条：块是 [「这个怎么弄啊」, 「哈哈」] 时触发是「哈哈」，
        短反应直接被判成不值得回，前面那句真问题就没人理了。
        """
        question = make_msg(is_chatroom=True, content='这个怎么弄啊')
        laugh = make_msg(is_chatroom=True, content='哈哈')
        ctx = make_ctx(laugh, history=history_with(12, time.time()), fresh=(question, laugh))

        decision = self.policy.evaluate_detailed(ctx, now=time.time())
        self.assertTrue(decision.allowed, decision.detail)
        self.assertEqual(decision.score_obj.kind, 'proactive')
        self.assertIn('问句', decision.score_obj.reasons)

    def test_addressed_message_anywhere_in_the_burst_wins(self):
        """ 被 @ 的那条不在块尾也要算：块里有一条指名了就必须回。 """
        addressed = make_msg(is_chatroom=True, is_at_me=True, content='Bot 在吗')
        laugh = make_msg(is_chatroom=True, content='哈哈')
        ctx = make_ctx(laugh, history=history_with(12, time.time()), fresh=(addressed, laugh))
        decision = self.policy.evaluate_detailed(ctx, now=time.time())
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.score_obj.kind, 'at')

    def test_quiet_group_is_silent(self):
        self.assertIsNone(self.policy.evaluate(self.ctx(content='大家好啊'), now=1000.0))

    def test_forced_skips_the_gate_but_keeps_the_score(self):
        """ 手动触发只跳过「拦不拦」：分数照算，面板要看得见「本来会被拦下」。 """
        ctx = self.ctx(content='大家好啊')
        ctx.forced = True
        decision = self.policy.evaluate_detailed(ctx, now=1000.0, forced=True)
        self.assertTrue(decision.allowed)
        self.assertIsNotNone(decision.score_obj)
        self.assertLess(decision.score, decision.threshold)

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

    def _focus_policy(self) -> ReplyPolicy:
        return ReplyPolicy(threshold=NECESSITY_THRESHOLD, activity_window=180.0,
                           focus=FocusMode(enter_activity=1, idle=10_000.0,
                                           maximum=10_000.0, cooldown=0.0))

    def test_focus_no_longer_bypasses_presence_penalty(self):
        """ 专注期只是加分，不再豁免门控：自己已经刷屏时照样闭嘴。 """
        history = ChatWindow()
        for i in range(6):
            history.append('Bot', f'我说的{i}', int(1000 - i), is_self=True)
        for i in range(2):
            history.append(f'用户{i}', '别人说的', int(1000 - i))
        self.assertIsNone(self._focus_policy().evaluate(
            self.ctx(content='这个怎么弄啊', history=history), now=1000.0))

    def test_focus_still_allows_when_group_is_hot(self):
        """ 专注期里没人点名、群里正热、有内容分时才参与（压力分封顶 40 不够）。 """
        history = history_with(15, now=1000.0)
        self.assertIsNotNone(self._focus_policy().evaluate(
            self.ctx(content='这个怎么弄啊', history=history), now=1000.0))

    def test_aliases_can_be_per_contact(self):
        """ 别名也能按会话取（分会话人设改了别名，在群里叫得应）。 """
        policy = ReplyPolicy(aliases=('全局名',), aliases_for=lambda contact: ('本群名',))
        self.assertEqual(policy.evaluate(self.ctx(content='本群名 在吗'), now=1000.0).kind, 'mention')
        self.assertIsNone(policy.evaluate(self.ctx(content='全局名 在吗'), now=1000.0))

    def test_rate_limit_blocks_proactive_but_not_being_called(self):
        """ 频率闸只拦主动参与；被点名必须回。 """
        speech = SpeechLimiter(window=300.0, maximum=1, min_gap=0.0)
        policy = ReplyPolicy(threshold=NECESSITY_THRESHOLD, speech=speech)
        ctx = self.ctx(content='这个怎么弄啊', history=history_with(12, now=1000.0))
        self.assertIsNotNone(policy.evaluate(ctx, now=1000.0))
        policy.record(ctx, spoke=True, now=1000.0)
        self.assertIsNone(policy.evaluate(ctx, now=1001.0))
        called = self.ctx(is_at_me=True, content='在吗', history=history_with(12, now=1000.0))
        self.assertIsNotNone(policy.evaluate(called, now=1001.0))


class TestRepeatPlugin(unittest.IsolatedAsyncioTestCase):
    async def test_media_markers_are_not_repeated(self):
        window = ChatWindow()
        window.append('Alice', '[表情]', 1)
        window.append('Bob', '[表情]', 2)
        window.append('Alice', '在吗', 3)
        reply = await RepeatPlugin().handle(make_ctx(make_msg(), history=window))
        self.assertIsNone(reply)

    async def test_marker_between_the_repeats_is_not_echoed(self):
        """ 真实的那个 bug：占位符被洗成空串后就从下标里消失了，
        但选中的却是**原历史**的第 N 条——于是把 [表情：欢迎] 当队形发出去。 """
        window = ChatWindow()
        window.append('Alice', '牛啊', 1)
        window.append('Bob', '[表情：欢迎]', 2)
        window.append('Carol', '牛啊', 3)
        window.append('Dave', '[表情：欢迎]', 4)
        window.append('Eve', '牛啊', 5)
        reply = await RepeatPlugin().handle(make_ctx(make_msg(), history=window))
        self.assertEqual(reply.texts, ['牛啊'])

    async def test_quote_annotation_is_not_echoed(self):
        """ render 给引用消息补的「（引用…）」是给模型看的，不能原样发给群
        （线上历史重复时就发过这条天书）。两种注解形状都要能砍掉。 """
        for annotation in ('（引用前文的消息：[图片]）', '（引用 #4k2p0z）'):
            window = ChatWindow()
            for who, at in (('Alice', 1), ('Bob', 2), ('Carol', 3)):
                window.append(who, f'这啥 官网？{annotation}', at)
            reply = await RepeatPlugin().handle(make_ctx(make_msg(), history=window))
            self.assertEqual(reply.texts, ['这啥 官网？'], annotation)

    async def test_repeats_and_stops(self):
        window = ChatWindow()
        window.append('Alice', '牛啊', 1)
        window.append('Bob', '牛啊', 2)
        window.append('Carol', '牛啊', 3)
        reply = await RepeatPlugin().handle(make_ctx(make_msg(), history=window))
        self.assertEqual(reply.texts, ['牛啊'])
        self.assertTrue(reply.stop)

    async def test_own_repeat_is_not_followed_again(self):
        window = ChatWindow(sample_history())
        window.append('Alice', '牛啊', 1)
        window.append('Bob', '牛啊', 2)
        window.append('Carol', '牛啊', 3)
        window.append('Bot', '牛啊', 4, is_self=True)
        reply = await RepeatPlugin().handle(make_ctx(make_msg(), history=window))
        self.assertIsNone(reply)


class TestIdleBackoffInPlugin(unittest.IsolatedAsyncioTestCase):
    """ 连着几轮没开口就别再每条都花一次决策模型调用。 """

    def silent_plugin(self):
        self.ai = FakeAi(turn=AssistantTurn())
        return ChatPlugin(self.ai, context_length=10)

    def talking_plugin(self):
        """ 前两轮不说话（制造退避），之后会说一句。 """
        self.ai = ToolAi([AssistantTurn(), AssistantTurn(), _reply('收到'), _reply('收到')])
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
        """ 连着两轮没说话进了退避，但被点名那轮照样走到模型、照样回。 """
        plugin = self.talking_plugin()
        for _ in range(3):
            await plugin.handle(proactive_ctx())
        reply = await plugin.handle(make_ctx(make_msg(is_chatroom=True, is_at_me=True)))
        self.assertEqual(reply.texts, ['收到'])

    async def test_replying_clears_the_backoff(self):
        plugin = self.talking_plugin()
        for _ in range(2):
            await plugin.handle(proactive_ctx())
        self.assertIsNone(await plugin.handle(proactive_ctx()))     # 退避中，模型都不调
        planned = len(self.ai.rounds)
        # 被点名回了一句 = 群里还需要它，退避清零（这一轮自己也要走一次模型）
        await plugin.handle(make_ctx(make_msg(is_chatroom=True, is_at_me=True)))
        self.assertEqual(len(self.ai.rounds), planned + 1)
        await plugin.handle(proactive_ctx())
        self.assertEqual(len(self.ai.rounds), planned + 2)


class TestToolLoop(unittest.IsolatedAsyncioTestCase):
    """ 决策者用工具：搜表情 → 选一张 → （顺带）说要说话。 """

    def plugin(self, turns, search=None, **kwargs):
        self.ai = ToolAi(turns)
        self.search = FakeEmojiSearch(search or {})
        return ChatPlugin(self.ai, context_length=10, emoji_search=self.search, **kwargs)

    async def test_reply_only_is_one_round(self):
        plugin = self.plugin([AssistantTurn(content='在吐槽加班',
                                            tool_calls=(_call('reply', text='确实'),))])
        reply = await plugin.handle(proactive_ctx())
        self.assertEqual(reply.texts, ['确实'])
        self.assertIsNone(reply.emoji)
        self.assertEqual(len(self.ai.rounds), 1)      # reply 终结本轮，不再多跑一轮

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
                                       _call('reply', text='确实')))],
            search={'狗头': [_emoji(desc='狗头')]})
        reply = await plugin.handle(proactive_ctx())
        self.assertEqual(reply.texts, ['确实'])
        self.assertEqual(reply.emoji.desc, '狗头')

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

    async def test_long_keyword_is_cut_to_two_chars(self):
        """ 表情是按字面匹配的，词越长命中越差；模型写长了就只取前两个字。 """
        plugin = self.plugin([AssistantTurn(tool_calls=(_call('search_emoji', keyword='无语到说不出话'),)),
                              AssistantTurn()], search={'无语': [_emoji()]})
        await plugin.handle(proactive_ctx())
        self.assertEqual(self.search.queries, ['无语'])

    async def test_empty_search_tells_the_model_to_try_another_word(self):
        plugin = self.plugin([AssistantTurn(tool_calls=(_call('search_emoji', keyword='不存在的词'),)),
                              AssistantTurn()])
        await plugin.handle(proactive_ctx())
        self.assertIn('没搜到', json.dumps(self.ai.rounds[1], ensure_ascii=False))

    async def test_loop_stops_at_max_rounds(self):
        endless = AssistantTurn(tool_calls=(_call('search_emoji', keyword='无语'),))
        plugin = self.plugin([endless] * 6, search={'无语': [_emoji()]})
        reply = await plugin.handle(proactive_ctx())
        self.assertEqual(len(self.ai.rounds), 5)      # planner_rounds 默认 5
        self.assertTrue(reply.stop)

    async def test_send_tool_is_hidden_without_a_searcher(self):
        ai = ToolAi([AssistantTurn()])
        plugin = ChatPlugin(ai, context_length=10)      # 没传 emoji_search
        await plugin.handle(proactive_ctx())
        self.assertNotIn('send_emoji', ai.tools_seen[0])

    async def test_emoji_cooldown_refuses_a_second_sticker(self):
        """ 模型看不见自己上次什么时候发的表情，节制只能由程序管。 """
        turns = [AssistantTurn(tool_calls=(_call('search_emoji', keyword='无语'),)),
                 AssistantTurn(tool_calls=(_call('send_emoji', index=1),))]
        plugin = self.plugin(turns, search={'无语': [_emoji()]})
        self.assertIsNotNone((await plugin.handle(proactive_ctx())).emoji)

        # 马上再来一次：搜得到，但不让发，得改用说话
        self.ai.turns = [AssistantTurn(tool_calls=(_call('search_emoji', keyword='无语'),)),
                         AssistantTurn(tool_calls=(_call('send_emoji', index=1),))]
        second = await plugin.handle(proactive_ctx())
        self.assertIsNone(second.emoji)
        self.assertIn('刚发过表情', json.dumps(self.ai.rounds[-1], ensure_ascii=False))

    async def test_prompt_says_how_to_act_without_repeating_the_tools(self):
        """ 工具用法只写在 schema 的 description 里；prompt 只留跨工具的规则。 """
        plugin = self.plugin([])
        prompt = await plugin._agent_prompt(proactive_ctx(),
                                            ('search_emoji', 'send_emoji', 'reply', 'revoke'))

        self.assertIn('什么工具都不调，就等于这一轮不发言', prompt)
        self.assertIn('send_emoji', prompt)          # 组合规则里要提它
        self.assertNotIn('search_emoji(关键词)', prompt)     # 单工具用法不在这里重抄
        self.assertNotIn('只输出一个 JSON', prompt)
        # {% if %} 独占一行不能留下空行，白烧 token
        self.assertNotIn('\n\n\n', prompt)

    async def test_emoji_tools_are_left_out_of_the_prompt_when_unavailable(self):
        """ 没接表情搜索时，提示词里不能提搜表情——不然模型会去调一个不存在的工具。 """
        plugin = ChatPlugin(ToolAi([]), context_length=10)
        prompt = await plugin._agent_prompt(proactive_ctx(), ('reply',))
        self.assertIn('reply', prompt)
        self.assertNotIn('search_emoji', prompt)
        self.assertNotIn('send_emoji', prompt)


class TestRevokeTool(unittest.IsolatedAsyncioTestCase):
    """ 撤回工具：模型只说「撤哪条」，daemon 要的 msg_id 由 Bot 解析。 """

    def setUp(self):
        self.revoked = []

    async def revoke(self, chat, index, text):
        self.revoked.append((chat, index, text))
        return '已撤回：「在的 咋啦」'

    def plugin(self, turns):
        self.ai = ToolAi(turns)
        return ChatPlugin(self.ai, context_length=10, revoke=self.revoke)

    async def test_revoke_is_exposed_and_called(self):
        plugin = self.plugin([AssistantTurn(tool_calls=(_call('revoke', index=1),))])
        reply = await plugin.handle(proactive_ctx())
        self.assertIn('revoke', self.ai.tools_seen[0])
        # 会话键是 reply_target（wxid/群 id），不是显示名
        self.assertEqual(self.revoked, [('456@chatroom', 1, '')])
        # 撤完没说话 = 本轮沉默
        self.assertTrue(reply.stop)

    async def test_text_target_is_passed_through(self):
        plugin = self.plugin([AssistantTurn(tool_calls=(_call('revoke', text='在的'),))])
        await plugin.handle(proactive_ctx())
        self.assertEqual(self.revoked, [('456@chatroom', 0, '在的')])

    async def test_revoke_is_hidden_without_a_backend(self):
        ai = ToolAi([AssistantTurn()])
        plugin = ChatPlugin(ai, context_length=10)
        await plugin.handle(proactive_ctx())
        self.assertNotIn('revoke', ai.tools_seen[0])

    async def test_revoke_is_capped_per_turn(self):
        """ 撤回不可逆，一轮最多撤几条，不能让它连点。 """
        calls = tuple(_call('revoke', index=1) for _ in range(REVOKE_PER_TURN + 1))
        plugin = self.plugin([AssistantTurn(tool_calls=calls)])
        await plugin.handle(proactive_ctx())
        self.assertEqual(len(self.revoked), REVOKE_PER_TURN)


class TestMessageSearchTool(unittest.IsolatedAsyncioTestCase):
    """ 查历史：窗口里没有的东西，模型自己发起去搜。 """

    def setUp(self):
        self.asked = []

    async def search(self, chat, keyword):
        self.asked.append((chat, keyword))
        return '[09-14 20:30] Alice: 上次说的那家店叫啥' if keyword == '店' else ''

    def plugin(self, turns, **kwargs):
        self.ai = ToolAi(turns)
        return ChatPlugin(self.ai, context_length=10, message_search=self.search,
                          **kwargs)

    async def test_hits_are_fed_back_and_the_turn_continues(self):
        plugin = self.plugin([AssistantTurn(tool_calls=(_call('search_messages', keyword='店'),)),
                              AssistantTurn(tool_calls=(_call('reply', text='就叫老张面馆'),))])
        reply = await plugin.handle(proactive_ctx())

        self.assertIn('search_messages', self.ai.tools_seen[0])
        self.assertEqual(self.asked, [('456@chatroom', '店')])
        # 搜到的东西要进下一轮的对话，否则搜了也白搜
        self.assertIn('上次说的那家店', json.dumps(self.ai.rounds[1], ensure_ascii=False))
        self.assertEqual(reply.texts, ['就叫老张面馆'])

    async def test_a_miss_says_so_and_the_model_can_still_speak(self):
        plugin = self.plugin([AssistantTurn(tool_calls=(_call('search_messages', keyword='不存在的词'),)),
                              AssistantTurn(tool_calls=(_call('reply', text='这个我真想不起来了'),))])
        reply = await plugin.handle(proactive_ctx())
        self.assertIn('没搜到', json.dumps(self.ai.rounds[1], ensure_ascii=False))
        self.assertEqual(reply.texts, ['这个我真想不起来了'])

    async def test_empty_keyword_does_not_touch_the_backend(self):
        plugin = self.plugin([AssistantTurn(tool_calls=(_call('search_messages'),)), AssistantTurn()])
        await plugin.handle(proactive_ctx())
        self.assertEqual(self.asked, [])
        self.assertIn('没给关键词', json.dumps(self.ai.rounds[1], ensure_ascii=False))

    async def test_tool_is_hidden_without_a_backend(self):
        ai = ToolAi([AssistantTurn()])
        await ChatPlugin(ai, context_length=10).handle(proactive_ctx())
        self.assertNotIn('search_messages', ai.tools_seen[0])


class TestWebSearchTool(unittest.IsolatedAsyncioTestCase):
    """ 联网搜索：模型自己发起，结果回灌给它。 """

    def setUp(self):
        self.searched = []

    async def search(self, query):
        self.searched.append(query)
        return '联网搜到的资料：\n综合回答：今天晴' if query == '天气' else '网上没搜到'

    def plugin(self, turns, **kwargs):
        self.ai = ToolAi(turns)
        return ChatPlugin(self.ai, context_length=10, web_search=self.search,
                          **kwargs)

    async def test_results_are_fed_back_and_the_turn_continues(self):
        plugin = self.plugin([AssistantTurn(tool_calls=(_call('web_search', query='天气'),)),
                              AssistantTurn(tool_calls=(_call('reply', text='今天晴'),))])
        reply = await plugin.handle(proactive_ctx())

        self.assertIn('web_search', self.ai.tools_seen[0])
        self.assertEqual(self.searched, ['天气'])
        # 搜到的东西要进下一轮的对话，否则搜了也白搜
        self.assertIn('今天晴', json.dumps(self.ai.rounds[1], ensure_ascii=False))
        self.assertEqual(reply.texts, ['今天晴'])

    async def test_empty_query_does_not_touch_the_backend(self):
        plugin = self.plugin([AssistantTurn(tool_calls=(_call('web_search'),)), AssistantTurn()])
        await plugin.handle(proactive_ctx())
        self.assertEqual(self.searched, [])
        self.assertIn('没给搜索词', json.dumps(self.ai.rounds[1], ensure_ascii=False))

    async def test_searches_are_capped_per_turn(self):
        """ 一轮里模型看不见自己搜过几回，不能让它一个词反复搜。 """
        calls = tuple(_call('web_search', query=f'词{i}') for i in range(WEB_SEARCH_PER_TURN + 1))
        plugin = self.plugin([AssistantTurn(tool_calls=calls)])
        await plugin.handle(proactive_ctx())
        self.assertEqual(len(self.searched), WEB_SEARCH_PER_TURN)

    async def test_tool_is_hidden_without_a_backend(self):
        ai = ToolAi([AssistantTurn()])
        await ChatPlugin(ai, context_length=10).handle(proactive_ctx())
        self.assertNotIn('web_search', ai.tools_seen[0])


class TestCalendarTool(unittest.IsolatedAsyncioTestCase):
    """ 日历：模型自己查，公历 + 农历 + 节日/节气。 """

    def plugin(self, turns, **kwargs):
        self.ai = ToolAi(turns)
        return ChatPlugin(self.ai, context_length=10, **kwargs)

    async def test_result_is_fed_back_and_the_turn_continues(self):
        plugin = self.plugin([AssistantTurn(tool_calls=(_call('calendar'),)),
                              AssistantTurn(tool_calls=(_call('reply', text='今天周一'),))])
        reply = await plugin.handle(proactive_ctx())

        self.assertIn('calendar', self.ai.tools_seen[0])
        # 日历要进下一轮的对话，模型才看得到农历和星期
        fed = json.dumps(self.ai.rounds[1], ensure_ascii=False)
        self.assertIn('农历', fed)
        self.assertIn('← 今天', fed)
        self.assertEqual(reply.texts, ['今天周一'])

    async def test_month_query_returns_that_month(self):
        plugin = self.plugin([AssistantTurn(tool_calls=(_call('calendar', when='2026-10'),)),
                              AssistantTurn()])
        await plugin.handle(proactive_ctx())
        self.assertIn('2026 年 10 月', json.dumps(self.ai.rounds[1], ensure_ascii=False))

    async def test_queries_are_capped_per_turn(self):
        """ 一轮里模型看不见自己查过几回，不能让它逮着一天反复查。 """
        calls = tuple(_call('calendar', when=f'2026-{month:02d}')
                      for month in range(1, CALENDAR_PER_TURN + 2))
        plugin = self.plugin([AssistantTurn(tool_calls=calls)])
        await plugin.handle(proactive_ctx())
        self.assertIn('查得够多了', json.dumps(self.ai.rounds[1], ensure_ascii=False))

    async def test_unparsable_input_comes_back_in_plain_words(self):
        plugin = self.plugin([AssistantTurn(tool_calls=(_call('calendar', when='下周三'),)),
                              AssistantTurn()])
        await plugin.handle(proactive_ctx())
        self.assertIn('看不懂', json.dumps(self.ai.rounds[1], ensure_ascii=False))

    async def test_tool_is_available_out_of_the_box(self):
        """ 不依赖任何可选能力注入——日历是内置的。 """
        ai = ToolAi([AssistantTurn()])
        await ChatPlugin(ai, context_length=10).handle(proactive_ctx())
        self.assertIn('calendar', ai.tools_seen[0])

    async def test_compose_carries_the_current_time(self):
        """ 聊天记录只有 MM-DD，没有年份和星期；正文末尾得补上「现在」。 """
        text = ChatPlugin._compose(proactive_ctx(), {}, '张三: 在吗')
        self.assertRegex(text, r'【现在】\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} '
                                r'星期[一二三四五六日] · 农历\S+月\S+')
        # 时刻必须是最后一行：它前面的聊天记录跨轮稳定，KV 前缀才能复用
        self.assertTrue(text.splitlines()[-1].startswith('【现在】'), text.splitlines()[-1])


class TestRefTool(unittest.IsolatedAsyncioTestCase):
    """ 引用哪条由模型说了算：ref 填聊天记录里的 #标签，解析成那条消息的 svrid。 """

    SVRID = 770559124696026893

    def ctx(self):
        window = history_with(12, now=time.time())
        window.append('Alice', '[表情：熊猫]', int(time.time()), msg_id=self.SVRID)
        return make_ctx(make_msg(is_chatroom=True, content='这个怎么弄啊'), history=window)

    def plugin(self, turns, **kwargs):
        self.ai = ToolAi(turns)
        return ChatPlugin(self.ai, context_length=10, **kwargs)

    async def test_ref_handle_becomes_the_svrid(self):
        handle = message_handle(self.SVRID)
        plugin = self.plugin([AssistantTurn(tool_calls=(_call('reply', text='这熊猫', ref=handle),))])
        reply = await plugin.handle(self.ctx())
        self.assertEqual(reply.lines[0].ref, self.SVRID)

    async def test_ref_is_optional(self):
        plugin = self.plugin([AssistantTurn(tool_calls=(_call('reply', text='在'),))])
        reply = await plugin.handle(self.ctx())
        self.assertEqual(reply.lines[0].ref, 0)

    async def test_unknown_handle_still_sends_the_text(self):
        """ 标签对不上就不引用，但话照发——引错了比不引更糟，不回更糟。 """
        plugin = self.plugin([AssistantTurn(tool_calls=(_call('reply', text='在', ref='#nope'),))])
        reply = await plugin.handle(self.ctx())
        self.assertEqual(reply.lines[0].ref, 0)
        self.assertEqual(reply.texts, ['在'])
        # 回执说清楚为什么没引，模型下轮能看到
        _, note = ChatPlugin._resolve_ref({'ref': '#nope'}, self.ctx())
        self.assertIn('没有「#nope」', note)

    async def test_private_chat_does_not_quote(self):
        """ 私聊只有一个人，引用框对着谁都是他——模型填了 ref 也不引，候选里干脆不列。 """
        handle = message_handle(self.SVRID)
        plugin = self.plugin([AssistantTurn(tool_calls=(_call('reply', text='这熊猫', ref=handle),))])
        ctx = self.ctx()
        ctx.msg.is_chatroom = False
        reply = await plugin.handle(ctx)
        self.assertEqual(reply.texts, ['这熊猫'])
        self.assertEqual(reply.lines[0].ref, 0)
        self.assertNotIn(handle, self.ai.prompts[0])

class TestAtTool(unittest.IsolatedAsyncioTestCase):
    """ @ 人：模型给群昵称，wxid 由 Bot 查，只在群聊里且只在有这个人时生效。 """

    MEMBERS = {'张三': 'wxid_zhang'}

    def setUp(self):
        self.looked = []

    async def lookup(self, chat, name):
        self.looked.append((chat, name))
        return self.MEMBERS.get(name, '')

    def plugin(self, turns, **kwargs):
        self.ai = ToolAi(turns)
        return ChatPlugin(self.ai, context_length=10, member_lookup=self.lookup,
                          **kwargs)

    async def test_at_name_becomes_a_wxid_on_the_reply(self):
        plugin = self.plugin([AssistantTurn(tool_calls=(_call('reply', text='你问他去', at='张三'),))])
        reply = await plugin.handle(proactive_ctx())
        self.assertEqual(self.looked, [('456@chatroom', '张三')])
        self.assertEqual(reply.lines[0].at, 'wxid_zhang')
        self.assertEqual(reply.texts, ['你问他去'])

    async def test_at_is_optional(self):
        plugin = self.plugin([AssistantTurn(tool_calls=(_call('reply', text='在'),))])
        reply = await plugin.handle(proactive_ctx())
        self.assertEqual(self.looked, [])
        self.assertEqual(reply.lines[0].at, '')

    async def test_unknown_name_still_sends_the_text(self):
        """ @ 不到人不是不发这条消息的理由。 """
        plugin = self.plugin([AssistantTurn(tool_calls=(_call('reply', text='在', at='查无此人'),))])
        reply = await plugin.handle(proactive_ctx())
        self.assertEqual(reply.lines[0].at, '')
        self.assertEqual(reply.texts, ['在'])

    async def test_private_chat_cannot_at(self):
        plugin = self.plugin([AssistantTurn(tool_calls=(_call('reply', text='在', at='张三'),))])
        ctx = proactive_ctx()
        ctx.msg.is_chatroom = False
        reply = await plugin.handle(ctx)
        self.assertEqual(self.looked, [])
        self.assertEqual(reply.lines[0].at, '')

    async def test_each_reply_carries_its_own_at(self):
        """ 一轮里回两个人：两条各 @ 各的。

        以前 at 是「一轮一份」，第二条会把第一条的目标盖掉——两条都 @ 最后那个人。
        """
        members = {'张三': 'wxid_zhang', '李四': 'wxid_li'}

        async def lookup(chat, name):
            self.looked.append((chat, name))
            return members.get(name, '')

        calls = (_call('reply', text='在', at='张三'), _call('reply', text='来了', at='李四'))
        self.ai = ToolAi([AssistantTurn(tool_calls=calls)])
        plugin = ChatPlugin(self.ai, context_length=10, member_lookup=lookup,
                            agent_max_replies=2)
        reply = await plugin.handle(proactive_ctx())
        self.assertEqual([line.at for line in reply.lines], ['wxid_zhang', 'wxid_li'])
        self.assertEqual(reply.texts, ['在', '来了'])

    async def test_notes_tell_the_model_what_happened(self):
        """ 工具回执要说清楚 @ 成没成——模型看不到自己在群里发出去的样子。 """
        plugin = self.plugin([])
        ctx = proactive_ctx()
        wxid, note = await plugin._resolve_at({'at': '张三'}, ctx)
        self.assertEqual(wxid, 'wxid_zhang')
        self.assertIn('这条会 @张三', note)
        _, missing = await plugin._resolve_at({'at': '查无此人'}, ctx)
        self.assertIn('没找到叫', missing)
        self.assertEqual(await plugin._resolve_at({}, ctx), ('', ''))

class TestPatTool(unittest.IsolatedAsyncioTestCase):
    """ 拍一拍：模型主动拍人。私聊不用名字，群聊按群昵称，拍成了才能算「回应过」。 """

    def setUp(self):
        self.patted = []

    async def pat_action(self, chat, name):
        self.patted.append((chat, name))
        if name == '查无此人':
            return False, '群里没找到叫「查无此人」的人，没拍成。'
        return True, f'拍好了：你拍了拍{name or "对方"}。'

    def plugin(self, turns, **kwargs):
        self.ai = ToolAi(turns)
        return ChatPlugin(self.ai, context_length=10, pat=self.pat_action,
                          **kwargs)

    async def test_pat_is_called_and_reported_back(self):
        plugin = self.plugin([AssistantTurn(tool_calls=(_call('pat', target='张三'),)),
                              AssistantTurn(tool_calls=(_call('reply', text='拍你一下'),))])
        reply = await plugin.handle(proactive_ctx())

        self.assertIn('pat', self.ai.tools_seen[0])
        self.assertEqual(self.patted, [('456@chatroom', '张三')])
        self.assertIn('拍了拍张三', json.dumps(self.ai.rounds[1], ensure_ascii=False))
        self.assertEqual(reply.texts, ['拍你一下'])

    async def test_a_successful_pat_counts_as_a_response(self):
        """ 被拍了只拍回去也算回应，别再逼它额外说一句。 """
        msg = make_msg(is_chatroom=True, is_at_me=True, content='拍了拍 你')
        ctx = make_ctx(msg, history=history_with(12, now=time.time()))
        ai = ToolAi([AssistantTurn(tool_calls=(_call('pat', target='张三'),))])
        plugin = ChatPlugin(ai, context_length=10, pat=self.pat_action)
        reply = await plugin.handle(ctx)
        # 没有补一轪「必须回应」：拍了就算回应过
        self.assertNotIn('你还没给回应', json.dumps(ai.rounds, ensure_ascii=False))
        self.assertEqual(reply.texts, [])

    async def test_a_failed_pat_does_not_count_as_a_response(self):
        msg = make_msg(is_chatroom=True, is_at_me=True, content='拍了拍 你')
        ctx = make_ctx(msg, history=history_with(12, now=time.time()))
        ai = ToolAi([AssistantTurn(tool_calls=(_call('pat', target='查无此人'),)),
                     AssistantTurn(),
                     AssistantTurn(tool_calls=(_call('reply', text='在'),))])
        plugin = ChatPlugin(ai, context_length=10, pat=self.pat_action)
        reply = await plugin.handle(ctx)
        # 没拍成就不算回应，下一轮会被推到「必须回应」再补一句
        self.assertIn('你还没给回应', json.dumps(ai.rounds[2], ensure_ascii=False))
        self.assertEqual(reply.texts, ['在'])

    async def test_private_chat_pats_without_a_name(self):
        ctx = proactive_ctx()
        ctx.msg.is_chatroom = False
        plugin = self.plugin([AssistantTurn(tool_calls=(_call('pat'),)), AssistantTurn()])
        await plugin.handle(ctx)
        self.assertEqual(self.patted, [('456@chatroom', '')])

    async def test_tool_is_hidden_without_a_backend(self):
        ai = ToolAi([AssistantTurn()])
        await ChatPlugin(ai, context_length=10).handle(proactive_ctx())
        self.assertNotIn('pat', ai.tools_seen[0])


class TestAgentMode(unittest.IsolatedAsyncioTestCase):
    """ 单段式：判断和台词一次做完，回话就是 reply 工具调用，不再叫表达者。 """

    def plugin(self, turns, search=None, **kwargs):
        self.ai = ToolAi(turns)
        self.search = FakeEmojiSearch(search or {})
        return ChatPlugin(self.ai, context_length=10, emoji_search=self.search,
                          **kwargs)

    async def test_reply_tool_produces_the_text_directly(self):
        plugin = self.plugin([AssistantTurn(tool_calls=(_call('reply', text='这也太离谱了'),))])
        reply = await plugin.handle(proactive_ctx())
        self.assertEqual(reply.texts, ['这也太离谱了'])
        self.assertEqual(self.ai.reply_calls, [])       # 没走表达者
        self.assertIn('reply', self.ai.tools_seen[0])
        # reply 终止本轮：不再多跑一轮去确认，一次调用就够
        self.assertEqual(len(self.ai.rounds), 1)

    async def test_trace_records_which_message_it_quoted(self):
        """ 引了哪条要留痕：光看 transcript 分不出「在回这一轮」还是「在回更早的那条」。 """
        msg = make_msg(is_chatroom=True, is_at_me=True, content='继续')
        quoted = 7100000000000000001
        history = sample_history()
        history.append('Alice', '周末去爬山不', 2, msg_id=quoted)
        history.append('Alice', '继续', 3, msg_id=msg.msg_id)
        plugin = self.plugin([AssistantTurn(tool_calls=(
            _call('reply', text='爬', ref=message_handle(quoted)),))])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'trace.jsonl'
            plugin.tracer = TraceLogger(path)
            reply = await plugin.handle(make_ctx(msg, history, fresh=(msg,)))
            record = json.loads(path.read_text(encoding='utf-8').strip())

        self.assertEqual(reply.texts, ['爬'])
        self.assertEqual(record['refs'], [quoted])

    async def test_replies_over_the_cap_are_refused(self):
        """ 上限是防刷屏的闸门：超出的那条被拒，剩下的下轮再说。 """
        calls = tuple(_call('reply', text=f'第{i}条') for i in range(1, 4))
        plugin = self.plugin([AssistantTurn(tool_calls=calls)], agent_max_replies=2)
        reply = await plugin.handle(proactive_ctx())
        self.assertEqual(reply.texts, ['第1条', '第2条'])

    async def test_plan_before_reply_puts_intent_and_style_next_to_the_line(self):
        """ 先 plan 再 reply：意图和表达风格写进上下文，台词在**下一次**生成里写。

        风格规则在 system prompt 最上面，离写台词隔了一百多行；plan 的回执把它再贴一遍，
        模型写这一句的时候眼前就是它——这是 agent 模式比两段式生硬的主要修法。
        """
        plugin = self.plugin([AssistantTurn(tool_calls=(_call('plan', intent='顺着接一句吐槽',
                                                              emotion='无奈'),)),
                              AssistantTurn(tool_calls=(_call('reply', text='那就不干 躺着吧'),))])
        reply = await plugin.handle(proactive_ctx())

        self.assertEqual(reply.texts, ['那就不干 躺着吧'])
        self.assertEqual(len(self.ai.rounds), 2)                # plan 一轮 + reply 一轮
        carried = json.dumps(self.ai.rounds[1], ensure_ascii=False)
        self.assertIn('想表达「顺着接一句吐槽」', carried)
        self.assertIn('情绪「无奈」', carried)
        self.assertIn('像微信打字', carried)                      # 人设里的表达风格也带上了
        self.assertIn('现在把这一句写出来', carried)

    async def test_reply_without_a_plan_still_goes_out(self):
        """ 提示词要求先 plan，但不硬拦：没 plan 就是少一条留痕，不能因此不回。 """
        plugin = self.plugin([AssistantTurn(tool_calls=(_call('reply', text='在'),))])
        reply = await plugin.handle(proactive_ctx())
        self.assertEqual(reply.texts, ['在'])

    async def test_empty_intent_tells_it_to_say_what_it_means(self):
        plugin = self.plugin([AssistantTurn(tool_calls=(_call('plan', intent=''),)),
                              AssistantTurn(tool_calls=(_call('reply', text='在'),))])
        reply = await plugin.handle(proactive_ctx())
        self.assertEqual(reply.texts, ['在'])
        self.assertIn('intent 是空的', json.dumps(self.ai.rounds[1], ensure_ascii=False))

    async def test_plan_tool_is_offered(self):
        """ 「开口前先想一步」是 agent 模式的一部分。 """
        plugin = self.plugin([AssistantTurn()])
        await plugin.handle(proactive_ctx())
        self.assertIn('plan', self.ai.tools_seen[0])

    async def test_sticker_reaches_the_model_in_a_hot_group(self):
        """ 表情消息不再被入口挡掉（历史里的 `[表情：…]` 自带语义）。 """
        plugin = self.plugin([AssistantTurn(tool_calls=(_call('reply', text='哈哈哈哈'),))])
        ctx = make_ctx(make_msg(is_chatroom=True, msg_type=47, content='[表情：摊倒]'),
                       history=history_with(12, now=time.time()))
        reply = await plugin.handle(ctx)
        self.assertEqual(reply.texts, ['哈哈哈哈'])

    async def test_sticker_in_a_quiet_group_stays_silent(self):
        plugin = self.plugin([])
        ctx = make_ctx(make_msg(is_chatroom=True, msg_type=47, content='[表情：摊倒]'),
                       history=history_with(1, now=time.time()))
        self.assertIsNone(await plugin.handle(ctx))

    async def test_transcribed_voice_reaches_the_model(self):
        """ 历史里已经是 `[语音 3″：文本]`，入口不该再把它挡掉。 """
        plugin = self.plugin([AssistantTurn(tool_calls=(_call('reply', text='骑车注意安全'),))])
        ctx = make_ctx(make_msg(is_chatroom=True, msg_type=34, content='[语音 3″：我正在骑车回家]'),
                       history=history_with(12, now=time.time()))
        reply = await plugin.handle(ctx)
        self.assertEqual(reply.texts, ['骑车注意安全'])

    async def test_per_conversation_persona_reaches_the_prompt(self):
        """ 分会话人设只盖这个会话，且真的进了 system prompt。 """
        store = PersonaStore(Path(tempfile.mkdtemp()) / 'personas.json')
        store.set('测试群', {'personality': '是只猫', 'aliases': '阿猫'})
        plugin = self.plugin([AssistantTurn(tool_calls=(_call('reply', text='喵'),))], personas=store)
        reply = await plugin.handle(proactive_ctx())
        self.assertEqual(reply.texts, ['喵'])
        self.assertIn('是只猫', self.ai.prompts[0])
        self.assertIn('阿猫', self.ai.prompts[0])
        # 没覆盖的会话回落全局
        self.assertEqual(plugin.persona_for('另一个群').personality, Persona().personality)

    async def test_sticker_and_text_in_one_turn(self):
        plugin = self.plugin(
            [AssistantTurn(tool_calls=(_call('search_emoji', keyword='无语'),)),
             AssistantTurn(tool_calls=(_call('send_emoji', index=1),
                                       _call('reply', text='确实'))),
             ],
            search={'无语': [_emoji()]})
        reply = await plugin.handle(proactive_ctx())
        self.assertEqual(reply.texts, ['确实'])
        self.assertEqual(reply.emoji.desc, '无语')

    async def test_no_tool_call_is_silent(self):
        plugin = self.plugin([AssistantTurn(content='接不上')])
        reply = await plugin.handle(proactive_ctx())
        self.assertEqual(reply.texts, [])
        self.assertTrue(reply.stop)

    async def test_must_speak_gets_one_forced_round(self):
        """ 被点名却什么都不调：只补一轮提醒。 """
        plugin = self.plugin([AssistantTurn(),
                              AssistantTurn(tool_calls=(_call('reply', text='在'),))])
        reply = await plugin.handle(make_ctx(make_msg(is_chatroom=False)))
        self.assertEqual(reply.texts, ['在'])
        self.assertIn('必须回应', json.dumps(self.ai.rounds[1], ensure_ascii=False))

    async def test_no_forced_reply_when_i_already_have_the_last_word(self):
        """ 这条发出时上一轮还在跑，自己的回复已经落在它后面——不再强制回一遍。 """
        history = sample_history()
        history.append('Bot', '在呢 咋啦', 2, is_self=True)
        plugin = self.plugin([AssistantTurn()])
        reply = await plugin.handle(make_ctx(make_msg(is_chatroom=False, content='在吗'), history))
        self.assertEqual(reply.texts, [])
        self.assertEqual(len(self.ai.rounds), 1)          # 没有第二轮「必须回应」提醒

    async def test_agent_prompt_tells_it_the_reply_cap(self):
        """ 一轮想说的要一次调完，所以模板里得写明最多几条。 """
        plugin = self.plugin([], agent_max_replies=3)
        prompt = await plugin._agent_prompt(proactive_ctx(), ('reply',))
        self.assertIn('最多 3 条', prompt)

    async def test_agent_prompt_switches_on_available_tools(self):
        plugin = self.plugin([])
        prompt = await plugin._agent_prompt(proactive_ctx(), ('search_emoji', 'send_emoji', 'reply'))
        self.assertIn('reply', prompt)
        self.assertIn('search_emoji', prompt)
        self.assertIn('什么工具都不调', prompt)
        self.assertNotIn('只输出一个 JSON', prompt)
        # 没接表情搜索时提示词里不能提它，否则模型会去调一个不存在的工具
        bare = await plugin._agent_prompt(proactive_ctx(), ('reply',))
        self.assertNotIn('search_emoji', bare)

    async def test_model_failure_is_reported_as_an_error_not_silence(self):
        """ 调不通和「模型决定不说话」都是空回复，面板上必须分得开。

        不然 429 / 超时会被当成模型的选择，看面板的人只看到一句「不发言」，
        根本不知道接口根本没调通。
        """
        class Boom(ToolAi):
            async def chat_tools(self, prompt, messages, tools):
                raise RuntimeError('429 用量超了')

        plugin = ChatPlugin(Boom([]), context_length=10)
        seen: list[tuple[str, str]] = []

        def record(event):
            seen.append((event.status, event.detail))

        bus.subscribe(record)
        try:
            reply = await plugin.handle(proactive_ctx())
        finally:
            bus.unsubscribe(record)

        self.assertEqual(reply.texts, [])
        self.assertTrue(reply.stop)
        self.assertIn(('planner_failed', '429 用量超了'), seen)
        self.assertNotIn('planner_silent', [status for status, _ in seen])


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

    def test_only_whitelisted_face_names_survive(self):
        """ 名字不在内置名单里，发出去就是纯文本方括号，一眼看穿。 """
        self.assertEqual(clean_reply('好[捂脸]'), '好[捂脸]')
        self.assertEqual(clean_reply('这也太[抓马]'), '这也太……')   # 删成「这也太」就是残句
        self.assertEqual(clean_reply('好[笑哭]'), '好……')          # 微信没有 [笑哭]
        self.assertEqual(clean_reply('[图片：一只橘猫] 真可爱'), '真可爱')

    def test_does_not_touch_body(self):
        self.assertEqual(clean_reply('在的，你说「那个」是什么意思'), '在的，你说「那个」是什么意思')


class TestCleanReplyInPlugin(unittest.IsolatedAsyncioTestCase):
    async def test_plugin_cleans_before_sending(self):
        ai = FakeAi(turn=_reply('Bot：「收到」'))
        plugin = ChatPlugin(ai, context_length=10)
        reply = await plugin.handle(make_ctx(make_msg(is_at_me=True)))
        self.assertEqual(reply.texts, ['收到'])

    async def test_all_empty_becomes_stop(self):
        ai = FakeAi(turn=_reply('Bot：'))
        plugin = ChatPlugin(ai, context_length=10)
        reply = await plugin.handle(make_ctx(make_msg(is_at_me=True)))
        self.assertTrue(reply.stop)
        self.assertEqual(reply.texts, [])

    async def test_alias_prefix_is_also_stripped(self):
        ai = FakeAi(turn=_reply('雨姐：收到'))
        plugin = ChatPlugin(ai, context_length=10, persona=Persona(aliases=('雨姐',)))
        reply = await plugin.handle(make_ctx(make_msg(is_at_me=True)))
        self.assertEqual(reply.texts, ['收到'])


if __name__ == '__main__':
    unittest.main()
