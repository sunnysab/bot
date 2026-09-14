""" Bot 编排自检：决策入队、stop 语义、静音、自身消息忽略。不连 relay。 """

import asyncio
import types
import unittest

from bot import Bot
from plugin import EndProcessing, Plugin, Reply


class FakeWechat:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []
        self.all_contacts: dict[str, str] = {}

    @property
    def wxid(self) -> str:
        return 'wxid_bot'

    async def send_text(self, text, receiver, at_list=''):
        self.sent.append((receiver, text))

    async def get_display_name(self, wxid, chatroom=''):
        return 'Bot'


class FakePlugin(Plugin):
    def __init__(self, texts=None, stop=False):
        self.texts = texts or []
        self._stop = stop

    async def handle(self, ctx):
        return Reply(texts=list(self.texts), stop=self._stop)


def make_msg(text='hello', is_self=False, msg_type=1):
    return types.SimpleNamespace(msg_type=msg_type, content=text, clean_text=text, is_self=is_self,
                                 reply_target='456@chatroom', sender_name='Alice', create_time=100,
                                 is_chatroom=True, is_at_me=True)


class TestBotOnMessage(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = Bot('127.0.0.1', 9999, x_delay=0, send_interval=0)
        self.bot._wechat = FakeWechat()
        await self.bot.outbox.start()

    async def asyncTearDown(self):
        await self.bot.outbox.stop()

    def _window(self):
        return self.bot._history.get_context('456@chatroom')

    async def test_reply_is_sent_and_recorded(self):
        self.bot.set_default_plugin(FakePlugin(texts=['hi']))
        await self.bot.on_message(make_msg())
        await asyncio.sleep(0.05)

        self.assertEqual(self.bot._wechat.sent, [('456@chatroom', 'hi')])
        self.assertEqual([r.text for r in self._window()], ['hello', 'hi'])

    async def test_stop_plugin_short_circuits(self):
        self.bot.set_default_plugin([FakePlugin(texts=['a'], stop=True), FakePlugin(texts=['b'])])
        await self.bot.on_message(make_msg())
        await asyncio.sleep(0.05)

        self.assertEqual(self.bot._wechat.sent, [('456@chatroom', 'a')])

    async def test_no_reply_sends_nothing_but_records(self):
        self.bot.set_default_plugin(FakePlugin(texts=[]))
        await self.bot.on_message(make_msg())
        await asyncio.sleep(0.05)

        self.assertEqual(self.bot._wechat.sent, [])
        self.assertEqual(len(self._window()), 1)

    async def test_self_message_is_ignored(self):
        self.bot.set_default_plugin(FakePlugin(texts=['hi']))
        await self.bot.on_message(make_msg(is_self=True))
        await asyncio.sleep(0.05)

        self.assertEqual(self.bot._wechat.sent, [])
        self.assertEqual(len(self._window()), 0)

    async def test_attached_plugin_overrides_defaults(self):
        self.bot.set_default_plugin(FakePlugin(texts=['hi']))
        self.bot.attach('456@chatroom', EndProcessing())
        await self.bot.on_message(make_msg())
        await asyncio.sleep(0.05)

        self.assertEqual(self.bot._wechat.sent, [])


if __name__ == '__main__':
    unittest.main()
