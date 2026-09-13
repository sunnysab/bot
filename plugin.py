import base64
from abc import ABC, abstractmethod
from typing import override

from loguru import logger

from ai import AiProvider
from context import ChatWindow, SingleRecord
from wechat import Message


class Plugin(ABC):
    @abstractmethod
    async def handle(self, msg: Message, **kwargs) -> tuple[list[str] | None, bool]:
        """ 处理消息

        :param msg: 消息对象
        :return: 回复内容和一个布尔值。 如果阻止后续插件处理，则返回 False
        """
        pass


class PluginManager:
    """Manages plugin registration and execution"""

    def __init__(self):
        self._defaults: list[Plugin] = []
        self._contact_mappings: dict[str, list[Plugin]] = {}

    def register(self, plugin: Plugin) -> None:
        """Register a plugin for all contacts"""
        self._defaults.append(plugin)
        logger.info(f'Registered plugin: {plugin.__class__.__name__}')

    def map_plugin_to_contact(self, contact: str, plugin: Plugin) -> None:
        """Map a specific plugin to a contact"""
        if contact not in self._contact_mappings:
            self._contact_mappings[contact] = []
        self._contact_mappings[contact].append(plugin)
        logger.info(f'Mapped plugin {plugin.__class__.__name__} to contact {contact}')

    def get_plugins_for_contact(self, contact: str) -> list[Plugin]:
        """Get plugins for a specific contact"""
        return self._contact_mappings.get(contact, self._defaults)


class EndProcessing(Plugin):
    @override
    async def handle(self, msg: Message, **kwargs):
        return None, False

class MessageTypeFilter(Plugin):
    @override
    async def handle(self, msg: Message, **kwargs):
        if msg.msg_type != 1 and msg.msg_type != 3 and msg.msg_type != 47:
            return None, False

class ImagePlugin(Plugin):
    @override
    async def handle(self, msg: Message, **kwargs):
        if msg.msg_type != 3:
            return None, True

class RepeatPlugin(Plugin):
    def __init__(self, repeat_count: int = 2, context_length: int = 10, max_length: int = 20):
        """ 跟队形（复读机）插件

        :param repeat_count: 队形阈值
        :param context_length: 拉取的上下文长度
        :param max_length: 单条队形的最大长度。 python 会计算中文字符长度，一个中文字符算一个长度。
        """
        assert repeat_count > 1
        assert context_length > repeat_count
        assert max_length > 0

        self.repeat_count = repeat_count
        self.context_length = context_length
        self.max_length = max_length
        # 为了避免重复回复，记录上一次重复的消息。
        # key 是聊天窗口编号，value 是最后一次重复的消息（去重）
        self.last_repeat = {}

    @override
    async def handle(self, msg: Message, **kwargs):
        """ 跟队形回复。 如果去掉中文英文标点及表情后的消息连续重复特定数量，则凑个热闹跟着回复一句。 """
        if msg.msg_type != 1:  # 只处理文本消息
            return None, True

        history: ChatWindow = kwargs['history']
        if history.empty():
            window = await kwargs['wechat'].fetch_history(msg.reply_target)
            history.extend(window)

        history: ChatWindow = history.latest_n(self.context_length)
        if len(history) < self.repeat_count:
            return None, True

        # 对历史消息去除昵称、标点等操作
        myself = kwargs['self_name']
        # 队形跟过了就不要再跟了
        blocked_set = set(map(lambda x: x.pure_text(), filter(lambda x: x.sender == myself, history)))
        clear_context: list[str] = [x.pure_text() for x in history if x.pure_text() and x.pure_text() not in blocked_set]
        count_dict = dict()
        for pos, text in enumerate(clear_context):
            if text not in count_dict:
                count_dict[text] = [pos]
            else:
                count_dict[text].append(pos)

        # 找到最多的重复元素及次数, 其实就是找 count_dict 中 value 最长的.
        max_repeat = max(count_dict.values(), key=len)
        if len(max_repeat) < self.repeat_count:
            return None, True

        # 找一条别人说过的队形，跟上
        record: SingleRecord = history[max_repeat[-1]]
        text: str = record.text
        # 太长不跟
        if len(text) > self.max_length:
            return None, True

        self.last_repeat[msg.reply_target] = text
        return [text], False


class ChatPlugin(Plugin):
    def __init__(self, ai_provider: AiProvider, context_length: int = 10):
        """ 聊天插件

        :param ai_provider: AI 服务提供者
        :param context_length: 送入模型的上下文消息条数
        """
        self.ai = ai_provider
        self.context_length = context_length

        from jinja2 import Template
        with open('prompt.txt', encoding='utf-8') as f:
            template_file = f.read()
        self.prompt_template = Template(template_file, enable_async=True)

    @override
    async def handle(self, msg: Message, **kwargs):
        if msg.msg_type not in (1, 3):  # 只处理文本和图片消息
            return None, True

        # 群聊只在被 @ 时回复；私聊始终回复
        if msg.is_chatroom and not msg.is_at_me:
            return None, True

        history: ChatWindow = kwargs['history']
        if history.empty():
            window = await kwargs['wechat'].fetch_history(msg.reply_target)
            history.extend(window)

        history: ChatWindow = history.latest_n(self.context_length)
        prompt = await self.prompt_template.render_async(self_name=kwargs['self_name'], contact=kwargs['contact'],
                                             is_group=msg.is_chatroom)

        text = '回复你扮演的角色所要说的话。以下聊天记录供你参考：\n\n' + str(history)
        response = await self.ai.chat(prompt.strip(), await self._build_message(msg, text))
        if not response:
            return None, False

        # 有时候模型返回数据中会带有 "昵称: " 的前缀，这里需要去掉
        prefix = kwargs['self_name'] + ':'
        response = [x.lstrip(prefix) for x in response]

        return response, response is not None

    @staticmethod
    async def _build_message(msg: Message, transcript: str) -> str | list:
        """ 文本消息直接发送；图片消息把真实图片作为 content block 附在文本后（DeepSeek 原生识图）。 """
        if msg.msg_type != 3:
            return transcript

        image = await msg.get_media_bytes()
        if not image:
            return transcript

        encoded = base64.b64encode(image).decode()
        return [
            {'type': 'text', 'text': transcript},
            {'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{encoded}'}},
        ]
