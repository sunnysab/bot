from abc import abstractmethod
from wechat import RawMessage


class Plugin:
    @abstractmethod
    def handle(self, msg: RawMessage):
        return None

class WeatherPlugin(Plugin):
    def handle(self, msg):
        if "天气" in msg.content:
            return f"天气情况为：晴天"
        return None

class IdiomPlugin(Plugin):
    def handle(self, msg):
        if "成语" in msg.content:
            return f"成语解释：百闻不如一见"
        return None

class DefaultPlugin(Plugin):
    def handle(self, msg):
        return '你好'

