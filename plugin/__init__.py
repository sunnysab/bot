from abc import abstractmethod
from wechat import RawMessage


class Plugin:
    @abstractmethod
    def handle(self, msg: RawMessage) -> tuple[str | None, bool]:
        """ 处理消息

        :param msg: 消息对象
        :return: 回复内容和一个布尔值。 如果阻止后续插件处理，则返回 False
        """
        return None, True

class WeatherPlugin(Plugin):
    def handle(self, msg):
        if "天气" in msg.content:
            return f"天气情况为：晴天"
        return None, True

class IdiomPlugin(Plugin):
    def handle(self, msg):
        if "成语" in msg.content:
            return f"成语解释：百闻不如一见"
        return None, True

class DoNothingPlugin(Plugin):
    def handle(self, msg):
        return None, False

class DefaultPlugin(Plugin):
    def handle(self, msg):
        return None, False

