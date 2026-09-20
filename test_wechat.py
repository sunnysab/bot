""" Wechat 适配层自检：不连 daemon，用 stub 替换 SDK 客户端。 """

import asyncio
import types
import unittest

from weixin_bot import Message

from wechat import Wechat

QUOTE_XML = ('<msg><appmsg><title>回复内容</title><type>57</type>'
             '<fromusername>wxid_me</fromusername><refermsg><type>49</type>'
             '<svrid>1</svrid><fromusr>456@chatroom</fromusr><chatusr>wxid_alice</chatusr>'
             '<displayname>Alice</displayname><content>&lt;msg&gt;&lt;appmsg&gt;&lt;title&gt;被引用的内容'
             '&lt;/title&gt;&lt;/appmsg&gt;&lt;/msg&gt;</content></refermsg></appmsg></msg>')


def make_message(**kwargs) -> Message:
    fields = dict(msg_id=1, from_user='456@chatroom', to_user='wxid_me', msg_type=1,
                  content='hello', clean_text='hello', create_time=100, is_chatroom=True,
                  chatroom_sender='wxid_alice', sender_display_name='Alice')
    fields.update(kwargs)
    return Message(bot=None, **fields)  # type: ignore[arg-type]


class FakeBot:
    def __init__(self):
        self.sent: list[dict] = []
        self.contact_calls: list[str] = []
        self.wxid = 'wxid_me'
        # 实测 daemon 查不到群成员时是拿 wxid 当名字返回的；置空模拟这种情况
        self.group_card = 'AliceGroupCard'
        self.contacts = {'wxid_alice': 'Alice', '456@chatroom': '一起哈啤'}

    async def get_all_contacts(self, **kwargs):
        return [types.SimpleNamespace(username='wxid_alice', name='Alice')]

    async def get_conversations(self, limit):
        self.requested_limit = limit
        return [types.SimpleNamespace(peer='456@chatroom'), types.SimpleNamespace(peer='gh_news'),
                types.SimpleNamespace(peer='wxid_bob'), types.SimpleNamespace(peer='wxid_me')]

    async def get_messages(self, limit, contact, reverse, exclude_system,
                           before_id=None, after_id=None, query=None):
        self.page = {'limit': limit, 'before_id': before_id, 'after_id': after_id}
        self.query = query
        return [
            make_message(msg_type=1, content='第一条', clean_text='第一条', create_time=200),
            make_message(msg_type=3, content='<img cdnurl="x"/>', clean_text='', create_time=201),
            make_message(msg_type=49, content=QUOTE_XML, clean_text=QUOTE_XML, create_time=202),
        ]

    async def get_chatroom_member(self, chatroom, wxid):
        return types.SimpleNamespace(name=self.group_card or wxid)

    async def get_chatroom(self, chatroom):
        members = getattr(self, 'members', [('wxid_alice', 'Alice', 'AliceGroupCard')])
        return types.SimpleNamespace(members=[
            types.SimpleNamespace(wxid=wxid, display_name=display, group_nickname=card, nickname='',
                                  name=display or card or wxid)
            for wxid, display, card in members])

    async def get_contact(self, user):
        self.contact_calls.append(user)
        name = self.contacts.get(user)
        return [types.SimpleNamespace(username=user, name=name)] if name else []

    async def send_text(self, to, text, at_users, at_names, ref_svrid=None):
        self.sent.append({'to': to, 'text': text, 'at_users': at_users, 'at_names': at_names,
                          'ref_svrid': ref_svrid})
        return {'sent': True}


class TestWechatFacade(unittest.IsolatedAsyncioTestCase):
    async def _call(self, action, params):
        self.calls.append((action, params))
        return self.reply
    def setUp(self):
        self.wechat = Wechat('127.0.0.1', 9999)
        self.wechat._bot = FakeBot()  # type: ignore[assignment]
        self.dispatched: list[Message] = []
        self.wechat._message_callback = self._collect

    async def _collect(self, msg):
        self.dispatched.append(msg)

    async def test_dispatch_forwards_appmsg(self):
        await self.wechat._dispatch(make_message(msg_type=49, content=QUOTE_XML, clean_text=QUOTE_XML))
        self.assertEqual(len(self.dispatched), 1)

    async def test_dispatch_ignores_unknown_type(self):
        await self.wechat._dispatch(make_message(msg_type=10000))
        self.assertEqual(self.dispatched, [])

    async def test_fetch_history_maps_and_filters(self):
        window = await self.wechat.fetch_history('456@chatroom', count=10)
        # 文本、图片标记、引用都在
        self.assertEqual(len(window), 3)
        self.assertEqual(window[0].sender, 'Alice')
        self.assertEqual(window[0].text, '第一条')
        self.assertEqual(window[1].text, '[图片]')
        # 被引的那条渲染成人话：这里是条链接（内层 appmsg 没有 type）
        self.assertEqual(window[2].text, '回复内容（引用前文的消息：[链接] 被引用的内容）')
        self.assertEqual([r.t for r in window], [200, 201, 202])

    async def test_get_recent_sessions_filters_non_contacts(self):
        sessions = await self.wechat.get_recent_sessions(3)
        # wxid_me = 自己（daemon 的 device-sync 会按 peer 分出一个和自己聊的假会话）
        self.assertEqual(sessions, ['456@chatroom', 'wxid_bob'])
        # 公众号会占满会话列表，必须多取一批再过滤
        self.assertGreater(self.wechat._bot.requested_limit, 3)

    async def test_send_text_with_at(self):
        await self.wechat.send_text('你好', '456@chatroom', at_list='wxid_alice')
        sent = self.wechat._bot.sent[0]
        self.assertEqual(sent['at_users'], ['wxid_alice'])
        self.assertEqual(sent['at_names'], ['AliceGroupCard'])
        self.assertEqual(sent['text'], ' @AliceGroupCard\n\n你好')

    async def test_send_text_at_all(self):
        await self.wechat.send_text('开会了', '456@chatroom', at_list='notify@all')
        sent = self.wechat._bot.sent[0]
        self.assertEqual(sent['at_users'], ['notify@all'])
        self.assertEqual(sent['text'], ' @所有人\n\n开会了')

    async def test_send_text_can_quote(self):
        """ 引用目标透传到 SDK；不引用时不带这个字段。 """
        await self.wechat.send_text('收到', '456@chatroom', ref_svrid=7100000000000000123)
        self.assertEqual(self.wechat._bot.sent[0]['ref_svrid'], 7100000000000000123)
        await self.wechat.send_text('普通的', '456@chatroom')
        self.assertIsNone(self.wechat._bot.sent[1]['ref_svrid'])

    async def test_dry_run_does_not_send(self):
        self.wechat.dry_run = True
        await self.wechat.send_text('你好', '456@chatroom')
        self.assertEqual(self.wechat._bot.sent, [])

    async def test_member_name_falls_back_when_daemon_returns_wxid(self):
        """ daemon 查不到群成员就返 wxid；那不是名字，得退到联系人表。 """
        self.wechat._bot.group_card = ''
        name = await self.wechat.get_display_name('wxid_alice', '456@chatroom')
        self.assertEqual(name, 'Alice')

    async def test_group_name_comes_from_get_contact(self):
        """ get_all_contacts 不含群，群名只能单独查。 """
        self.assertEqual(await self.wechat.get_display_name('456@chatroom'), '一起哈啤')

    async def test_resolved_names_are_cached(self):
        for _ in range(3):
            await self.wechat.get_display_name('456@chatroom')
            await self.wechat.get_display_name('wxid_alice', '456@chatroom')
        self.assertEqual(self.wechat._bot.contact_calls, ['456@chatroom'])   # 群里那个人走群名片
        self.assertEqual(await self.wechat.get_display_name('456@chatroom'), '一起哈啤')

    async def test_media_bytes_goes_through_plain_rpc(self):
        """ 附件走普通 RPC `get_media_bytes`，不走坏的 DL_OPEN 分块流。

        分块流那条路：daemon 的 DL_OPEN 分支按 DaemonRequest（action + params）解包，
        而 SDK 只塞了 params，解不出就被 unwrap_or_default() 吞掉，最后报 400
        「缺少 msg_id」——那个报错和真实原因（图片没有 cdnurl）完全无关。
        """
        import base64

        self.wechat._bot.call = self._call
        self.calls: list[tuple[str, dict]] = []
        self.reply = {'filename': 'msg_1.jpg', 'size': 4, 'ext': 'jpg',
                      'data_base64': base64.b64encode(b'\xff\xd8\xff\xe0').decode()}
        raw = await self.wechat.media_bytes(make_message(msg_id=777))
        self.assertEqual(raw, b'\xff\xd8\xff\xe0')
        self.assertEqual(self.calls, [('get_media_bytes', {'msg_id': 777})])

        # daemon 说没内容时要报得诚实，而不是拿一个空字节流充数
        self.reply = {'filename': 'x', 'size': 0}
        with self.assertRaises(ValueError):
            await self.wechat.media_bytes(make_message(msg_id=778))

    async def test_media_bytes_by_id_returns_ext(self):
        """ 面板要的是 (字节, 扩展名)，附件仍由 daemon 保管。 """
        import base64

        self.wechat._bot.call = self._call
        self.calls: list[tuple[str, dict]] = []
        self.reply = {'ext': 'gif', 'data_base64': base64.b64encode(b'GIF89a').decode()}
        data, ext = await self.wechat.media_bytes_by_id(999)
        self.assertEqual((data, ext), (b'GIF89a', 'gif'))

    async def test_same_msg_id_is_fetched_once(self):
        """ 同一张图只问 daemon 一次：当前轮内联给模型、后台补描述，是两条路各要一次。 """
        import base64

        self.wechat._bot.call = self._call
        self.calls: list[tuple[str, dict]] = []
        self.reply = {'ext': 'jpg', 'data_base64': base64.b64encode(b'jpeg').decode()}
        first = await self.wechat.media_bytes_by_id(7)
        second = await self.wechat.media_bytes_by_id(7)
        self.assertEqual(first, second)
        self.assertEqual(len(self.calls), 1)
        # 不同消息各拉各的
        await self.wechat.media_bytes_by_id(8)
        self.assertEqual(len(self.calls), 2)

    async def test_concurrent_requests_for_one_image_are_merged(self):
        """ 两边几乎同时要：单飞，合流成一次 RPC（daemon 的 CDN 回退不持锁）。 """
        import base64

        self.wechat._bot.call = self._call
        self.calls: list[tuple[str, dict]] = []
        self.reply = {'ext': 'jpg', 'data_base64': base64.b64encode(b'jpeg').decode()}
        results = await asyncio.gather(*(self.wechat.media_bytes_by_id(7) for _ in range(5)))
        self.assertTrue(all(item == results[0] for item in results))
        self.assertEqual(len(self.calls), 1)

    async def test_a_failed_fetch_is_not_cached(self):
        """ 失败不进内存缓存：网络抖动、daemon 刚重启都会失败，下次要还能再试。 """
        self.wechat._bot.call = self._call
        self.calls: list[tuple[str, dict]] = []
        self.reply = {'filename': 'x', 'size': 0}      # 没有 data_base64
        for _ in range(2):
            with self.assertRaises(ValueError):
                await self.wechat.media_bytes_by_id(7)
        self.assertEqual(len(self.calls), 2)

    async def test_message_pagination_cursors_are_passed_through(self):
        """ 面板翻页要用 daemon 的 messages.id 游标，不能拿 64 位 msg_id 比大小。 """
        await self.wechat.messages('456@chatroom', limit=50, before_id=99)
        self.assertEqual(self.wechat._bot.page,
                         {'limit': 50, 'before_id': 99, 'after_id': None})
        await self.wechat.messages('456@chatroom', after_id=7)
        self.assertEqual(self.wechat._bot.page['after_id'], 7)

    async def test_unknown_name_stays_wxid(self):
        self.wechat._bot.group_card = ''
        self.assertEqual(await self.wechat.get_display_name('wxid_nobody', '456@chatroom'),
                         'wxid_nobody')

    async def test_find_member_matches_display_name_and_group_card(self):
        self.wechat._bot.members = [('wxid_alice', 'Alice', 'AliceGroupCard'), ('wxid_bob', 'Bob', '')]
        self.assertEqual(await self.wechat.find_member('456@chatroom', 'Bob'), 'wxid_bob')
        self.assertEqual(await self.wechat.find_member('456@chatroom', 'AliceGroupCard'), 'wxid_alice')

    async def test_find_member_prefers_exact_over_partial(self):
        """ @ 错人比不 @ 更难看：模糊命中不止一个就不猜。 """
        self.wechat._bot.members = [('wxid_a', '小张', ''), ('wxid_b', '张老师', '')]
        self.assertEqual(await self.wechat.find_member('456@chatroom', '张'), '')
        self.assertEqual(await self.wechat.find_member('456@chatroom', '小张'), 'wxid_a')

    async def test_find_member_is_a_noop_outside_chatrooms(self):
        self.wechat._bot.members = [('wxid_alice', 'Alice', '')]
        self.assertEqual(await self.wechat.find_member('wxid_alice', 'Alice'), '')
        self.assertEqual(await self.wechat.find_member('456@chatroom', ''), '')


if __name__ == '__main__':
    unittest.main()
