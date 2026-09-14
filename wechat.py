import base64
from collections.abc import Callable, Coroutine
from typing import Any

from loguru import logger
from weixin_bot import Message, WeChatBot

from context import SYSTEM_KINDS, ChatWindow, MessageKind
from emojis import Emoji
from media import render


class Wechat:
    """ 微信机器人组件（weixin-rs relay 适配层） """

    AsyncMessageCb = Callable[[Message], Coroutine[Any, Any, None]]

    def __init__(self, host: str, port: int, callback: AsyncMessageCb = None, dry_run: bool = False,
                 unix_path: str = None):
        logger.info('starting robot...')
        self._bot = WeChatBot(host=host, port=port, unix_path=unix_path or None)
        self.dry_run = dry_run
        # 联系人字典，键值对格式: {'wxid': '显示名'}
        self.all_contacts: dict[str, str] = {}
        # 按需查到的名字（群名、群里的人）；只记成功的结果，查不到就下次再查
        self._names: dict[str, str] = {}
        self._message_callback = callback or self._on_message

        self._bot.on_message(self._dispatch)
        self._bot.on_ready(self._refresh_contacts)

    @property
    def wxid(self) -> str:
        return self._bot.wxid or ''

    @property
    def nickname(self) -> str:
        return self._bot.nickname or ''

    def on_ready(self, func) -> None:
        """ 注册连接就绪（含重连）后的回调 """
        self._bot.on_ready(func)

    async def start(self) -> None:
        """ 连接 relay 并持续接收消息，断线自动重连 """
        await self._bot.start()

    async def stop(self) -> None:
        await self._bot.close()

    async def _refresh_contacts(self, _bot: WeChatBot) -> None:
        contacts = await self._bot.get_all_contacts(force_refresh=True)
        self.all_contacts = {c.username: c.name for c in contacts}
        logger.info(f'Hello, {self.nickname}! ({len(self.all_contacts)} contacts)')

    async def _dispatch(self, msg: Message) -> None:
        """ 分发消息到回调函数。载荷渲染统一交给上层 PayloadProcessor。 """
        logger.debug(msg)

        kind = MessageKind.of(msg.msg_type)
        if kind in SYSTEM_KINDS:  # 好友请求 / 设备同步 / 系统消息
            return
        if kind is MessageKind.OTHER:
            logger.warning(f'Unknown message type: {msg.msg_type}')
            return
        await self._message_callback(msg)

    @staticmethod
    async def _on_message(msg: Message) -> None:
        """ 默认消息处理函数 """
        logger.info(f'{msg.sender}: {msg.content}')

    async def send_text(self, msg: str, receiver: str, at_list: str = '') -> dict:
        """ 发送消息
        :param msg: 消息字符串
        :param receiver: 接收人wxid或者群id
        :param at_list: 要@的wxid, 多个用逗号分隔; @所有人的wxid为：notify@all
        """
        if self.dry_run:
            logger.info(f'[DRY RUN] To {receiver}: {msg}')
            return {'sent': False, 'dry_run': True}

        at_users: list[str] = []
        at_names: list[str] = []
        ats = ''
        if at_list:
            if at_list == 'notify@all':  # @所有人
                at_users = ['notify@all']
                ats = ' @所有人'
            else:
                for wxid in filter(None, at_list.split(',')):
                    name = await self.get_display_name(wxid, receiver)
                    at_users.append(wxid)
                    at_names.append(name)
                    ats += f' @{name}'

        text = f'{ats}\n\n{msg}' if ats else msg
        logger.info(f'To {receiver}: {text}')
        # 返回值里带 relay 给的 msg_id（微信服务端消息 ID）。管理后台靠它把
        # 「排队中 → 已发送」贴到 relay 库里的那条出站记录上，不能丢
        return await self._bot.send_text(to=receiver, text=text, at_users=at_users, at_names=at_names)

    async def search_emoji(self, keyword: str, limit: int = 10) -> list[Emoji]:
        """ 按关键词搜表情。返回的已经是可直接发送的完整元数据。 """
        found = await self._bot.search_emoji(keyword)
        emojis = [Emoji.from_dict(item.to_dict() if hasattr(item, 'to_dict') else item) for item in found]
        return [item for item in emojis if item.sendable][:limit]

    async def send_emoji(self, emoji: Emoji, receiver: str) -> dict:
        """ 发一张表情。返回值带 relay 的 msg_id，理由同 send_text。 """
        if not emoji.sendable:
            logger.warning(f'表情缺下载地址，发不出去：{emoji.md5}')
            return {'sent': False}
        if self.dry_run:
            logger.info(f'[DRY RUN] To {receiver}: 表情「{emoji.desc or emoji.md5}」')
            return {'sent': False, 'dry_run': True}
        logger.info(f'To {receiver}: 表情「{emoji.desc or emoji.md5}」')
        return await self._bot.send_emoji(to=receiver, emoji=emoji.to_relay())

    async def conversations(self, limit: int = 20):
        """ 管理后台用：会话列表（带 display_name / last_message / msg_count）。 """
        return await self._bot.get_conversations(limit=limit)

    async def status(self) -> dict:
        """ relay 的连接 / 登录状态。管理后台的健康卡用。 """
        return await self._bot.get_status()

    async def media_bytes(self, msg: Message) -> bytes:
        """ 取一条消息附件的原始字节。

        **不走 SDK 的 `msg.get_media_bytes()`**：那条路走 PKT_DL_OPEN 分块流，
        而 relay 的 DL_OPEN 分支按 `RelayRequest`（action + params）解包，SDK 只塞了
        params，解不出就被 `unwrap_or_default()` 吞掉，最后报 400「缺少 msg_id」——
        报错和真实原因（图片的 XML 里没有 cdnurl）完全无关，查半天。

        这里走普通 RPC 的 `get_media_bytes`（形状是对的），拿 base64 再解回来，
        失败时的报错就是 relay 的原始报错。
        """
        data = await self._bot.call('get_media_bytes', {'msg_id': msg.msg_id})
        encoded = (data or {}).get('data_base64') if isinstance(data, dict) else None
        if not encoded:
            raise ValueError(f'relay 没返回附件内容（msg_id={msg.msg_id}）')
        return base64.b64decode(encoded)

    async def messages(self, peer: str, limit: int = 300, exclude_system: bool = True):
        """ 管理后台用：某个会话的历史。

        直接返回 relay 的原始消息，不走 fetch_history 那层历史记录封装——
        面板要 msg_id 才能把状态贴上去，也要原始 XML 才能渲染媒体。
        """
        return await self._bot.get_messages(limit=limit, user=peer, reverse=True,
                                            exclude_system=exclude_system)

    async def sender_names(self, chat: str, senders) -> dict[str, str]:
        """ 把 wxid 批量解析成显示名（群昵称 > 备注）。

        relay 的 `sender_display_name` 查不到时会**拿 wxid 当名字**返回，那不是名字。
        bot 自己的消息处理链会退到 `get_contact` 再查一次（见 get_display_name），
        面板走的是 relay 原始消息，没经过那道，所以要自己补。
        实测某群里 278 条有 130 条的发送者是 wxid，但只有 4 个不同的人——
        解析一遍就都好了，而且查到的名字会缓存。
        """
        names = {}
        for sender in dict.fromkeys(senders):
            if sender:
                names[sender] = await self.get_display_name(sender, chat)
        return names

    async def get_recent_sessions(self, count: int = 10) -> list[str]:
        """ 获取最近的聊天会话 """
        # 公众号推送会占满会话列表，多取一些再过滤掉
        conversations = await self._bot.get_conversations(limit=max(count * 5, 50))
        sessions = [c.peer for c in conversations
                    if c.peer.startswith('wxid_') or c.peer.endswith('@chatroom')]
        return sessions[:count]

    async def get_display_name(self, wxid: str, chatroom: str = '') -> str:
        """ 获取联系人的显示名称. 对于群友，优先使用本地的备注，其次使用群昵称，最后使用微信昵称

        注意 relay 查不到群成员时会**拿 wxid 当名字**返回，那不是名字，得接着往下找。
        """
        if chatroom == wxid:  # 非群聊
            chatroom = ''

        if wxid == self.wxid:
            return self.nickname or wxid

        key = f'{chatroom}|{wxid}' if chatroom else wxid
        if key in self._names:
            return self._names[key]

        name = ''
        if chatroom and wxid:
            try:
                member = await self._bot.get_chatroom_member(chatroom, wxid)
                if member.name and member.name != wxid:
                    name = member.name
            except Exception as e:
                logger.debug(f'failed to get chatroom member {wxid} in {chatroom}: {e}')

        if not name:
            name = await self._resolve_contact(wxid)

        if name:
            self._names[key] = name
        return name or wxid

    async def _resolve_contact(self, wxid: str) -> str:
        """ 从联系人表查显示名，查不到就现查一次。

        群名也走这条路：`get_all_contacts` 不含群，得单独 `get_contact` 才拿得到。
        """
        if wxid in self.all_contacts:
            return self.all_contacts[wxid]
        try:
            for contact in await self._bot.get_contact(wxid):
                if contact.username == wxid and contact.name:
                    self.all_contacts[wxid] = contact.name
                    return contact.name
        except Exception as e:
            logger.debug(f'failed to resolve contact {wxid}: {e}')
        return ''

    async def fetch_history(self, wxid: str, count: int = 50) -> ChatWindow:
        """ 获取群或联系人的聊天记录

        返回一个列表。每个元素为一个元组，格式为 (发送者名称, 消息内容, 创建时间)
        """
        messages = await self._bot.get_messages(limit=count, user=wxid, reverse=True, exclude_system=True)

        result = ChatWindow()
        for msg in messages:
            content, media = render(msg.msg_type, msg.content, msg.text)
            # 不知道为什么有的消息还是 xml 格式的，这里直接过滤掉吧
            if not content or content.startswith('<'):
                continue
            download = (lambda m=msg: self.media_bytes(m)) if (media and media.describable) else None
            result.append(msg.sender_name, content, msg.create_time, media=media, download=download,
                          is_self=msg.is_self)

        result.sort()
        return result
