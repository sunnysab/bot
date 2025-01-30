import re
import logging
from collections.abc import Callable
from queue import Empty
from threading import Thread
from wcferry import Wcf, WxMsg as RawMessage


class WxBot:
    """ 微信机器人组件 """

    def __init__(self, host: str, port: int, callback: Callable[[RawMessage], None]=None):
        logging.info('starting robot...')
        self.wcf = Wcf(host, port)
        logging.info('connected to wechatferry.')

        self.wxid = self.wcf.get_self_wxid()
        self.all_contacts = self.get_all_contacts()
        self.message_callback = callback or self.on_message

    def cleanup(self):
        self.wcf.cleanup()

    def get_all_contacts(self) -> dict:
        """ 获取联系人（包括好友、公众号、服务号、群成员……）
        :return: 联系人字典，键值对. 格式: {'wxid': 'NickName'}
        """
        contacts = self.wcf.query_sql('MicroMsg.db', 'SELECT UserName, NickName FROM Contact;')
        return {contact['UserName']: contact['NickName'] for contact in contacts}

    def _auto_accept_friend_request(self, msg: RawMessage) -> None:
        """ 自动通过好友请求 """
        import xml.etree.ElementTree as ET

        try:
            xml = ET.fromstring(msg.content)
            v3 = xml.attrib['encryptusername']
            v4 = xml.attrib['ticket']
            scene = int(xml.attrib['scene'])
            logging.info(f'Accepting friend request from {v3} (ticket: {v4})')
            self.wcf.accept_new_friend(v3, v4, scene)
            logging.info(f'Accepted friend request from {v3}')
        except Exception as e:
            logging.error(f'Failed to accept friend: {e}')

    def _say_hi_to_new_friend(self, msg: RawMessage) -> None:
        """ 自动发送欢迎消息给新好友 """
        PATTERN_1 = r'你已添加了(.*)，现在可以开始聊天了。'
        PATTERN_2 = 'You have added (.*) as your Weixin contact. Start chatting!'
        nick_name = re.findall(PATTERN_1, msg.content) or re.findall(PATTERN_2, msg.content)
        if nick_name:
            self.all_contacts[msg.sender] = nick_name[0]
            self.send_text_msg(f'[Doge] {nick_name[0]}你终于加我了！！', msg.sender)

        raise Exception('Failed to get new friend\'s nickname.')

    def _process_message(self, msg: RawMessage) -> None:
        """ 处理消息. 将消息放入回调函数中处理 """
        match msg.type:
            case 1: # 文本消息
                self.message_callback(msg)
            case 37: # 好友请求
                self._auto_accept_friend_request(msg)
            case 10000: # 系统信息
                self._say_hi_to_new_friend(msg)
            case _:
                logging.info(f'Unknown message type: {msg.type}')
                logging.info(msg)

    @staticmethod
    def on_message(msg: RawMessage):
        """ 默认的文本消息处理方法, 输出消息内容 """
        try:
            logging.info(msg)
        except Exception as e:
            logging.error(e)

    def start_receiving_message(self) -> None:
        def inner_process_msg(wcf: Wcf):
            while wcf.is_receiving_msg():
                try:
                    msg = wcf.get_msg()
                    logging.info(msg)
                    self._process_message(msg)
                except Empty:
                    continue  # Empty message
                except Exception as e:
                    logging.error(f'Error on receiving: {e}')

        self.wcf.enable_receiving_msg()
        Thread(target=inner_process_msg, name='GetMessage', args=(self.wcf,), daemon=True).start()

    def stop_receiving_message(self) -> None:
        """ 停止接收消息 """
        self.wcf.disable_recv_msg()

    def send_text_msg(self, msg: str, receiver: str, at_list: str = '') -> None:
        """ 发送消息
        :param msg: 消息字符串
        :param receiver: 接收人wxid或者群id
        :param at_list: 要@的wxid, @所有人的wxid为：notify@all
        """
        ats = ''
        if at_list:
            if at_list == 'notify@all':  # @所有人
                ats = ' @所有人'
            else:
                wxids = at_list.split(',')
                for wxid in wxids:
                    ats += f' @{self.wcf.get_alias_in_chatroom(wxid, receiver)}'

        if not ats:
            logging.info(f'To {receiver}: {msg}')
            self.wcf.send_text(f'{msg}', receiver, at_list)
        else:
            logging.info(f'To {receiver}: {ats}\r{msg}')
            self.wcf.send_text(f'{ats}\n\n{msg}', receiver, at_list)