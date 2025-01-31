import logging
import signal
from typing import override

from chat import ChatGLM, Ollama, Deepseek
from context import ContextManager
from plugin import *
from wechat import WxBot, RawMessage

logging.basicConfig(level=logging.DEBUG)


class WxHelper(WxBot):
    """ 微信机器人助手 """

    _plugin_mappings: dict[str, list[Plugin]]
    _default_plugin = DefaultPlugin()

    """ 插件映射表. 键为联系人名称，值为插件列表 """

    def __init__(self, host: str, port: int, _default_plugin: Plugin = DefaultPlugin()):
        super().__init__(host, port)
        self._default_plugin = _default_plugin
        self._plugin_mappings = {}
        self._context = ContextManager()
        logging.info(f'Hello, {self.self_info["name"]}!')

    def set_default_plugin(self, plugin: Plugin):
        """ 设置默认插件 """
        self._default_plugin = plugin

    def attach_plugin(self, contact: str, plugins: Plugin | list[Plugin]):
        """ 设置插件 """
        if isinstance(plugins, Plugin):
            plugins = [plugins]
        self._plugin_mappings[contact] = plugins

    def load_context(self) -> None:
        """ 加载上下文 """

        count_loaded = 0
        sessions = self.get_recent_sessions(10)
        for contact in sessions:
            messages = self.fetch_history(contact)
            count_loaded += len(messages)
            for user, text in messages:
                self._context.push_message(contact, text, user)

        logging.info(f'Loaded {len(sessions)} sessions, {count_loaded} messages.')

    @override
    def on_message(self, msg: RawMessage) -> None:
        """ 消息处理函数 """

        # 自己的消息不用回复，也不用存储。在发送的时候会自动存储。
        if msg.from_self():
            return

        if self._context.get_context(msg.roomid).empty():
            # 如果收到某人的消息，但是没有上下文，则尝试加载历史消息
            messages = self.fetch_history(msg.roomid)
            for user, text in messages:
                self._context.push_message(msg.roomid, text, user)

        # 加入聊天记录缓存
        self._context.push_message(msg.roomid, msg.content, self.all_contacts.get(msg.sender, msg.sender))

        # 自己的名字
        self_name = self.self_info['name']

        # 消息来源。如果是群消息，则为群名称；否则为联系人备注
        name = lambda x: self.all_contacts.get(x, x)
        source = name(msg.roomid)
        logging.info(f'New message from {name(msg.roomid)}: {msg.content}')

        # 消息处理
        response_back: list[str] | None = None
        for plugin in self._plugin_mappings.get(source, [self._default_plugin]):
            chat_context = self._context.get_context(msg.roomid)
            response_back, _continue = plugin.handle(msg, context=chat_context, self_name=self_name, contact=source)
            if response_back or not _continue:
                break
        if not response_back:
            logging.info('No response to send.')
            return  # 没有回复内容

        # 回复消息
        # TODO: 改成异步，加延时.
        for text in response_back:
            self.send_text_msg(text, msg.roomid)
            if msg.from_group():
                self._context.push_message(msg.roomid, text, self_name)
            else:
                self._context.push_message(msg.sender, text)


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
    # ai_provider: ChatAI = ChatGLM(key=CONFIG['chatglm-key'])
    ai_provider: ChatAI = Deepseek(key=CONFIG['deepseek-key'])
    chat_plugin = ChatPlugin(ai_provider, max_ignore=2, frequency=10, context_length=10)

    # FERRY.set_default_plugin(ChatPlugin(ai_implementation))
    # FERRY.attach_plugin('后端重构开发群', [DoNothingPlugin()])
    # FERRY.attach_plugin('研究生摆烂群', [DoNothingPlugin()])
    FERRY.attach_plugin('sunnysab', [ChatPlugin(ai_implementation)])

    FERRY.load_context()

    # 开始接收消息
    FERRY.start_receiving_message()
    while True:
        time.sleep(1)

if __name__ == '__main__':
    main()