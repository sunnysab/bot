""" ChatPlugin 自检：群聊 @ 门禁、私聊直通、图片作为 content block 直传。 """

import base64
import types
import unittest

from context import ChatWindow
from plugin import ChatPlugin


class FakeAi:
    def __init__(self):
        self.calls = []

    async def chat(self, prompt, message):
        self.calls.append((prompt, message))
        return ['收到']


def make_msg(msg_type=1, is_chatroom=False, is_at_me=False, media=b''):
    msg = types.SimpleNamespace(msg_type=msg_type, is_chatroom=is_chatroom, is_at_me=is_at_me,
                                reply_target='456@chatroom')

    async def get_media_bytes():
        return media

    msg.get_media_bytes = get_media_bytes
    return msg


def sample_history():
    window = ChatWindow()
    window.append('Alice', '在吗', 1)
    return window


class TestChatPlugin(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ai = FakeAi()
        self.plugin = ChatPlugin(self.ai, context_length=10)
        self.kwargs = dict(wechat=None, self_name='Bot', contact='测试群')

    async def test_group_without_mention_is_silent(self):
        reply, cont = await self.plugin.handle(make_msg(is_chatroom=True, is_at_me=False),
                                               history=sample_history(), **self.kwargs)
        self.assertIsNone(reply)
        self.assertTrue(cont)
        self.assertEqual(self.ai.calls, [])

    async def test_group_with_mention_replies(self):
        reply, _ = await self.plugin.handle(make_msg(is_chatroom=True, is_at_me=True),
                                            history=sample_history(), **self.kwargs)
        self.assertEqual(reply, ['收到'])
        self.assertEqual(len(self.ai.calls), 1)
        self.assertIsInstance(self.ai.calls[0][1], str)

    async def test_private_replies_without_mention(self):
        reply, _ = await self.plugin.handle(make_msg(is_chatroom=False),
                                            history=sample_history(), **self.kwargs)
        self.assertEqual(reply, ['收到'])

    async def test_image_is_sent_as_content_block(self):
        image = b'\xff\xd8fake-jpeg'
        reply, _ = await self.plugin.handle(make_msg(msg_type=3, is_chatroom=False, media=image),
                                            history=sample_history(), **self.kwargs)
        self.assertEqual(reply, ['收到'])
        content = self.ai.calls[0][1]
        self.assertIsInstance(content, list)
        self.assertEqual(content[0]['type'], 'text')
        self.assertEqual(content[1]['type'], 'image_url')
        expected = 'data:image/jpeg;base64,' + base64.b64encode(image).decode()
        self.assertEqual(content[1]['image_url']['url'], expected)


if __name__ == '__main__':
    unittest.main()
