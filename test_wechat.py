""" Wechat 适配层自检：不连 relay，用 stub 替换 SDK 客户端。 """

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
        # 实测 relay 查不到群成员时是拿 wxid 当名字返回的；置空模拟这种情况
        self.group_card = 'AliceGroupCard'
        self.contacts = {'wxid_alice': 'Alice', '456@chatroom': '一起哈啤'}

    async def get_all_contacts(self, **kwargs):
        return [types.SimpleNamespace(username='wxid_alice', name='Alice')]

    async def get_conversations(self, limit):
        self.requested_limit = limit
        return [types.SimpleNamespace(peer='456@chatroom'), types.SimpleNamespace(peer='gh_news'),
                types.SimpleNamespace(peer='wxid_bob')]

    async def get_messages(self, limit, user, reverse, exclude_system):
        return [
            make_message(msg_type=1, content='第一条', clean_text='第一条', create_time=200),
            make_message(msg_type=3, content='<img cdnurl="x"/>', clean_text='', create_time=201),
            make_message(msg_type=49, content=QUOTE_XML, clean_text=QUOTE_XML, create_time=202),
        ]

    async def get_chatroom_member(self, chatroom, wxid):
        return types.SimpleNamespace(name=self.group_card or wxid)

    async def get_contact(self, user):
        self.contact_calls.append(user)
        name = self.contacts.get(user)
        return [types.SimpleNamespace(username=user, name=name)] if name else []

    async def send_text(self, to, text, at_users, at_names):
        self.sent.append({'to': to, 'text': text, 'at_users': at_users, 'at_names': at_names})
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
        self.assertEqual(window[2].text, '回复内容（引用前文的消息：被引用的内容）')
        self.assertEqual([r.t for r in window], [200, 201, 202])

    async def test_get_recent_sessions_filters_non_contacts(self):
        sessions = await self.wechat.get_recent_sessions(3)
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

    async def test_dry_run_does_not_send(self):
        self.wechat.dry_run = True
        await self.wechat.send_text('你好', '456@chatroom')
        self.assertEqual(self.wechat._bot.sent, [])

    async def test_member_name_falls_back_when_relay_returns_wxid(self):
        """ relay 查不到群成员就返 wxid；那不是名字，得退到联系人表。 """
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

        分块流那条路：relay 的 DL_OPEN 分支按 RelayRequest（action + params）解包，
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

        # relay 说没内容时要报得诚实，而不是拿一个空字节流充数
        self.reply = {'filename': 'x', 'size': 0}
        with self.assertRaises(ValueError):
            await self.wechat.media_bytes(make_message(msg_id=778))

    async def test_unknown_name_stays_wxid(self):
        self.wechat._bot.group_card = ''
        self.assertEqual(await self.wechat.get_display_name('wxid_nobody', '456@chatroom'),
                         'wxid_nobody')


if __name__ == '__main__':
    unittest.main()
