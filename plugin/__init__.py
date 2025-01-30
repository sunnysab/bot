from abc import abstractmethod

from chat import ChatAI
from context import ChatWindow
from wechat import RawMessage


class Plugin:
    @abstractmethod
    def handle(self, msg: RawMessage, context: ChatWindow=None) -> tuple[str | None, bool]:
        """ 处理消息

        :param context: 上下文窗口
        :param msg: 消息对象
        :return: 回复内容和一个布尔值。 如果阻止后续插件处理，则返回 False
        """
        return None, True


class WeatherPlugin(Plugin):
    def handle(self, msg: RawMessage, _: ChatWindow=None):
        if "天气" in msg.content:
            return f"天气情况为：晴天"
        return None, True


class IdiomPlugin(Plugin):
    def handle(self, msg: RawMessage, _: ChatWindow=None):
        if "成语" in msg.content:
            return f"成语解释：百闻不如一见"
        return None, True


class DoNothingPlugin(Plugin):
    def handle(self, msg: RawMessage, _: ChatWindow=None):
        return None, False


class ChatPlugin(Plugin):
    def __init__(self, ai_implementation: ChatAI):
        self.ai = ai_implementation

    def handle(self, msg: RawMessage, context: ChatWindow=None):
        history = str(context)
        response = self.ai.chat(history)

        return response, response is not None


class DefaultPlugin(Plugin):
    def handle(self, msg: RawMessage, context: ChatWindow=None):
        return None, False
