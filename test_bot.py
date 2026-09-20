""" Bot 编排自检：决策入队、stop 语义、静音、手机上自己发的消息只记不回。不连 daemon。 """

import asyncio
import random
import time
import types
import unittest

import events
from bot import EMOJI_LOCAL_ENOUGH, Bot
from context import ChatWindow
from emojis import Emoji, EmojiLibrary
from media import Media, render
from plugin import EndProcessing, Plugin, RepeatPlugin, Reply

# daemon 回的 server_id：真实量级是 19 位，故意用超过 JS 安全整数 2^53 的值
DAEMON_MSG_ID = 4212345678901234567


class FakeWechat:
    def __init__(self, fail=False):
        self.sent: list[tuple[str, str]] = []
        self.attempts: list[tuple[str, str]] = []
        self.fail = fail
        self.all_contacts: dict[str, str] = {}
        self.emojis_sent: list[tuple[str, str]] = []    # (receiver, md5)
        self.downloads: list[int] = []
        self.image: bytes | None = None
        self.emoji_queries: list[str] = []
        self.online: list = []                          # 假线上搜索结果
        self.names = {'wxid_bot': 'Bot'}                # wxid -> 显示名，查不到就回 wxid
        self.revoked: list[tuple[str, int]] = []        # (会话, msg_id)
        self.refs: list = []                            # 每条出站的 ref_svrid（引用回复）
        self.ats: list = []                             # 每条出站的 at_list（@ 人）
        self.revoke_fail = False
        self.voices: list[int] = []                     # 请求过转写的语音 msg_id
        self.transcript = None                          # 非 None = 转写成功，返回这段文本

    async def revoke(self, to, msg_id):
        self.revoked.append((to, msg_id))
        if self.revoke_fail:
            raise RuntimeError('已超过 120(2 分钟)撤回时限（已过 300s）')
        return {'sent': True}

    async def search_emoji(self, keyword, limit=10):
        self.emoji_queries.append(keyword)
        return list(self.online)

    async def send_emoji(self, emoji, receiver):
        self.emojis_sent.append((receiver, emoji.md5))
        return {'sent': True, 'msg_id': DAEMON_MSG_ID}

    @property
    def wxid(self) -> str:
        return 'wxid_bot'

    async def send_text(self, text, receiver, at_list='', ref_svrid=None):
        self.attempts.append((receiver, text))
        if self.fail:
            raise RuntimeError('解析长链 newsendmsg 响应失败: invalid data: unsupported wiretype 7')
        self.sent.append((receiver, text))
        self.refs.append(ref_svrid)
        self.ats.append(at_list)
        return {'sent': True, 'msg_id': DAEMON_MSG_ID}

    async def media_bytes(self, msg):
        """ 附件下载。默认拉不下来——这套 daemon 下的图片就是拉不下来。

        要测成功的路径就往 FakeWechat.image 里塞字节。
        """
        self.downloads.append(msg.msg_id)
        if self.image is None:
            raise RuntimeError('下载附件失败: invalid data: 消息无 cdnurl')
        return self.image

    async def voice_text(self, msg, timeout=20):
        """ 语音转写。默认返回空（= 转写失败），测成功的路径就设 self.transcript。 """
        self.voices.append(msg.msg_id)
        if self.transcript is None:
            raise RuntimeError('语音转写失败: daemon 拒绝：dry_run=true')
        return self.transcript

    async def get_display_name(self, wxid, chatroom=''):
        return self.names.get(wxid, wxid)


class FakePlugin(Plugin):
    def __init__(self, texts=None, stop=False, emoji=None, at=''):
        self.texts = texts or []
        self._stop = stop
        self.emoji = emoji
        self.at = at

    async def handle(self, ctx):
        return Reply(texts=list(self.texts), stop=self._stop, emoji=self.emoji, at=self.at)


def make_msg(text='hello', is_self=False, msg_type=1, create_time=None):
    return types.SimpleNamespace(msg_type=msg_type, content=text, clean_text=text, text=text, is_self=is_self,
                                 msg_id=random.getrandbits(62),          # daemon 的 server_id 是 64 位
                                 reply_target='456@chatroom', sender_name='Alice', sender='wxid_alice',
                                 sender_display_name='Alice',
                                 create_time=int(time.time()) if create_time is None else create_time,
                                 is_chatroom=True, is_at_me=True)


async def _fake_image():
    return b'png'


# 一条入站引用（appmsg type=57）。它的 msg_type 是 49，但正文就是一句话。
QUOTE_XML = ('<msg><appmsg><title>这个我也想去</title><type>57</type>'
             '<refermsg><type>1</type><chatusr>wxid_bob</chatusr>'
             '<content>周末去爬山不</content></refermsg></appmsg>'
             '<fromusername>wxid_alice</fromusername></msg>')


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

    async def test_status_events_trace_a_reply(self):
        """ 一条消息从接收到已发送，事件流要串得起来——面板的气泡状态全靠它。 """
        events.bus.reset()
        self.bot.set_default_plugin(FakePlugin(texts=['hi']))
        msg = make_msg()
        await self.bot.on_message(msg)
        await asyncio.sleep(0.05)

        got = events.bus.since(0)
        self.assertEqual([e.status for e in got][:2],
                         [events.Status.RECEIVED, events.Status.QUEUED])
        sent = got[-1]
        self.assertEqual(sent.status, events.Status.SENT)
        self.assertEqual(sent.reply_to, str(msg.msg_id))     # 挂回被回复的那条
        self.assertEqual(sent.origin, 'bot')
        # daemon 回的 server_id 是 64 位整数，进事件流必须已经是字符串
        self.assertEqual(sent.msg_id, str(DAEMON_MSG_ID))
        # 两个不同的问题：这条消息自己走到哪了 / 回复走到哪了
        self.assertEqual(events.bus.status_of(msg.msg_id).status, events.Status.RECEIVED)
        self.assertEqual(events.bus.progress_of(msg.msg_id).status, events.Status.SENT)

    async def test_send_failure_marks_the_event_failed(self):
        """ 发送报错（但按已投递处理）在面板上要看得见。 """
        events.bus.reset()
        self.bot._wechat = FakeWechat(fail=True)
        self.bot.set_default_plugin(FakePlugin(texts=['hi']))
        await self.bot.on_message(make_msg())
        await asyncio.sleep(0.05)

        last = events.bus.since(0)[-1]
        self.assertEqual(last.status, events.Status.FAILED)
        self.assertIn('newsendmsg', last.detail)

    async def test_dry_run_shows_the_reply_but_touches_nothing(self):
        """ 演习：群里没动静、历史不记，但面板必须看得见这条回复。 """
        events.bus.reset()
        bot = Bot('127.0.0.1', 9999, x_delay=0, send_interval=0, dry_run=True)
        bot._wechat = FakeWechat()
        await bot.outbox.start()
        try:
            bot.set_default_plugin(FakePlugin(texts=['hi']))
            msg = make_msg()
            await bot.on_message(msg)
            await asyncio.sleep(0.05)
        finally:
            await bot.outbox.stop()

        # 自己的回复没进历史（入站的还在）
        self.assertEqual([r.text for r in bot._history.get_context('456@chatroom')], ['hello'])
        dry = [e for e in events.bus.since(0) if e.status == events.Status.DRY_RUN]
        self.assertEqual(len(dry), 1)
        self.assertEqual(dry[0].text, 'hi')          # 正文只能靠事件带出来
        self.assertEqual(dry[0].reply_to, str(msg.msg_id))
        self.assertEqual(dry[0].origin, 'bot')

    async def test_reply_is_sent_and_recorded(self):
        self.bot.set_default_plugin(FakePlugin(texts=['hi']))
        await self.bot.on_message(make_msg())
        await asyncio.sleep(0.05)

        self.assertEqual(self.bot._wechat.sent, [('456@chatroom', 'hi')])
        self.assertEqual([r.text for r in self._window()], ['hello', 'hi'])

    async def test_send_failure_still_records_history(self):
        """ daemon 回执解析失败但消息已投递：不能重试，历史也要照写。 """
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

    async def test_stale_backlog_is_recorded_but_not_answered(self):
        """ daemon 长链重连补推的离线积压：写进历史当上下文，但不触发回复。 """
        self.bot.set_default_plugin(FakePlugin(texts=['hi']))
        await self.bot.on_message(make_msg(create_time=int(time.time()) - 3600))
        await asyncio.sleep(0.05)

        self.assertEqual(self.bot._wechat.sent, [])
        self.assertEqual([r.text for r in self._window()], ['hello'])

    async def test_self_message_is_recorded_but_not_replied(self):
        """ 手机上自己发的：进上下文让模型看得见，但不触发插件、不回复。 """
        self.bot.set_default_plugin(FakePlugin(texts=['hi']))
        await self.bot.on_message(make_msg(text='我来说一句', is_self=True))
        await asyncio.sleep(0.05)

        self.assertEqual(self.bot._wechat.sent, [])            # 不回复
        window = self._window()
        self.assertEqual([r.text for r in window], ['我来说一句'])
        self.assertTrue(window[0].is_self)                     # 记成「自己说的」
        assert '我: 我来说一句' in str(self.bot._history.get_context('456@chatroom'))

    async def test_self_emoji_is_stolen(self):
        """ 手机上发的表情也是群里用过的，一样要偷（所以偷表情在 is_self 判断之前）。 """
        self.bot.emojis = EmojiLibrary()
        xml = '<msg><emoji md5="m1" desc="ChEKB2RlZmF1bHQSBuWVp+WVpw==" cdnurl="u" /></msg>'
        await self.bot.on_message(make_msg(text=xml, msg_type=47, is_self=True))
        self.assertEqual([e.md5 for e in self.bot.emojis.search('啧啧')], ['m1'])

    async def test_voice_is_transcribed_before_deciding(self):
        """ 语音先转成文字再进历史，模型才接得上；否则只看到 `[语音 3″]`。 """
        self.bot._wechat.transcript = '我的麦好像有点炸'
        self.bot.set_default_plugin(FakePlugin(texts=['收到']))
        msg = make_msg(text='<msg><voicemsg voicelength="3000" /></msg>', msg_type=34)
        await self.bot.on_message(msg)
        await asyncio.sleep(0.05)

        self.assertEqual(self.bot._wechat.voices, [msg.msg_id])
        self.assertEqual(self._window()[0].text, '[语音 3″：我的麦好像有点炸]')
        self.assertEqual([text for _, text in self.bot._wechat.sent], ['收到'])

    async def test_voice_transcription_failure_keeps_the_placeholder(self):
        """ 转写失败不能把这一轮带崩，也不该丢掉「这里有条语音」。 """
        self.bot._wechat.transcript = None                     # voice_text 会抛
        self.bot.set_default_plugin(FakePlugin(texts=['收到']))
        await self.bot.on_message(make_msg(text='<msg><voicemsg voicelength="3000" /></msg>', msg_type=34))
        await asyncio.sleep(0.05)

        self.assertEqual(self._window()[0].text, '[语音 3″]')
        self.assertEqual([text for _, text in self.bot._wechat.sent], ['收到'])

    async def test_attached_plugin_overrides_defaults(self):
        self.bot.set_default_plugin(FakePlugin(texts=['hi']))
        self.bot.attach('456@chatroom', EndProcessing())
        await self.bot.on_message(make_msg())
        await asyncio.sleep(0.05)

        self.assertEqual(self.bot._wechat.sent, [])

    async def test_mute_and_unmute(self):
        """ 管理后台的静音按钮。键是**显示名**，因为插件链就是按显示名分发的。 """
        self.bot.set_default_plugin(FakePlugin(texts=['hi']))
        # 这个群在 daemon 里叫「测试群」；插件链拿到的 source 就是显示名
        self.bot._wechat.names['456@chatroom'] = '测试群'
        self.assertFalse(self.bot.is_muted('测试群'))

        self.bot.mute('测试群')          # 注意：不是 '456@chatroom'
        self.assertTrue(self.bot.is_muted('测试群'))
        await self.bot.on_message(make_msg())
        await asyncio.sleep(0.05)
        self.assertEqual(self.bot._wechat.sent, [])          # 静音了就不回

        self.bot.unmute('测试群')
        self.assertFalse(self.bot.is_muted('测试群'))
        await self.bot.on_message(make_msg())
        await asyncio.sleep(0.05)
        self.assertEqual(self.bot._wechat.sent, [('456@chatroom', 'hi')])

    async def test_set_default_plugin_replaces_not_appends(self):
        """ 热重载靠它整体换掉插件链，不能是追加（否则每重载一次多跑一遍旧链）。 """
        self.bot.set_default_plugin(FakePlugin(texts=['first']))
        self.bot.set_default_plugin(FakePlugin(texts=['second']))
        await self.bot.on_message(make_msg())
        await asyncio.sleep(0.05)

        self.assertEqual(self.bot._wechat.sent, [('456@chatroom', 'second')])

    async def test_history_sizes_and_counters(self):
        """ 健康卡要看的两个数。 """
        self.bot.set_default_plugin(FakePlugin(texts=['hi']))
        await self.bot.on_message(make_msg())
        await asyncio.sleep(0.05)

        self.assertEqual(self.bot.history.sizes(), {'456@chatroom': 2})   # 入站 + 自己的回复
        self.assertEqual(self.bot.reply_count, 1)
        self.assertEqual(self.bot.outbox.depth(), 0)

    async def test_multi_line_reply_is_enqueued_all_at_once(self):
        """ 一轮的多行是**同步全部入队**的：先都进历史，再按序发出。

        发送由队列按限速逐条做，是另一条 task，不能阻塞插件链（否则后一条要等
        前一条的打字延迟跑完，群里就像卡住了）。
        """
        self.bot.outbox._typing_delay = lambda text: 0.2
        self.bot.set_default_plugin(FakePlugin(texts=['一', '二', '三']))
        await self.bot.on_message(make_msg())
        await asyncio.sleep(0.05)          # 插件跑完、入队完成，但还没轮到发

        self.assertEqual(self.bot._wechat.sent, [])          # 一条都还没真发
        self.assertEqual([r.text for r in self._window()], ['hello', '一', '二', '三'])
        await asyncio.sleep(1.2)
        self.assertEqual([text for _, text in self.bot._wechat.sent], ['一', '二', '三'])

    async def test_quote_replies_carry_the_trigger_svr_id(self):
        """ 群聊且消息多（乱）时，文字回复引「这一轮在回的那条」：ref_svrid = 它的 svrId。 """
        self.bot.quote_replies = True
        self._seed_busy_group()
        self.bot.set_default_plugin(FakePlugin(texts=['收到']))
        msg = make_msg()
        await self.bot.on_message(msg)
        await asyncio.sleep(0.1)
        self.assertEqual(self.bot._wechat.refs, [msg.msg_id])

    async def test_quote_replies_skipped_in_quiet_group(self):
        """ 群里没几个人说话时不引用——每条都带引用反而怪。 """
        self.bot.quote_replies = True
        self.bot.set_default_plugin(FakePlugin(texts=['收到']))
        await self.bot.on_message(make_msg())        # 只有这一条入站消息
        await asyncio.sleep(0.1)
        self.assertEqual(self.bot._wechat.refs, [None])

    async def test_quote_replies_skipped_in_private(self):
        """ 私聊从不引用。 """
        self.bot.quote_replies = True
        self.bot.set_default_plugin(FakePlugin(texts=['收到']))
        msg = make_msg()
        msg.is_chatroom = False
        await self.bot.on_message(msg)
        await asyncio.sleep(0.1)
        self.assertEqual(self.bot._wechat.refs, [None])

    def _seed_busy_group(self, count: int = 8) -> None:
        """ 往历史里灌几条别人的消息，把群填「乱」。 """
        window = self.bot._history.get_context('456@chatroom')
        for i in range(count):
            window.append(f'用户{i}', '别人说的', int(time.time()))

    async def test_quote_replies_off_by_default(self):
        self.bot.set_default_plugin(FakePlugin(texts=['收到']))
        await self.bot.on_message(make_msg())
        await asyncio.sleep(0.1)
        self.assertIn(None, self.bot._wechat.refs)      # 没开引用就不带 ref_svrid

    async def test_at_reaches_the_transport(self):
        """ 插件层只负责把 wxid 带回来，怎么 @ 是出站那一段的事。 """
        self.bot.set_default_plugin(FakePlugin(texts=['你问他去'], at='wxid_zhang'))
        await self.bot.on_message(make_msg())
        await asyncio.sleep(0.1)
        self.assertEqual(self.bot._wechat.sent, [('456@chatroom', '你问他去')])
        self.assertEqual(self.bot._wechat.ats, ['wxid_zhang'])

    async def test_no_at_means_no_at_list(self):
        self.bot.set_default_plugin(FakePlugin(texts=['收到']))
        await self.bot.on_message(make_msg())
        await asyncio.sleep(0.1)
        self.assertEqual(self.bot._wechat.ats, [None])

    async def test_reply_is_in_history_before_it_is_sent(self):
        """回复一决策就写历史，不等打字延迟过完。

        否则对方在这几秒里又发一条，下一轮决策看不到自己即将发出的那句，会再答一遍
        （日志里的「在？/有事」被回了两次）。
        """
        self.bot.outbox._typing_delay = lambda text: 0.2
        self.bot.set_default_plugin(FakePlugin(texts=['在的 咋啦']))
        await self.bot.on_message(make_msg(text='在？'))
        await asyncio.sleep(0.05)          # 回复在后台任务里跑，等它入队

        self.assertEqual(self.bot._wechat.sent, [])        # 还没真发出去
        self.assertEqual([r.text for r in self._window()], ['在？', '在的 咋啦'])
        await asyncio.sleep(0.3)
        self.assertEqual([text for _, text in self.bot._wechat.sent], ['在的 咋啦'])

    async def test_next_turn_sees_the_pending_reply(self):
        """对方连发时，后一条的上下文里要看得到前一条还没发出的回复。 """
        seen: list[str] = []

        class Inspector(Plugin):
            async def handle(self, ctx):
                seen.extend(r.text for r in ctx.history)
                return Reply(texts=['在 咋了'], stop=True)

        self.bot.outbox._typing_delay = lambda text: 0.2
        self.bot.set_default_plugin(FakePlugin(texts=['在的 咋啦']))
        await self.bot.on_message(make_msg(text='在？'))
        await asyncio.sleep(0.05)          # 让第一轮跑完、把回复写进历史
        self.bot.set_default_plugin(Inspector())
        await self.bot.on_message(make_msg(text='有事'))
        await asyncio.sleep(0.6)

        self.assertIn('在的 咋啦', seen)                    # 第二条决策看得到第一条回复
        self.assertEqual(self.bot._wechat.sent,
                         [('456@chatroom', '在的 咋啦'), ('456@chatroom', '在 咋了')])

    async def test_messages_during_a_turn_form_one_next_block(self):
        """一轮还没跑完时又来的消息合并成**下一轮**，不是一条一轮地追。 """
        calls: list[str] = []
        release = asyncio.Event()

        class Slow(Plugin):
            async def handle(self, ctx):
                calls.append(ctx.msg.content)
                if len(calls) == 1:
                    await release.wait()        # 卡住第一轮，让后面的消息攒起来
                return Reply(texts=[f'回 {ctx.msg.content}'], stop=True)

        def quiet(text):
            msg = make_msg(text=text)
            msg.is_at_me = False
            return msg

        self.bot.set_default_plugin(Slow())
        await self.bot.on_message(quiet('第一条'))
        await asyncio.sleep(0)                 # 让第一轮起跑并卡住
        await self.bot.on_message(quiet('第二条'))
        await self.bot.on_message(quiet('第三条'))
        release.set()
        await asyncio.sleep(0.1)

        # 第一轮只看到第一条；二、三条合并成一轮，以最新那条为「本轮」（历史里三条都在）
        self.assertEqual(calls, ['第一条', '第三条'])
        self.assertEqual(self.bot._wechat.sent, [('456@chatroom', '回 第一条'),
                                                 ('456@chatroom', '回 第三条')])
        self.assertEqual(self.bot._turns, {})   # 跑完的会话任务自己清掉

    async def test_at_in_a_later_block_still_wins(self):
        """忙的时候被 @、紧跟着一句闲话：下一轮要拿 @ 的那条当本轮，不能被闲话顶掉。 """
        calls: list[str] = []
        release = asyncio.Event()

        class Slow(Plugin):
            async def handle(self, ctx):
                calls.append(ctx.msg.content)
                if len(calls) == 1:
                    await release.wait()
                return Reply(texts=['ok'], stop=True)

        def msg(text, at):
            item = make_msg(text=text)
            item.is_at_me = at
            return item

        self.bot.set_default_plugin(Slow())
        await self.bot.on_message(msg('开场', at=False))
        await asyncio.sleep(0)
        await self.bot.on_message(msg('哈哈', at=False))
        await self.bot.on_message(msg('@机器人 在吗', at=True))
        release.set()
        await asyncio.sleep(0.1)

        self.assertEqual(calls, ['开场', '@机器人 在吗'])

    async def test_repeat_happens_once_even_before_the_first_reply_is_sent(self):
        """复读一次后，即便那条回复还没真发出去，也不能再复读一次。

        线上 bug：第二条队形触发复读、回复卡在出站队列里（daemon RPC 还没回），第三条队形
        紧跟着进来；当时历史是发完才写的，复读机看不到自己刚复读过，就再跟一遍。
        """
        self.bot.outbox._typing_delay = lambda text: 0.3
        self.bot.set_default_plugin(RepeatPlugin())

        def line(text, who):
            msg = make_msg(text=text)
            msg.sender_name = who
            return msg

        await self.bot.on_message(line('牛啊', 'Alice'))
        await self.bot.on_message(line('牛啊', 'Bob'))
        await asyncio.sleep(0.05)          # 复读已入队，但还没发出去
        self.assertEqual(self.bot._wechat.sent, [])
        await self.bot.on_message(line('牛啊', 'Carol'))
        await asyncio.sleep(0.8)

        self.assertEqual([text for _, text in self.bot._wechat.sent], ['牛啊'])

    async def test_repeat_is_verbatim(self):
        """复读必须一字不改——抖了错别字就不叫复读了。 """
        self.bot.typo_rate = 1.0
        self.bot._rng = random.Random(0)
        self.bot.set_default_plugin(RepeatPlugin())

        def line(text, who):
            msg = make_msg(text=text)
            msg.sender_name = who
            return msg

        await self.bot.on_message(line('牛啊牛啊', 'Alice'))
        await self.bot.on_message(line('牛啊牛啊', 'Bob'))
        await asyncio.sleep(0.05)

        self.assertEqual([text for _, text in self.bot._wechat.sent], ['牛啊牛啊'])

    async def test_typos_are_applied_on_send_and_in_history(self):
        """ 错别字在出站那一刻抖，历史里记的也得是抖过的版本。 """
        self.bot.typo_rate = 1.0
        self.bot._rng = random.Random(0)
        self.bot.set_default_plugin(FakePlugin(texts=['这个我觉得不行，得再改改']))
        await self.bot.on_message(make_msg())
        await asyncio.sleep(0.05)

        sent = self.bot._wechat.sent[0][1]
        self.assertNotEqual(sent, '这个我觉得不行，得再改改')
        self.assertEqual(len(sent), len('这个我觉得不行，得再改改'))   # 只换字，不增删
        self.assertEqual(self._window()[-1].text, sent)

    async def test_typos_off_by_default(self):
        self.bot.set_default_plugin(FakePlugin(texts=['这个我觉得不行']))
        await self.bot.on_message(make_msg())
        await asyncio.sleep(0.05)
        self.assertEqual(self.bot._wechat.sent[0][1], '这个我觉得不行')

    async def test_media_placeholder_is_never_sent_as_text(self):
        """ [表情：欢迎] 只该出现在历史里；插件/模型把它当话说就拦下。 """
        self.bot.set_default_plugin(FakePlugin(texts=['[表情：欢迎]']))
        await self.bot.on_message(make_msg())
        await asyncio.sleep(0.05)
        self.assertEqual(self.bot._wechat.sent, [])
        self.assertEqual(len(self._window()), 1)     # 只有别人那条，自己什么也没说

    async def test_placeholder_is_dropped_but_the_emoji_still_goes(self):
        """ 搜不到表情时宁可不说话；真选中了表情就正常发。 """
        self.bot.set_default_plugin(FakePlugin(texts=['[表情：欢迎]'],
                                               emoji=Emoji(md5='a' * 32, desc='欢迎', cdnurl='http://cdn/x')))
        await self.bot.on_message(make_msg())
        await asyncio.sleep(0.05)
        self.assertEqual(self.bot._wechat.sent, [])
        self.assertEqual(self.bot._wechat.emojis_sent, [('456@chatroom', 'a' * 32)])


    async def test_quoted_message_is_answered(self):
        """ 引用消息的正文就是一句话，不能因为 msg_type=49 就被当成「不支持的类型」。 """
        self.bot.set_default_plugin(FakePlugin(texts=['周末去啊']))
        await self.bot.on_message(make_msg(text=QUOTE_XML, msg_type=49))
        await asyncio.sleep(0.1)
        self.assertEqual([text for _, text in self.bot._wechat.sent], ['周末去啊'])
        # 历史里存的是渲染后的人话（带被引内容），不是原始 XML
        self.assertIn('这个我也想去（引用前文的消息：周末去爬山不）', [r.text for r in self._window()])

    async def test_quoting_a_link_is_answered_too(self):
        """ 分享链接 + @ 机器人：@ 必须能到模型手里，不能被类型门闷掉。 """
        self.bot.set_default_plugin(FakePlugin(texts=['看了，挺有意思']))
        link = '<msg><appmsg><title>一篇文章</title><type>5</type><url>http://x</url></appmsg></msg>'
        await self.bot.on_message(make_msg(text=link, msg_type=49))
        await asyncio.sleep(0.1)
        self.assertEqual([text for _, text in self.bot._wechat.sent], ['看了，挺有意思'])

    async def test_silent_chain_is_reported_as_muted(self):
        """ 插件链什么都没说、也没自己发状态时，面板要有个交代。

        现在只剩「静音」这一种：其余路径（门控/决策/表达）都会自己发一条状态。
        """
        self.bot.mute('456@chatroom')
        msg = make_msg()
        await self.bot.on_message(msg)
        await asyncio.sleep(0.1)
        self.assertEqual(self.bot._wechat.sent, [])
        self.assertEqual(events.bus.status_of(msg.msg_id).status, events.Status.MUTED)


class TestRevoke(unittest.IsolatedAsyncioTestCase):
    """ 撤回：模型只说「撤哪条」，daemon 要的 msg_id 由 Bot 记账解析。 """

    async def asyncSetUp(self):
        self.bot = Bot('127.0.0.1', 9999, x_delay=0, send_interval=0)
        self.bot._wechat = FakeWechat()
        await self.bot.outbox.start()

    async def asyncTearDown(self):
        await self.bot.outbox.stop()

    async def send(self, texts):
        self.bot.set_default_plugin(FakePlugin(texts=list(texts)))
        await self.bot.on_message(make_msg())
        await asyncio.sleep(0.1)          # 让出站队列真的发出去、记账完成

    def window(self):
        return self.bot._history.get_context('456@chatroom')

    async def test_revoke_last_drops_it_from_history(self):
        await self.send(['在的 咋啦'])
        self.assertIn('在的 咋啦', [r.text for r in self.window()])

        result = await self.bot.revoke('456@chatroom', index=1)
        self.assertEqual(self.bot._wechat.revoked, [('456@chatroom', DAEMON_MSG_ID)])
        self.assertIn('已撤回', result)
        # 撤了就得从上下文里抹掉，否则下一轮模型以为自己说过
        self.assertNotIn('在的 咋啦', [r.text for r in self.window()])

    async def test_revoke_by_text_when_unambiguous(self):
        await self.send(['在的 咋啦'])
        result = await self.bot.revoke('456@chatroom', text='咋啦')
        self.assertIn('已撤回', result)
        self.assertEqual(len(self.bot._wechat.revoked), 1)

    async def test_ambiguous_text_lists_candidates_and_revokes_nothing(self):
        await self.send(['在的', '在的呀'])
        result = await self.bot.revoke('456@chatroom', text='在的')
        self.assertNotIn('已撤回', result)
        self.assertIn('有好几条', result)
        self.assertIn('1. 在的呀', result)          # 1 = 最近一条
        self.assertEqual(self.bot._wechat.revoked, [])

    async def test_listing_is_newest_first(self):
        await self.send(['第一条', '第二条'])
        result = await self.bot.revoke('456@chatroom')
        self.assertIn('1. 第二条', result)
        self.assertIn('2. 第一条', result)

    async def test_out_of_window_is_refused_without_a_call(self):
        await self.send(['在的'])
        self.bot._sent['456@chatroom'][0].ts -= 200      # 人为推过 2 分钟窗口
        result = await self.bot.revoke('456@chatroom', index=1)
        self.assertIn('没有可撤回', result)
        self.assertEqual(self.bot._wechat.revoked, [])

    async def test_daemon_refusal_is_reported_verbatim(self):
        await self.send(['在的'])
        self.bot._wechat.revoke_fail = True
        result = await self.bot.revoke('456@chatroom', index=1)
        self.assertIn('撤回失败', result)
        self.assertIn('时限', result)
        self.assertIn('在的', [r.text for r in self.window()])   # 没删掉


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


class TestPayloadRender(unittest.TestCase):
    """ render() 把非文本正文换成可读占位；媒体消息额外带回 Media。 """

    def test_image_becomes_marker(self):
        text, media = render(3, '<?xml version="1.0"?><msg><img md5="abc" /></msg>', '')
        self.assertEqual(text, '[图片]')
        self.assertEqual(media, Media('image', 'abc'))

    def test_quote_of_an_image_is_not_a_placeholder(self):
        """ 回归：被引的是图片时，以前整条退化成 `[引用消息]`（日志里 67 条坏 40 条）。

        refermsg 的正文是那张图的 XML（没有 title），旧代码 `find('.//title').text`
        直接 AttributeError，异常被吞掉。
        """
        quote = ('<msg><appmsg><title>这也太好看了</title><type>57</type>'
                 '<refermsg><type>3</type><chatusr>wxid_bob</chatusr>'
                 '<content>&lt;?xml version="1.0"?&gt;&lt;msg&gt;&lt;img aeskey="0bda33" '
                 'md5="2f8f1e" /&gt;&lt;/msg&gt;</content></refermsg></appmsg>'
                 '<fromusername>wxid_alice</fromusername></msg>')
        text, _ = render(49, quote, '')
        self.assertEqual(text, '这也太好看了（引用前文的消息：[图片]）')

    def test_quote_of_a_sticker_keeps_its_label(self):
        quote = ('<msg><appmsg><title>哈哈</title><type>57</type>'
                 '<refermsg><type>47</type><chatusr>wxid_bob</chatusr>'
                 '<content>&lt;msg&gt;&lt;emoji md5="m" '
                 'desc="ChEKB2RlZmF1bHQSBuaRiuWAkg==" /&gt;&lt;/msg&gt;</content>'
                 '</refermsg></appmsg><fromusername>wxid_alice</fromusername></msg>')
        text, _ = render(49, quote, '')
        self.assertEqual(text, '哈哈（引用前文的消息：[表情：摊倒]）')

    def test_emoji_keeps_description(self):
        text, _ = render(47, '<msg><emoji desc="ChEKB2RlZmF1bHQSBuWVp+WVpw==" /></msg>', '')
        self.assertEqual(text, '[表情：啧啧]')

    def test_emoji_without_description_falls_back(self):
        text, _ = render(47, '<msg><emoji desc="" /></msg>', '')
        self.assertEqual(text, '[表情]')

    def test_link_does_not_leak_xml(self):
        text, _ = render(49, '<msg><appmsg><title>一篇文章</title><type>5</type>'
                             '<appname>某公众号</appname></appmsg></msg>', '')
        self.assertEqual(text, '[某公众号] 一篇文章')

    def test_location_and_video(self):
        self.assertEqual(render(48, '<msg><location label="海淀区京新高速" /></msg>', '')[0],
                         '[位置：海淀区京新高速]')
        self.assertEqual(render(43, '<msg><videomsg /></msg>', '')[0], '[视频]')


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

        # 收到就算本群用过一次（热度 1），够格补场景了
        self.bot.emoji_describer = EmojiDescriber(FakeAi(), self.bot.emojis, model='m')

        msg = make_msg(msg_type=47)
        msg.content = msg.clean_text = make_sticker()
        # 附件现在走 Wechat.media_bytes（普通 RPC），不再用 SDK 的 msg.get_media_bytes
        self.bot._wechat.image = await _fake_image()
        await self.bot.on_message(msg)
        await self.bot.emoji_describer.join()

        stored = self.bot.emojis.get('a' * 32)
        self.assertEqual(stored.scene, '加班到很晚、只想趴着的时候')
        self.assertEqual(stored.desc, '摊倒')      # 微信的标签不被模型覆盖

    async def test_no_describer_means_no_scene_work(self):
        msg = make_msg(msg_type=47)
        msg.content = msg.clean_text = make_sticker()
        await self.bot.on_message(msg)          # 不能抛
        self.assertEqual(self.bot.emojis.get('a' * 32).scene, '')

    async def test_heat_grows_at_runtime(self):
        """ 热度不再靠离线重跑：群里每收到一次就加一。 """
        for expected in (1, 2, 3):
            msg = make_msg(msg_type=47)
            msg.content = msg.clean_text = make_sticker()
            await self.bot.on_message(msg)
            self.assertEqual(self.bot.emojis.get('a' * 32).count, expected)
        self.assertTrue(self.bot.emojis.get('a' * 32).sendable)   # XML 自带下载信息

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
            raise RuntimeError('daemon 挂了')

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
