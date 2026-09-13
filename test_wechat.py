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

    async def get_all_contacts(self, **kwargs):
        return [types.SimpleNamespace(username='wxid_alice', name='Alice')]

    async def get_conversations(self, limit):
        return [types.SimpleNamespace(peer='456@chatroom'), types.SimpleNamespace(peer='gh_news'),
                types.SimpleNamespace(peer='wxid_bob')]

    async def get_messages(self, limit, user, reverse, exclude_system):
        return [
            make_message(msg_type=1, content='第一条', clean_text='第一条', create_time=200),
            make_message(msg_type=3, content='<img cdnurl="x"/>', clean_text='', create_time=201),
            make_message(msg_type=49, content=QUOTE_XML, clean_text=QUOTE_XML, create_time=202),
        ]

    async def get_chatroom_member(self, chatroom, wxid):
        return types.SimpleNamespace(name='AliceGroupCard')

    async def send_text(self, to, text, at_users, at_names):
        self.sent.append({'to': to, 'text': text, 'at_users': at_users, 'at_names': at_names})
        return {'sent': True}


class TestWechatFacade(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.wechat = Wechat('127.0.0.1', 9999)
        self.wechat._bot = FakeBot()  # type: ignore[assignment]
        self.dispatched: list[Message] = []
        self.wechat._message_callback = self._collect

    async def _collect(self, msg):
        self.dispatched.append(msg)

    async def test_dispatch_quote_rewrites_content(self):
        await self.wechat._dispatch(make_message(msg_type=49, content=QUOTE_XML, clean_text=QUOTE_XML))
        self.assertEqual(len(self.dispatched), 1)
        msg = self.dispatched[0]
        self.assertEqual(msg.content, '回复内容（引用前文的消息：被引用的内容）')
        self.assertEqual(msg.text, msg.content)

    async def test_dispatch_ignores_unknown_type(self):
        await self.wechat._dispatch(make_message(msg_type=10000))
        self.assertEqual(self.dispatched, [])

    async def test_fetch_history_maps_and_filters(self):
        window = await self.wechat.fetch_history('456@chatroom', count=10)
        # 图片被跳过，只剩文本和引用
        self.assertEqual(len(window), 2)
        self.assertEqual(window[0].sender, 'Alice')
        self.assertEqual(window[0].text, '第一条')
        self.assertEqual(window[1].text, '回复内容（引用前文的消息：被引用的内容）')
        self.assertEqual([r.t for r in window], [200, 202])

    async def test_get_recent_sessions_filters_non_contacts(self):
        sessions = await self.wechat.get_recent_sessions(3)
        self.assertEqual(sessions, ['456@chatroom', 'wxid_bob'])

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


if __name__ == '__main__':
    unittest.main()
