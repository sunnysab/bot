import logging
import signal
import time
from typing import override
from plugin import *

from wechat import WxBot, RawMessage

HOST = '192.168.2.105'
PORT = 10086

logging.basicConfig(level=logging.INFO)


class WxHelper(WxBot):
    """ 微信机器人助手 """

    plugin_mappings: dict[str, list[Plugin]]
    """ 插件映射表. 键为联系人名称，值为插件列表 """

    def __init__(self, host: str, port: int):
        super().__init__(host, port)
        self.plugin_mappings = {}

    def set(self, contact: str, plugins: list[Plugin]):
        """ 设置插件 """
        self.plugin_mappings[contact] = plugins

    @override
    def on_message(self, msg: RawMessage):
        """ 消息处理函数 """
        if msg.from_self():
            return

        # 消息来源。如果是群消息，则为群名称；否则为联系人备注
        source = msg.roomid or msg.sender
        display_source = self.all_contacts.get(source, source)
        logging.info(f'New message from {display_source}: {msg.content}')

        # 消息处理
        response_back: str | None = None

        for plugin in self.plugin_mappings.get(display_source, [DefaultPlugin()]):
            response_back = plugin.handle(msg)
            if response_back:
                break
        if not response_back:
            return # 没有回复内容

        # 发送消息
        if msg.from_group():
            self.send_text_msg(response_back, msg.roomid, msg.sender)
        else:
            self.send_text_msg(response_back, msg.sender)

FERRY: WxHelper = WxHelper(HOST, PORT)

def cleanup():
    logging.info('Cleaning up before exit...')
    FERRY.stop_receiving_message()
    FERRY.cleanup()
    exit(0)

def signal_handler(sig, frame):
    logging.info('Ctrl+C pressed. Exit.')
    cleanup()

signal.signal(signal.SIGINT, signal_handler)


FERRY.start_receiving_message()
FERRY.set('后端重构开发群', [WeatherPlugin(), IdiomPlugin()])
while True:
    time.sleep(1)