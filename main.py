import logging
import signal
import time
from typing import override

from chat import ChatAI, ChatGLM
from context import ContextManager, ChatWindow
from plugin import *
from wechat import WxBot, RawMessage


logging.basicConfig(level=logging.INFO)


class WxHelper(WxBot):
    """ 微信机器人助手 """

    _plugin_mappings: dict[str, list[Plugin]]
    """ 插件映射表. 键为联系人名称，值为插件列表 """

    def __init__(self, host: str, port: int):
        super().__init__(host, port)
        self._plugin_mappings = {}
        self._context = ContextManager()
        self.self_info = self.get_myself()
        logging.info(f'Hello, {self.self_info["name"]}!')

    def set(self, contact: str, plugins: list[Plugin]):
        """ 设置插件 """
        self._plugin_mappings[contact] = plugins

    def get_myself(self) -> dict:
        """ 获取自己的微信信息
        {wxid, code, name, gender}
        """
        return self.wcf.get_user_info()

    def load_context(self) -> None:
        """ 加载上下文 """
        for contact in self.get_recent_sessions(10):
            messages = self.fetch_history(contact)
            messages = [ChatWindow.message_template(user, text) for user, text in messages]
            context = ChatWindow(messages)
            self._context.push_window(context)

    @override
    def on_message(self, msg: RawMessage) -> None:
        """ 消息处理函数 """
        if msg.from_self():
            return

        # 加入聊天记录缓存
        self._context.push_message(msg.roomid, msg.content, self.all_contacts.get(msg.sender, msg.sender))

        # 消息来源。如果是群消息，则为群名称；否则为联系人备注
        name = lambda x: self.all_contacts.get(x, x)
        source = name(msg.roomid)
        logging.info(f'New message from {name(msg.roomid)}: {msg.content}')

        # 消息处理
        response_back: str | None = None

        for plugin in self._plugin_mappings.get(source, [DefaultPlugin()]):
            response_back, _continue = plugin.handle(msg)
            if response_back or not _continue:
                break
        if not response_back:
            return  # 没有回复内容

        # 发送消息
        if msg.from_group():
            self.send_text_msg(response_back, msg.roomid, msg.sender)
            self._context.push_message(msg.roomid, response_back, name(msg.sender))
        else:
            self.send_text_msg(response_back, msg.sender)
            self._context.push_message(msg.sender, response_back)


def main():
    from config import CONFIG
    host, port = CONFIG['wcf-host'], CONFIG['wcf-port']
    FERRY: WxHelper = WxHelper(host, port)

    # 捕获 Ctrl+C 信号
    def signal_handler(sig, frame):
        logging.info('Ctrl+C pressed. Exit.')
        FERRY.stop_receiving_message()
        FERRY.cleanup()
        exit(0)

    signal.signal(signal.SIGINT, signal_handler)

    # 设置插件
    ai_implementation: ChatAI = ChatGLM(key=CONFIG['chatglm-key'], name=FERRY.self_info['name'])

    FERRY.set('后端重构开发群', [WeatherPlugin(), IdiomPlugin()])
    FERRY.set('研究生摆烂群', [DoNothingPlugin()])

    FERRY.load_context()

    # 开始接收消息
    FERRY.start_receiving_message()
    while True:
        time.sleep(1)

if __name__ == '__main__':
    main()