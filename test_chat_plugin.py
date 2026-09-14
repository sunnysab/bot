""" 插件自检：回复策略、群聊主动参与、私聊直通、图片直传，以及复读机不跟媒体占位标记。 """

import base64
import time
import types
import unittest

from context import ChatWindow, MessageContext
from plugin import ChatPlugin, RepeatPlugin, ReplyPolicy


class FakeAi:
    def __init__(self, response=('收到',)):
        self.responses = list(response) if response else None
        self.calls = []

    async def chat(self, prompt, message):
        self.calls.append((prompt, message))
        return self.responses


def make_msg(msg_type=1, is_chatroom=False, is_at_me=False, media=b'', content=''):
    msg = types.SimpleNamespace(msg_type=msg_type, is_chatroom=is_chatroom, is_at_me=is_at_me,
                                content=content, reply_target='456@chatroom')

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
        window.append(f'用户{i}', f'msg{i}', int(now - i))
    return window


class TestChatPlugin(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ai = FakeAi()
        self.plugin = ChatPlugin(self.ai, context_length=10)

    def throttle(self):
        self.plugin.policy.note_reply('测试群', time.time())

    async def test_group_message_participates_proactively(self):
        reply = await self.plugin.handle(make_ctx(make_msg(is_chatroom=True)))
        self.assertEqual(reply.texts, ['收到'])
        self.assertEqual(len(self.ai.calls), 1)

    async def test_group_proactive_is_throttled_after_speaking(self):
        self.throttle()
        reply = await self.plugin.handle(make_ctx(make_msg(is_chatroom=True)))
        self.assertIsNone(reply)
        self.assertEqual(self.ai.calls, [])

    async def test_group_at_bypasses_throttle(self):
        self.throttle()
        reply = await self.plugin.handle(make_ctx(make_msg(is_chatroom=True, is_at_me=True)))
        self.assertEqual(reply.texts, ['收到'])

    async def test_group_name_mention_bypasses_throttle(self):
        self.throttle()
        reply = await self.plugin.handle(make_ctx(make_msg(is_chatroom=True, content='Bot 在吗')))
        self.assertEqual(reply.texts, ['收到'])

    async def test_private_bypasses_throttle(self):
        self.throttle()
        reply = await self.plugin.handle(make_ctx(make_msg(is_chatroom=False)))
        self.assertEqual(reply.texts, ['收到'])

    async def test_silent_model_does_not_reply(self):
        self.plugin.ai = FakeAi(response=None)
        reply = await self.plugin.handle(make_ctx(make_msg(is_chatroom=False)))
        self.assertEqual(reply.texts, [])
        self.assertTrue(reply.stop)

    async def test_image_is_sent_as_content_block(self):
        image = b'\xff\xd8fake-jpeg'
        reply = await self.plugin.handle(make_ctx(make_msg(msg_type=3, is_chatroom=False, media=image)))
        self.assertEqual(reply.texts, ['收到'])
        content = self.ai.calls[0][1]
        self.assertIsInstance(content, list)
        self.assertEqual(content[0]['type'], 'text')
        self.assertEqual(content[1]['type'], 'image_url')
        expected = 'data:image/jpeg;base64,' + base64.b64encode(image).decode()
        self.assertEqual(content[1]['image_url']['url'], expected)


class TestReplyPolicy(unittest.TestCase):
    def setUp(self):
        self.policy = ReplyPolicy(activity_window=180.0, factor=2.0, wait_min=30.0, wait_max=180.0)

    def ctx(self, is_chatroom=True, is_at_me=False, content='', history=None):
        history = history if history is not None else ChatWindow()
        return make_ctx(make_msg(is_chatroom=is_chatroom, is_at_me=is_at_me, content=content), history=history)

    def test_private_always(self):
        self.assertTrue(self.policy.should_reply(self.ctx(is_chatroom=False), now=1000.0))

    def test_at_and_mention_bypass_throttle(self):
        self.policy.note_reply('测试群', 1000.0)
        self.assertTrue(self.policy.should_reply(self.ctx(is_at_me=True), now=1000.0))
        self.assertTrue(self.policy.should_reply(self.ctx(content='Bot 在吗'), now=1000.0))

    def test_first_group_message_passes(self):
        self.assertTrue(self.policy.should_reply(self.ctx(content='大家好啊'), now=1000.0))

    def test_quiet_group_waits_max(self):
        self.policy.note_reply('测试群', 1000.0)
        self.assertFalse(self.policy.should_reply(self.ctx(content='大家好啊'), now=1179.0))
        self.assertTrue(self.policy.should_reply(self.ctx(content='大家好啊'), now=1180.0))

    def test_active_group_waits_less(self):
        # 12 条别人消息 → 2 * 180 / 12 = 30s
        history = history_with(12, now=1000.0)
        self.policy.note_reply('测试群', 1000.0)
        self.assertFalse(self.policy.should_reply(self.ctx(content='大家好啊', history=history), now=1029.0))
        self.assertTrue(self.policy.should_reply(self.ctx(content='大家好啊', history=history), now=1030.0))

    def test_own_messages_do_not_count_as_activity(self):
        history = ChatWindow()
        for i in range(5):
            history.append('Bot', f'我发的{i}', int(1000 - i), is_self=True)
        history.append('张三', '别人说的', 1000)
        self.policy.note_reply('测试群', 1000.0)
        # 若把自己的消息也算进去 (6 条) 等待会是 60s；实际上只有 1 条别人消息，等待封顶 180s
        self.assertFalse(self.policy.should_reply(self.ctx(content='大家好啊', history=history), now=1060.0))


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
