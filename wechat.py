from collections.abc import Callable, Coroutine
from typing import Any

from loguru import logger
from weixin_bot import Message, WeChatBot

from context import ChatWindow
from message import parse_reference_message


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
        """ 分发消息到回调函数 """
        logger.debug(msg)

        match msg.msg_type:
            case 1:  # 文本消息
                await self._message_callback(msg)
            case 3:  # 图片（预处理器按需下载）
                await self._message_callback(msg)
            case 37:  # 好友请求
                pass
            case 49:  # 引用消息
                self._process_reference(msg)
                await self._message_callback(msg)
            case 10000:  # 系统信息
                pass
            case _:
                logger.warning(f'Unknown message type: {msg.msg_type}')

    @staticmethod
    def _process_reference(msg: Message) -> None:
        """ 解析引用消息，把被引用的正文拼进 content """
        try:
            parsed = parse_reference_message(msg.content)
            text = parsed['content'] + f'（引用前文的消息：{parsed["referred_message"]}）'
            msg.content = msg.clean_text = text
        except Exception as e:
            logger.error(f'parsing reference message: {e}')

    @staticmethod
    async def _on_message(msg: Message) -> None:
        """ 默认消息处理函数 """
        logger.info(f'{msg.sender}: {msg.content}')

    async def send_text(self, msg: str, receiver: str, at_list: str = '') -> None:
        """ 发送消息
        :param msg: 消息字符串
        :param receiver: 接收人wxid或者群id
        :param at_list: 要@的wxid, 多个用逗号分隔; @所有人的wxid为：notify@all
        """
        if self.dry_run:
            logger.info(f'[DRY RUN] To {receiver}: {msg}')
            return

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
        await self._bot.send_text(to=receiver, text=text, at_users=at_users, at_names=at_names)

    async def get_recent_sessions(self, count: int = 10) -> list[str]:
        """ 获取最近的聊天会话 """
        conversations = await self._bot.get_conversations(limit=count)
        return [c.peer for c in conversations
                if c.peer.startswith('wxid_') or c.peer.endswith('@chatroom')]

    async def get_display_name(self, wxid: str, chatroom: str = '') -> str:
        """ 获取联系人的显示名称. 对于群友，优先使用本地的备注，其次使用群昵称，最后使用微信昵称 """
        if chatroom == wxid:  # 非群聊
            chatroom = ''

        if chatroom and wxid:
            try:
                member = await self._bot.get_chatroom_member(chatroom, wxid)
                if member.name:
                    return member.name
            except Exception as e:
                logger.debug(f'failed to get chatroom member {wxid} in {chatroom}: {e}')

        if wxid == self.wxid:
            return self.nickname or wxid

        # 使用兜底方案：联系人表中查找昵称
        return self.all_contacts.get(wxid, wxid)

    async def fetch_history(self, wxid: str, count: int = 50) -> ChatWindow:
        """ 获取群或联系人的聊天记录

        返回一个列表。每个元素为一个元组，格式为 (发送者名称, 消息内容, 创建时间)
        """
        messages = await self._bot.get_messages(limit=count, user=wxid, reverse=True, exclude_system=True)

        result = ChatWindow()
        for msg in messages:
            content = ''
            if msg.msg_type == 1:  # 纯文本消息
                content = msg.text
            elif msg.msg_type == 49:  # 引用消息
                try:
                    parsed = parse_reference_message(msg.content)
                    content = parsed['content'] + f'（引用前文的消息：{parsed["referred_message"]}）'
                except Exception:
                    continue
            elif msg.msg_type == 3:  # 图片消息
                # TODO: 加载缓存的图片描述信息
                continue

            # 不知道为什么有的消息还是 xml 格式的，这里直接过滤掉吧
            if content and not content.startswith('<'):
                result.append(msg.sender_name, content, msg.create_time)

        result.sort()
        return result
