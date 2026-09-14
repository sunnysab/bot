""" 插件自检：群聊 @ 门禁、私聊直通、图片直传，以及复读机不跟媒体占位标记。 """

import base64
import time
import types
import unittest

from context import ChatWindow, MessageContext
from plugin import ChatPlugin, RepeatPlugin, ReplyPolicy


class FakeAi:
    def __init__(self):
        self.calls = []

    async def chat(self, prompt, message):
        self.calls.append((prompt, message))
        return ['收到']


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
    return MessageContext(msg=msg, history=history or sample_history(), self_name='Bot', contact='测试群')


class TestChatPlugin(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ai = FakeAi()
        self.plugin = ChatPlugin(self.ai, context_length=10)

    async def test_group_without_mention_is_silent(self):
        reply = await self.plugin.handle(make_ctx(make_msg(is_chatroom=True, is_at_me=False)))
        self.assertIsNone(reply)
        self.assertEqual(self.ai.calls, [])

    async def test_group_with_mention_replies(self):
        reply = await self.plugin.handle(make_ctx(make_msg(is_chatroom=True, is_at_me=True)))
        self.assertEqual(reply.texts, ['收到'])
        self.assertEqual(len(self.ai.calls), 1)
        self.assertIsInstance(self.ai.calls[0][1], str)

    async def test_private_replies_without_mention(self):
        reply = await self.plugin.handle(make_ctx(make_msg(is_chatroom=False)))
        self.assertEqual(reply.texts, ['收到'])

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

    async def test_group_name_mention_replies_without_at(self):
        reply = await self.plugin.handle(make_ctx(make_msg(is_chatroom=True, content='Bot 在吗')))
        self.assertEqual(reply.texts, ['收到'])

    async def test_cooldown_suppresses_name_mention(self):
        self.plugin.policy.note_reply('测试群', time.monotonic())
        reply = await self.plugin.handle(make_ctx(make_msg(is_chatroom=True, content='Bot 在吗')))
        self.assertIsNone(reply)
        self.assertEqual(self.ai.calls, [])

    async def test_at_bypasses_cooldown(self):
        self.plugin.policy.note_reply('测试群', time.monotonic())
        reply = await self.plugin.handle(make_ctx(make_msg(is_chatroom=True, is_at_me=True, content='Bot')))
        self.assertEqual(reply.texts, ['收到'])

    async def test_private_bypasses_cooldown(self):
        self.plugin.policy.note_reply('测试群', time.monotonic())
        reply = await self.plugin.handle(make_ctx(make_msg(is_chatroom=False)))
        self.assertEqual(reply.texts, ['收到'])


class TestReplyPolicy(unittest.TestCase):
    def setUp(self):
        self.policy = ReplyPolicy(cooldown=30)

    def should(self, is_chatroom, is_at_me, content=''):
        ctx = make_ctx(make_msg(is_chatroom=is_chatroom, is_at_me=is_at_me, content=content))
        return self.policy.should_reply(ctx, now=1000.0)

    def test_private_always(self):
        self.assertTrue(self.should(is_chatroom=False, is_at_me=False))

    def test_group_requires_at_or_mention(self):
        self.assertFalse(self.should(is_chatroom=True, is_at_me=False, content='大家好啊'))
        self.assertTrue(self.should(is_chatroom=True, is_at_me=True))
        self.assertTrue(self.should(is_chatroom=True, is_at_me=False, content='@Bot 帮个忙'))

    def test_cooldown_only_blocks_mentions(self):
        self.policy.note_reply('测试群', 990.0)
        self.assertFalse(self.should(is_chatroom=True, is_at_me=False, content='Bot 在吗'))
        self.assertTrue(self.should(is_chatroom=True, is_at_me=True))


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
