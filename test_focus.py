""" 专注模式与门控的接线：热起来就盯一会儿，期间不看阈值也不退避。 """

import time
import types
import unittest

from context import ChatWindow, MessageContext
from focus import FocusMode
from necessity import IdleBackoff
from plugin import ChatPlugin, ReplyPolicy
from tools import AssistantTurn, ToolCall

# 决策者默认「说一句」；传 AssistantTurn() 表示这一轮不发言
_REPLY = AssistantTurn(tool_calls=(ToolCall('reply-1', 'reply', {'text': '收到'}),))


class FakeAi:
    """ 决策者走 `chat_tools`（工具模式），表达者走 `chat`。 """

    def __init__(self, reply=('收到',), turn=_REPLY):
        self.reply = list(reply)
        self.turn = turn
        self.calls = []

    async def chat_tools(self, prompt, messages, tools):
        self.calls.append((prompt, messages[0]['content']))
        return self.turn

    async def chat(self, prompt, message):
        self.calls.append((prompt, message))
        return self.reply

    @property
    def planner_calls(self):
        # agent 模式只有一次调用（`chat_tools`）；名字留着，断言少改
        return self.calls


def make_ctx(others: int, content='大家好啊', now=None):
    now = now or time.time()
    window = ChatWindow()
    for i in range(others):
        window.append('Alice', f'msg{i}', int(now - i))
    msg = types.SimpleNamespace(msg_type=1, is_chatroom=True, is_at_me=False, content=content,
                                sender_name='Alice', reply_target='456@chatroom')

    async def get_media_bytes():
        return b''

    msg.get_media_bytes = get_media_bytes
    return MessageContext(msg=msg, history=window, self_name='Bot', contact='测试群')


class TestFocusGate(unittest.TestCase):
    def test_hot_group_enters_focus(self):
        policy = ReplyPolicy(focus=FocusMode(enter_activity=3))
        # 冷清群里这句闲话本来不够分
        self.assertIsNone(policy.evaluate(make_ctx(0, content='大家好啊'), now=time.time()))
        # 聊热了进专注；但只是加分，不再整个豁免门控（存在感惩罚仍然生效）
        result = policy.evaluate(make_ctx(10, content='这个怎么弄啊'), now=time.time())
        self.assertIsNotNone(result)
        self.assertTrue(result.focused)

    def test_focus_bypasses_idle_backoff(self):
        now = time.time()
        ctx = make_ctx(10, content='这个怎么弄啊', now=now)
        backoff = IdleBackoff()
        for _ in range(3):                     # 连着几轮不开口，本该退避
            backoff.record(ctx.contact, spoke=False, now=now, history=ctx.history)
        self.assertFalse(backoff.allows(ctx.contact, now, ctx.history))
        # 没开专注：退避拦下这一轮
        self.assertIsNone(ReplyPolicy(backoff=backoff).evaluate(ctx, now=now))
        # 开着专注：同样的退避历史不拦
        result = ReplyPolicy(backoff=backoff, focus=FocusMode(enter_activity=3)).evaluate(ctx, now=now)
        self.assertIsNotNone(result)
        self.assertTrue(result.focused)

    def test_disabled_focus_never_bypasses(self):
        policy = ReplyPolicy(focus=FocusMode(enabled=False, enter_activity=1))
        self.assertIsNone(policy.evaluate(make_ctx(5, content='今天天气不错'), now=time.time()))


class TestFocusPrompt(unittest.IsolatedAsyncioTestCase):
    async def test_focus_note_reaches_both_models(self):
        ai = FakeAi()
        plugin = ChatPlugin(ai, context_length=10, policy=ReplyPolicy(focus=FocusMode(enter_activity=3)))
        reply = await plugin.handle(make_ctx(10, content='这个怎么弄啊'))

        self.assertEqual(reply.texts, ['收到'])
        self.assertIn('【专注模式】', ai.planner_calls[0][1])
        self.assertIn('【专注模式】', ai.calls[-1][1])          # 表达者那份也带上

    async def test_no_focus_note_when_not_focused(self):
        ai = FakeAi()
        plugin = ChatPlugin(ai, context_length=10, policy=ReplyPolicy(focus=FocusMode(enter_activity=99)))
        await plugin.handle(make_ctx(10, content='这个怎么弄啊'))
        self.assertTrue(ai.calls)                                  # 走了模型，只是没进专注
        self.assertNotIn('【专注模式】', ai.calls[-1][1])


if __name__ == '__main__':
    unittest.main()
