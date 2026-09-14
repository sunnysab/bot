""" Bot 编排自检：决策入队、stop 语义、静音、自身消息忽略。不连 relay。 """

import asyncio
import types
import unittest

from bot import EMOJI_LOCAL_ENOUGH, Bot, PayloadProcessor
from context import ChatWindow
from emojis import Emoji, EmojiLibrary
from media import Media
from plugin import EndProcessing, Plugin, Reply


class FakeWechat:
    def __init__(self, fail=False):
        self.sent: list[tuple[str, str]] = []
        self.attempts: list[tuple[str, str]] = []
        self.fail = fail
        self.all_contacts: dict[str, str] = {}
        self.emojis_sent: list[tuple[str, str]] = []    # (receiver, md5)
        self.emoji_queries: list[str] = []
        self.online: list = []                          # 假线上搜索结果

    async def search_emoji(self, keyword, limit=10):
        self.emoji_queries.append(keyword)
        return list(self.online)

    async def send_emoji(self, emoji, receiver):
        self.emojis_sent.append((receiver, emoji.md5))

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
    def __init__(self, texts=None, stop=False, emoji=None):
        self.texts = texts or []
        self._stop = stop
        self.emoji = emoji

    async def handle(self, ctx):
        return Reply(texts=list(self.texts), stop=self._stop, emoji=self.emoji)


def make_msg(text='hello', is_self=False, msg_type=1):
    return types.SimpleNamespace(msg_type=msg_type, content=text, clean_text=text, text=text, is_self=is_self,
                                 reply_target='456@chatroom', sender_name='Alice', create_time=100,
                                 is_chatroom=True, is_at_me=True)


async def _fake_image():
    return b'png'


def make_sticker(md5='a' * 32, cdnurl='http://cdn/x', aeskey='key'):
    """ 一条表情消息的 XML。微信的表情载荷自带下载信息，所以能原样发回去。 """
    return (f'<msg><emoji fromusername="wxid_alice" md5="{md5}" len="348401" '
            f'cdnurl="{cdnurl}" encrypturl="{cdnurl}" aeskey="{aeskey}" width="300" height="300" '
            f'desc="ChEKB2RlZmF1bHQSBuaRiuWAkg==" /></msg>')


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


class TestEmojiStealing(unittest.IsolatedAsyncioTestCase):
    """ 偷表情：群里发的存下来，以后遇到合适的场合能原样发回去。 """

    async def asyncSetUp(self):
        self.bot = Bot('127.0.0.1', 9999, x_delay=0, send_interval=0, emojis=EmojiLibrary())
        self.bot._wechat = FakeWechat()
        await self.bot.outbox.start()

    async def asyncTearDown(self):
        await self.bot.outbox.stop()

    async def test_received_sticker_enters_the_library(self):
        msg = make_msg(msg_type=47)
        msg.content = msg.clean_text = make_sticker()
        await self.bot.on_message(msg)
        stolen = self.bot.emojis.get('a' * 32)
        self.assertIsNotNone(stolen)
        self.assertTrue(stolen.sendable)          # XML 自带 cdnurl/aeskey
        self.assertEqual(stolen.desc, '摊倒')      # desc 属性就是中文标签
        self.assertEqual(stolen.aeskey, 'key')

    async def test_received_sticker_schedules_scene_enrichment(self):
        """ 微信给的 desc 只有一个标签，场景要另看真图写。 """
        from describer import EmojiDescriber

        class FakeAi:
            async def describe(self, image, max_tokens=64, prompt=''):
                return '累到不行|加班到很晚、只想趴着的时候'

        # 有热度的才补：先把这张标成本群梗
        self.bot.emojis.record(Emoji(md5='a' * 32, count=801))
        self.bot.emoji_describer = EmojiDescriber(FakeAi(), self.bot.emojis, model='m')

        msg = make_msg(msg_type=47)
        msg.content = msg.clean_text = make_sticker()
        msg.get_media_bytes = _fake_image
        await self.bot.on_message(msg)
        await self.bot.emoji_describer.join()

        stored = self.bot.emojis.get('a' * 32)
        self.assertEqual(stored.scene, '加班到很晚、只想趴着的时候')
        self.assertEqual(stored.desc, '摊倒')      # 微信的标签不被模型覆盖

    async def test_no_describer_means_no_scene_work(self):
        msg = make_msg(msg_type=47)
        msg.content = msg.clean_text = make_sticker()
        msg.get_media_bytes = _fake_image
        await self.bot.on_message(msg)          # 不能抛
        self.assertEqual(self.bot.emojis.get('a' * 32).scene, '')

    async def test_history_only_entry_becomes_sendable_once_seen(self):
        """ 历史挖出来的只有 md5 + 热度，收到一次补上元数据才能发。 """
        self.bot.emojis.record(Emoji(md5='a' * 32, count=801))
        self.assertFalse(self.bot.emojis.get('a' * 32).sendable)
        msg = make_msg(msg_type=47)
        msg.content = msg.clean_text = make_sticker()
        await self.bot.on_message(msg)
        merged = self.bot.emojis.get('a' * 32)
        self.assertTrue(merged.sendable)
        self.assertEqual(merged.count, 801)       # 热度没丢

    def _stock(self, desc='摊倒', count=EMOJI_LOCAL_ENOUGH, prefix='a'):
        """ 往本地库里塞几张能发的表情。 """
        for i in range(count):
            self.bot.emojis.record(Emoji(md5=f'{prefix}{i:031x}', desc=desc, cdnurl='http://cdn/x', count=9))

    async def test_enough_local_hits_skip_the_network(self):
        self._stock()
        found = await self.bot.search_emoji('摊倒')
        self.assertEqual(len(found), EMOJI_LOCAL_ENOUGH)
        self.assertEqual(self.bot._wechat.emoji_queries, [])   # 本群够挑就不用联网

    async def test_few_local_hits_still_ask_online(self):
        """ 本群只有一两张时还是要连商店，不然早期只能反复发那两张。 """
        self._stock(count=EMOJI_LOCAL_ENOUGH - 1)
        self.bot._wechat.online = [Emoji(md5='b' * 32, desc='摊倒', cdnurl='http://cdn/y')]
        found = await self.bot.search_emoji('摊倒')
        self.assertEqual(self.bot._wechat.emoji_queries, ['摊倒'])
        self.assertEqual(len(found), EMOJI_LOCAL_ENOUGH)       # 本地的在前，线上的接在后面
        self.assertTrue(found[0].md5.startswith('a'))

    async def test_local_miss_falls_back_online_and_caches(self):
        self.bot._wechat.online = [Emoji(md5=f'b{i:031x}', desc='狗头', cdnurl='http://cdn/y')
                                   for i in range(EMOJI_LOCAL_ENOUGH)]
        found = await self.bot.search_emoji('狗头')
        self.assertEqual(len(found), EMOJI_LOCAL_ENOUGH)
        self.assertEqual(self.bot._wechat.emoji_queries, ['狗头'])
        # 线上结果存进库当缓存，够数了下次就不联网
        await self.bot.search_emoji('狗头')
        self.assertEqual(self.bot._wechat.emoji_queries, ['狗头'])

    async def test_local_results_come_first_and_dedup(self):
        self._stock(count=1)
        self.bot.emojis.record(Emoji(md5='a0' + '0' * 30, desc='狗头', cdnurl='http://cdn/x', count=9))
        self.bot._wechat.online = [Emoji(md5='a0' + '0' * 30, desc='狗头', cdnurl='http://cdn/x'),
                                   Emoji(md5='b' * 32, desc='狗头', cdnurl='http://cdn/y')]
        found = await self.bot.search_emoji('狗头')
        # 本地那张在前，重复的线上结果丢掉
        self.assertEqual([item.md5 for item in found], ['a0' + '0' * 30, 'b' * 32])

    async def test_search_failure_keeps_local_results(self):
        self._stock(count=1)

        async def boom(keyword, limit=10):
            raise RuntimeError('relay 挂了')

        self.bot._wechat.search_emoji = boom
        found = await self.bot.search_emoji('摊倒')
        self.assertEqual(len(found), 1)     # 联网挂了不能把本地的也弄丢

    async def test_emoji_is_sent_through_the_outbox(self):
        emoji = Emoji(md5='a' * 32, desc='摊倒', cdnurl='http://cdn/x')
        self.bot.set_default_plugin(FakePlugin(texts=['累了'], emoji=emoji))
        await self.bot.on_message(make_msg())
        await asyncio.sleep(0.05)
        self.assertEqual(self.bot._wechat.sent, [('456@chatroom', '累了')])
        self.assertEqual(self.bot._wechat.emojis_sent, [('456@chatroom', 'a' * 32)])
        # 表情也要写进历史，否则下一轮模型看不到自己发过
        self.assertEqual([r.text for r in self.bot._history.get_context('456@chatroom')],
                         ['hello', '累了', '[表情：摊倒]'])

    async def test_sticker_only_reply(self):
        emoji = Emoji(md5='a' * 32, desc='摊倒', cdnurl='http://cdn/x')
        self.bot.set_default_plugin(FakePlugin(emoji=emoji))
        await self.bot.on_message(make_msg())
        await asyncio.sleep(0.05)
        self.assertEqual(self.bot._wechat.sent, [])
        self.assertEqual(self.bot._wechat.emojis_sent, [('456@chatroom', 'a' * 32)])

    async def test_library_survives_a_round_trip(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'emojis.json'
            self.bot.emoji_path = str(path)
            msg = make_msg(msg_type=47)
            msg.content = msg.clean_text = make_sticker()
            await self.bot.on_message(msg)
            self.bot.save_emojis()
            self.assertTrue(path.exists())
            again = EmojiLibrary.load(path)
            self.assertTrue(again.get('a' * 32).sendable)

    async def test_no_library_means_no_stealing(self):
        self.bot.emojis = None
        msg = make_msg(msg_type=47)
        msg.content = msg.clean_text = make_sticker()
        await self.bot.on_message(msg)          # 不能抛
        self.assertEqual(await self.bot.search_emoji('摊倒'), [])

    async def test_library_limit_counts_sendable_first(self):
        library = EmojiLibrary()
        for i in range(EMOJI_LOCAL_ENOUGH + 5):
            library.record(Emoji(md5=f'{i:032x}', cdnurl='http://cdn/x'))
        self.assertEqual(library.sendable_count, EMOJI_LOCAL_ENOUGH + 5)


if __name__ == '__main__':
    unittest.main()
