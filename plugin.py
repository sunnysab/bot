import time
from abc import abstractmethod

from chat import ChatAI
from wechat import RawMessage


class Plugin:
    @abstractmethod
    def handle(self, msg: RawMessage, **kwargs) -> tuple[list[str] | None, bool]:
        """ 处理消息

        :param msg: 消息对象
        :return: 回复内容和一个布尔值。 如果阻止后续插件处理，则返回 False
        """
        return None, True


class WeatherPlugin(Plugin):
    def handle(self, msg: RawMessage, **kwargs):
        if "天气" in msg.content:
            return f"天气情况为：晴天"
        return None, True


class IdiomPlugin(Plugin):
    def handle(self, msg: RawMessage, **kwargs):
        if "成语" in msg.content:
            return f"成语解释：百闻不如一见"
        return None, True


class DoNothingPlugin(Plugin):
    def handle(self, msg: RawMessage, **kwargs):
        return None, False



class ChatPlugin(Plugin):
    def __init__(self, ai_provider: ChatAI, max_ignore: int = 5, frequency: int = 10, context_length: int = 10):
        """ 聊天插件

        :param ai_provider: AI 服务提供者
        :param max_ignore: 最大忽略消息数量
        :param frequency: 最小调用频率
        """
        self.ai = ai_provider
        # 记录不同会话的最后一次调用模型时间, 以及模型从上次调用起忽略的消息数量
        self.last_check_time = {}
        if max_ignore < 1:
            self.max_ignore = 1
        elif max_ignore > 50:
            self.max_ignore = 50
        else:
            self.max_ignore = max_ignore
        self.frequency = frequency # 最快情况每 frequency 秒调用一次模型
        self.context_length = context_length

        from jinja2 import Template
        template_file = open('prompt.txt', encoding='utf-8').read()
        self.prompt_template = Template(template_file)

    def handle(self, msg: RawMessage, **kwargs):
        # rate limit
        now = time.time()
        if msg.roomid in self.last_check_time:
            last_check_time, ignored = self.last_check_time[msg.roomid]
            if now - last_check_time < self.frequency or ignored < self.max_ignore:
                self.last_check_time[msg.roomid] = last_check_time, ignored + 1
                return None, True
        self.last_check_time[msg.roomid] = now, 0

        history = str(kwargs['context'].latest_n(self.context_length))
        prompt = self.prompt_template.render(self_name=kwargs['self_name'], contact=kwargs['contact'], is_group=msg.from_group())
        response = self.ai.chat(history, prompt.strip())
        if not response:
            return None, False

        # 有时候模型返回数据中会带有 "昵称: " 的前缀，这里需要去掉
        prefix = kwargs['self_name'] + ':'
        response = [x.lstrip(prefix) for x in response]

        return response, response is not None


class DefaultPlugin(Plugin):
    def handle(self, msg: RawMessage, **kwargs):
        return None, False
