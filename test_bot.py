""" Bot 编排自检：决策入队、stop 语义、静音、自身消息忽略。不连 relay。 """

import asyncio
import types
import unittest

from bot import Bot, PayloadProcessor
from context import ChatWindow
from media import Media
from plugin import EndProcessing, Plugin, Reply


class FakeWechat:
    def __init__(self, fail=False):
        self.sent: list[tuple[str, str]] = []
        self.attempts: list[tuple[str, str]] = []
        self.fail = fail
        self.all_contacts: dict[str, str] = {}

    @property
    def wxid(self) -> str:
        return 'wxid_bot'

    async def send_text(self, text, receiver, at_list=''):
        self.attempts.append((receiver, text))
        if self.fail:
            raise RuntimeError('解析长链 newsendmsg 响应失败: invalid data: unsupported wiretype 7')
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
    return types.SimpleNamespace(msg_type=msg_type, content=text, clean_text=text, text=text, is_self=is_self,
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

    async def test_send_failure_still_records_history(self):
        """ relay 回执解析失败但消息已投递：不能重试，历史也要照写。 """
        self.bot.set_default_plugin(FakePlugin(texts=['hi', 'there']))
        self.bot._wechat.fail = True
        await self.bot.on_message(make_msg())
        await asyncio.sleep(0.05)

        self.assertEqual(self.bot._wechat.attempts, [('456@chatroom', 'hi'), ('456@chatroom', 'there')])
        self.assertNotIn(('456@chatroom', 'hi'), self.bot._wechat.sent)  # 没有重发
        self.assertEqual([r.text for r in self._window()], ['hello', 'hi', 'there'])

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


class FakeDescriber:
    def __init__(self):
        self.scheduled = []

    def schedule(self, record):
        self.scheduled.append(record)


class TestHistoryMediaScheduling(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = Bot('127.0.0.1', 9999, describe_history_limit=2)
        self.bot._describer = FakeDescriber()

    def test_only_newest_media_up_to_limit(self):
        window = ChatWindow()
        for i in range(5):
            window.append('Alice', '[图片]', i, media=Media('image', f'md5-{i}'), download=lambda: None)
        self.bot._schedule_history_media(window)

        self.assertEqual([r.t for r in self.bot._describer.scheduled], [4, 3])

    def test_no_describer_is_noop(self):
        self.bot._describer = None
        window = ChatWindow()
        window.append('Alice', '[图片]', 1, media=Media('image', 'md5-x'), download=lambda: None)
        self.bot._schedule_history_media(window)  # 不报错即可


class TestPayloadProcessor(unittest.IsolatedAsyncioTestCase):
    async def test_image_becomes_marker(self):
        msg = make_msg(msg_type=3)
        msg.content = msg.clean_text = '<?xml version="1.0"?><msg><img md5="abc" /></msg>'
        out = await PayloadProcessor().process(msg)
        self.assertEqual(out.content, '[图片]')

    async def test_emoji_keeps_description(self):
        msg = make_msg(msg_type=47)
        msg.content = msg.clean_text = '<msg><emoji desc="ChEKB2RlZmF1bHQSBuWVp+WVpw==" /></msg>'
        out = await PayloadProcessor().process(msg)
        self.assertEqual(out.content, '[表情：啧啧]')

    async def test_emoji_without_description_falls_back(self):
        msg = make_msg(msg_type=47)
        msg.content = msg.clean_text = '<msg><emoji desc="" /></msg>'
        out = await PayloadProcessor().process(msg)
        self.assertEqual(out.content, '[表情]')

    async def test_link_does_not_leak_xml(self):
        msg = make_msg(msg_type=49)
        msg.content = msg.clean_text = ('<msg><appmsg><title>一篇文章</title><type>5</type>'
                                        '<appname>某公众号</appname></appmsg></msg>')
        out = await PayloadProcessor().process(msg)
        self.assertEqual(out.content, '[某公众号] 一篇文章')

    async def test_location_and_video(self):
        msg = make_msg(msg_type=48)
        msg.content = msg.clean_text = '<msg><location label="海淀区京新高速" /></msg>'
        self.assertEqual((await PayloadProcessor().process(msg)).content, '[位置：海淀区京新高速]')
        video = make_msg(msg_type=43)
        video.content = video.clean_text = '<msg><videomsg /></msg>'
        self.assertEqual((await PayloadProcessor().process(video)).content, '[视频]')


if __name__ == '__main__':
    unittest.main()
