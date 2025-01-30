import time
from abc import abstractmethod

from chat import ChatAI
from wechat import RawMessage


class Plugin:
    @abstractmethod
    def handle(self, msg: RawMessage, **kwargs) -> tuple[str | None, bool]:
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
    def __init__(self, ai_implementation: ChatAI, max_ignore: int = 5, frequency: int = 10):
        self.ai = ai_implementation
        # 记录不同会话的最后一次调用模型时间, 以及模型从上次调用起忽略的消息数量
        self.last_check_time = {}
        self.max_ignore = max_ignore
        self.frequency = frequency # 每 f 秒调用一次模型

    @staticmethod
    def get_group_chat_prompt(self_name: str):
        """ 获取系统提示 """

        return f'''你现在位于一个群聊中聊天。请你发表回复，尽量简洁，符合语境和聊天人类习惯，结尾不要添加标点符号。
               注意话题的变更，无需回复旧的话题。
               且如果相关主题你曾经回复过，没有必要再回复，或你决定不说话，请说：本轮不发言。
               聊天记录包含了你和群友最近的发言。你叫 {{{self_name}}}，回答你要说的话即可，不要带上下文：\n'''

    @staticmethod
    def get_private_chat_prompt(self_name: str):
        """ 获取系统提示 """

        return f'''你在和朋友聊天。尽量简洁，符合语境和聊天人类习惯，口语化，结尾不要添加标点符号
                注意话题的变更，无需回复旧的话题
                且如果相关主题你曾经回复过，没有必要再回复，或你决定不说话，请说：本轮不发言
                聊天记录包含了你和对方最近的发言。你叫 {{{self_name}}}。回答你要说的话即可，不要带上下文：\n'''

    def handle(self, msg: RawMessage, **kwargs):
        # 检查是否需要调用模型
        now = time.time()
        if msg.roomid in self.last_check_time:
            last_check_time, ignored = self.last_check_time[msg.roomid]
            if now - last_check_time < self.frequency and ignored < self.max_ignore:
                self.last_check_time[msg.roomid] = last_check_time, ignored + 1
                return None, True
        self.last_check_time[msg.roomid] = now, 0

        me = kwargs['me']
        history = str(kwargs['context'])
        prompt = self.get_group_chat_prompt(me) if msg.from_group() else self.get_private_chat_prompt(me)
        response = self.ai.chat(history, prompt)
        if not response:
            return None, False

        # 有时候模型返回数据中会带有 "昵称: " 的前缀，这里需要去掉
        prefix = f'{me}: '
        if response.startswith(prefix):
            response = response[len(prefix):]

        return response, response is not None


class DefaultPlugin(Plugin):
    def handle(self, msg: RawMessage, **kwargs):
        return None, False
