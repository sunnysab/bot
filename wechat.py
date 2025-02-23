import asyncio
import re
import time
from collections.abc import Callable
from queue import Empty
from threading import Thread
from typing import Tuple, Optional, Coroutine, Any

from loguru import logger
from wcferry import Wcf, WxMsg as RawMessage

from context import ChatWindow
from message import decode_sender_name, decode_compress_content


class Message(RawMessage):

    def __init__(self, raw: RawMessage):
        for key, value in raw.__dict__.items():
            setattr(self, key, value)

        # 图片或语音的音频文件的 URL
        self.resource_url: Optional[str] = None

    def __repr__(self):
        if self.roomid != self.sender:  # 群消息
            return f'<Message from {self.sender} in {self.roomid}: {self.content}>'
        return f'<Message from {self.sender}: {self.content}>'


class Wechat:
    """ 微信机器人组件 """

    AsyncMessageCb = Callable[[Message], Coroutine[Any, Any, None]]

    def __init__(self, host: str, port: int, remote_storage_path: str, remote_server_prefix: str,
                 callback: AsyncMessageCb = None, dry_run: bool = False):
        logger.info('starting robot...')
        self.wcf = Wcf(host, port)
        logger.info('connected to wechatferry.')

        self.remote_storage_path = remote_storage_path.replace('\\', '/')
        self.remote_server_prefix = remote_server_prefix
        self.dry_run = dry_run
        self.self_info = self.get_myself()
        self.wxid = self.wcf.get_self_wxid()
        self._cached_display_name = dict()
        self.all_contacts = self.get_all_contacts()
        self._message_callback = callback or self._on_message
        self._daemon_running = False

    def cleanup(self):
        self.wcf.cleanup()

    def get_all_contacts(self) -> dict:
        """ 获取联系人（包括好友、公众号、服务号、群成员……）
        :return: 联系人字典，键值对. 格式: {'wxid': 'NickName'}
        """
        contacts = self.wcf.query_sql('MicroMsg.db', 'SELECT UserName, NickName FROM Contact;')
        return {contact['UserName']: contact['NickName'] for contact in contacts}

    async def _say_hi_to_new_friend(self, msg: RawMessage) -> None:
        """ 自动发送欢迎消息给新好友 """
        PATTERN_1 = r'你已添加了(.*)，现在可以开始聊天了。'
        PATTERN_2 = 'You have added (.*) as your Weixin contact. Start chatting!'
        nick_name = re.findall(PATTERN_1, msg.content) or re.findall(PATTERN_2, msg.content)
        if nick_name:
            self.all_contacts[msg.sender] = nick_name[0]
            await self.send_text(f'[Doge] {nick_name[0]}你终于加我了！！', msg.sender)

        raise Exception('fail to get new friend\'s nickname.')

    async def _fetch_image(self, msg: RawMessage) -> str:
        message_id, extra = msg.id, msg.extra
        actual_path = self.wcf.download_image(message_id, extra, self.remote_storage_path)
        if not actual_path:
            raise Exception(f'failed to download image (unknown reason)')

        logger.info(f'new image saved to {actual_path}')
        relative_path = actual_path.replace(self.remote_storage_path, '').lstrip('/')
        url = f'{self.remote_server_prefix}/{relative_path}'
        return url

    def _process_reference(self, msg: RawMessage) -> RawMessage:
        """ Process reference message by parsing content """
        if msg.type != 49:  # Not a reference message
            return msg

        from message import parse_reference_message
        try:
            parsed_message = parse_reference_message(msg.content)
            msg.content = parsed_message['content'] + \
                          f'（引用前文的消息：{parsed_message['referred_message']}）'
            return msg
        except Exception as e:
            logger.error(f'parsing reference message: {e}')
            return msg

    @staticmethod
    async def _on_message(msg: Message) -> None:
        """ Default message handler """
        logger.info(f'{msg.sender}: {msg.content}')

    async def _process_message(self, msg: RawMessage) -> None:
        """ 处理消息. 将消息放入回调函数中处理 """

        logger.debug(msg)
        msg = Message(msg)

        match msg.type:
            case 1:  # 文本消息
                await self._message_callback(msg)
            case 3:  # 图片
                msg.resource_url = await self._fetch_image(msg)
                await self._message_callback(msg)
            case 37:  # 好友请求
                pass
            case 49:  # 引用消息
                msg.content = self._process_reference(msg)
                await self._message_callback(msg)
            case 10000:  # 系统信息
                # self._say_hi_to_new_friend(msg)
                pass
            case _:
                logger.warning(f'Unknown message type: {msg.type}')

    def start_receiving_message(self) -> None:
        async def f(wcf: Wcf):
            loop = asyncio.get_event_loop()
            while wcf.is_receiving_msg():
                try:
                    msg = await loop.run_in_executor(None, wcf.get_msg, True)
                    await self._process_message(msg)
                except Empty:
                    continue  # Empty message
                except Exception as e:
                    logger.error(f'processing message error: {e}')

            self._daemon_running = False

        def thread(wcf: Wcf):
            asyncio.run(f(wcf))

        self.wcf.enable_receiving_msg()
        self._daemon_running = True
        Thread(target=thread, name='GetMessage', args=(self.wcf,), daemon=True).start()

    def stop_receiving_message(self) -> None:
        """ 停止接收消息 """
        self.wcf.disable_recv_msg()

        logger.info('waiting for message receiving thread to stop...')
        while self._daemon_running:
            time.sleep(0.1)
        logger.info('message receiving thread stopped.')

    def get_myself(self) -> dict:
        """ 获取自己的微信信息
        {wxid, code, name, gender}
        """
        return self.wcf.get_user_info()

    async def send_text(self, msg: str, receiver: str, at_list: str = '') -> None:
        """ 发送消息
        :param msg: 消息字符串
        :param receiver: 接收人wxid或者群id
        :param at_list: 要@的wxid, @所有人的wxid为：notify@all
        """
        if self.dry_run:
            logger.info(f'[DRY RUN] To {receiver}: {msg}')
            return

        ats = ''
        if at_list:
            if at_list == 'notify@all':  # @所有人
                ats = ' @所有人'
            else:
                wxids = at_list.split(',')
                for wxid in wxids:
                    ats += f' @{self.wcf.get_alias_in_chatroom(wxid, receiver)}'

        if not ats:
            logger.info(f'To {receiver}: {msg}')
            self.wcf.send_text(f'{msg}', receiver, at_list)
        else:
            logger.info(f'To {receiver}: {ats}\r{msg}')
            self.wcf.send_text(f'{ats}\n\n{msg}', receiver, at_list)

    async def get_recent_sessions(self, count: int = 10):
        """ 获取最近的聊天会话 """
        SQL = f'SELECT Username FROM ChatInfo ORDER BY LastReadedCreateTime DESC LIMIT {count};'
        records = self.wcf.query_sql('MicroMsg.db', SQL)

        def filter_wxid(wxid: str) -> bool:
            return wxid.startswith('wxid_') or wxid.endswith('@chatroom')

        return [x['Username'] for x in records if filter_wxid(x['Username'])]

    async def get_display_name(self, wxid: str, chatroom: str = '') -> str:
        """ 获取联系人的显示名称. 对于群友，优先使用本地的备注，其次使用群昵称，最后使用微信昵称 """
        if chatroom == wxid:  # 非群聊
            chatroom = ''

        if (wxid, chatroom) in self._cached_display_name:
            return self._cached_display_name[(wxid, chatroom)]

        if chatroom:
            if alias := self.wcf.get_alias_in_chatroom(wxid, chatroom):
                self._cached_display_name[(wxid, chatroom)] = alias
                return alias

        if wxid == self.wxid:
            return self.self_info['name']

        # 使用兜底方案：在 contacts 表中查找昵称
        return self.all_contacts.get(wxid, wxid)

    async def fetch_history(self, wxid: str, count: int = 50) -> ChatWindow:
        """ 获取群或联系人的聊天记录

        返回一个列表。每个元素为一个元组，格式为 (发送者名称, 消息内容, 创建时间)
        """
        # 注意在 SQL 的结尾补一个空格.
        SQL = '''SELECT IsSender, BytesExtra, CompressContent, StrContent, Type, SubType, CreateTime FROM msg ''' \
              f'''WHERE StrTalker = "{wxid}" AND (Type = 1 OR (Type = 49 AND SubType = 57)) ''' \
              f'''ORDER BY CreateTime DESC LIMIT {count};'''
        records = self.wcf.query_sql('MSG0.db', SQL)

        async def parse_record(wxid: str, record: dict) -> Tuple[str, str, int] | None:
            is_group: bool = wxid.endswith('@chatroom')
            is_self: bool = record['IsSender'] == 1
            if is_group:
                sender_wxid: str = decode_sender_name(record['BytesExtra'])
            else:
                sender_wxid: str = self.wxid if is_self else wxid
            # 如果是群聊，get_display_name 可以获取发送者的群昵称
            sender_name: str = await self.get_display_name(sender_wxid, wxid)

            content = ''
            if record['Type'] == 1:  # 纯文本消息
                content = record['StrContent']
            elif record['Type'] == 3:  # 图片消息
                # TODO: 加载缓存的图片描述信息
                pass
            elif record['Type'] == 49 and record['SubType'] == 57:  # 引用消息
                content = decode_compress_content(record['CompressContent'])

            # 不知道为什么有的 StrContent 中的消息还是 xml 格式的.
            # Type == 1 时 StrContent 应该就是纯文本。而对于引用消息，它为空.
            # 这里直接过滤掉吧.
            if not content.startswith('<'):
                return sender_name, content, record['CreateTime']

        async def try_parse_record(wxid: str, record: dict) -> Tuple[str, str, int] | None:
            try:
                return await parse_record(wxid, record)
            except Exception as e:
                # logger.error(f'Error on processing record: {e}')
                return None

        result = ChatWindow()
        for record in records:
            if parsed := await try_parse_record(wxid, record):
                result.append(*parsed)
        result.sort()
        return result
