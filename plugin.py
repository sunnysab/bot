import re
import time
from abc import abstractmethod

from chat import ChatAI
from context import ChatWindow
from wechat import RawMessage


class Plugin:
    @abstractmethod
    def handle(self, msg: RawMessage, **kwargs) -> tuple[list[str] | None, bool]:
        """ 处理消息

        :param msg: 消息对象
        :return: 回复内容和一个布尔值。 如果阻止后续插件处理，则返回 False
        """
        return None, True


class EndProcessingPlugin(Plugin):
    def handle(self, msg: RawMessage, **kwargs):
        return None, False


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

    @staticmethod
    def preprocess(text: str) -> str:
        """ 预处理消息. 此时消息格式为：'昵称: 消息内容' """
        # 去掉昵称
        text = text.split(':', 1)[1]
        # 去掉中文标点
        text = re.sub(r'[，。！？；：、（）《》【】“”‘’—…]', '', text)
        # 去掉英文标点
        text = re.sub(r'[,.!?;:(){}"\'\[\]<>]', '', text)
        # 去掉表情（由英文方括号括起，中间是中英的文字）
        text = re.sub(r'[.*?]', '', text)
        return text

    def handle(self, msg: RawMessage, **kwargs):
        """ 跟队形回复。 如果去掉中文英文标点及表情后的消息连续重复特定数量，则凑个热闹跟着回复一句。 """
        if msg.type != 1:  # 只处理文本消息
            return None, True

        context: ChatWindow = kwargs['context'].latest_n(self.context_length)
        if len(context) < self.repeat_count:
            return None, True

        # 对历史消息去除昵称、标点等操作
        clear_context: list[str] = [self.preprocess(x) for x in context]
        count_dict = {}
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
        text = context[max_repeat[-1]]
        text = text.split(':', 1)[1]
        # 太长不跟
        if len(text) > self.max_length:
            return None, True
        # 队形跟过了就不要再跟了
        if msg.roomid in self.last_repeat and self.last_repeat[msg.roomid] == text:
            return None, True
        self.last_repeat[msg.roomid] = text
        return [text], True


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
        self.frequency = frequency  # 最快情况每 frequency 秒调用一次模型
        self.context_length = context_length

        from jinja2 import Template
        template_file = open('prompt.txt', encoding='utf-8').read()
        self.prompt_template = Template(template_file)

    def handle(self, msg: RawMessage, **kwargs):
        if msg.type != 1:  # 只处理文本消息
            return None, True

        # rate limit
        now = time.time()
        if msg.roomid in self.last_check_time:
            last_check_time, ignored = self.last_check_time[msg.roomid]
            if now - last_check_time < self.frequency or ignored < self.max_ignore:
                self.last_check_time[msg.roomid] = last_check_time, ignored + 1
                return None, True
        self.last_check_time[msg.roomid] = now, 0

        history = str(kwargs['context'].latest_n(self.context_length))
        prompt = self.prompt_template.render(self_name=kwargs['self_name'], contact=kwargs['contact'],
                                             is_group=msg.from_group())
        response = self.ai.chat(prompt.strip(), history)
        if not response:
            return None, False

        # 有时候模型返回数据中会带有 "昵称: " 的前缀，这里需要去掉
        prefix = kwargs['self_name'] + ':'
        response = [x.lstrip(prefix) for x in response]

        return response, response is not None
