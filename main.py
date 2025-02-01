import signal
from typing import override

import requests
from loguru import logger

from chat import Deepseek, ChatGLM
from config import CONFIG
from context import ContextManager
from message import parse_reference_message
from plugin import *
from wechat import WxBot, RawMessage

logger.add(sink='bot.log', rotation='1 week', retention='7 days', level='DEBUG')


class WxHelper(WxBot):
    """ 微信机器人助手 """

    _default_plugins: list[Plugin]
    _plugin_mappings: dict[str, list[Plugin]]
    """ 插件映射表. 键为联系人名称，值为插件列表 """

    def __init__(self, host: str, port: int, remote_storage_path: str, remote_server_prefix: str, dry_run: bool = False,
                 _default_plugin: Plugin | list[Plugin] = EndProcessingPlugin(), reply_delay_coefficient: float = 0):
        # TODO: 用 kwargs 传参，简化代码
        super().__init__(host, port, remote_storage_path=remote_storage_path, remote_server_prefix=remote_server_prefix,
                         dry_run=dry_run)

        assert isinstance(_default_plugin, Plugin) or isinstance(_default_plugin, list)
        assert reply_delay_coefficient >= 0

        self.set_default_plugin(_default_plugin)
        self._plugin_mappings = {}
        self._context = ContextManager()
        self.delay_coefficient = reply_delay_coefficient
        logger.info(f'Hello, {self.self_info["name"]}!')

    def set_default_plugin(self, plugin: Plugin | list[Plugin]):
        """ 设置默认插件 """
        if isinstance(plugin, Plugin):
            self._default_plugins = [plugin]
        else:
            self._default_plugins = plugin

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

        logger.info(f'{len(sessions)} sessions, {count_loaded} messages loaded.')

    @staticmethod
    def _parse_reference_message(msg: RawMessage) -> None:
        """ 解析引用消息. 注意。这个函数会修改原始消息对象 """

        assert msg.type == 49
        try:
            parsed_message = parse_reference_message(msg.content)
        except Exception as e:
            logger.error(f'Error on parsing reference message: {e}')
            raise e
        msg.content = parsed_message['content'] + f'引用了消息（{parsed_message['referred_message']}）'

    def _parse_image_message(self, msg: RawMessage) -> None:
        """ 解析图片消息 """
        try:
            accessible_url = self._fetch_image(msg)
        except Exception as e:
            logger.error(e)
            raise e

        image_content = requests.get(accessible_url).content
        assert image_content, '图片内容为空'

        chatglm_provider: ChatGLM = ChatGLM(key=CONFIG['chatglm-key'])
        description = chatglm_provider.describe_image(chatglm_provider.get_image_prompt(), image_content)
        description = description.replace('\n', '')
        logger.info(f'image description: {description}')
        msg.content = f'图片（文字描述：{description}）'

    def _special_message_hook(self, msg: RawMessage) -> None:
        """ 特殊消息处理 """
        match msg.type:
            case 49:  # 引用消息
                self._parse_reference_message(msg)
            case 3:  # 图片消息
                self._parse_image_message(msg)
            case _:
                pass

    @override
    def on_message(self, msg: RawMessage) -> None:
        """ 消息处理函数 """

        self._special_message_hook(msg)

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
        self_name = self.get_display_name(self.wxid, msg.roomid)

        # 消息来源。如果是群消息，则为群名称；否则为联系人备注
        name = lambda x: self.all_contacts.get(x, x)
        source = name(msg.roomid)
        logger.info(f'new message from {name(msg.roomid)}: {repr(msg.content)}')

        # 不回复表情
        if msg.type == 47:
            return

        # 消息处理
        response_back: list[str] | None = []
        for plugin in self._plugin_mappings.get(source, self._default_plugins):
            chat_context = self._context.get_context(msg.roomid)
            current_round, _continue = plugin.handle(msg, context=chat_context, self_name=self_name, contact=source)
            if not _continue:
                break
            if current_round:
                response_back.extend(current_round)

        if not response_back:
            logger.debug('no response to send.')
            return  # 没有回复内容

        # 回复消息
        # TODO: 改成异步发送
        for text in response_back:
            if self.delay_coefficient:
                time.sleep(len(text) * self.delay_coefficient)
            self.send_text_msg(text, msg.roomid)
            if msg.from_group():
                self._context.push_message(msg.roomid, text, self_name)
            else:
                self._context.push_message(msg.sender, text)


def main():
    from config import CONFIG

    host, port = CONFIG['wcf-host'], CONFIG['wcf-port']
    FERRY: WxHelper = WxHelper(host, port, remote_storage_path=CONFIG['remote-storage-path'],
                               remote_server_prefix=CONFIG['remote-server-prefix'], reply_delay_coefficient=0.2)

    # 捕获 Ctrl+C 信号
    def signal_handler(sig, frame):
        logger.warning('Ctrl+C pressed. Exit.')
        FERRY.stop_receiving_message()
        FERRY.cleanup()
        exit(0)

    signal.signal(signal.SIGINT, signal_handler)

    # 设置插件
    # ai_provider: ChatAI = ChatGLM(key=CONFIG['chatglm-key'])
    ai_provider: ChatAI = Deepseek(key=CONFIG['deepseek-key'], model='deepseek-reasoner', temperature=1.3)
    chat_plugin = ChatPlugin(ai_provider, max_ignore=2, frequency=10, context_length=10)
    repeater = RepeatPlugin()

    FERRY.set_default_plugin([repeater, chat_plugin])
    FERRY.attach_plugin('研究生摆烂群', [EndProcessingPlugin()])
    # FERRY.attach_plugin('sunnysab', [chat_plugin])

    FERRY.load_context()

    # 开始接收消息
    FERRY.start_receiving_message()
    while True:
        time.sleep(1)


if __name__ == '__main__':
    main()
