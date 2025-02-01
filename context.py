import re
from typing import Optional, List


class SingleRecord:
    """ 单条记录 """

    def __init__(self, sender: str, text: str, t: int):
        """ 维护单个会话中的记录.

        :param sender: 发送者的群昵称或者联系人昵称
        :param text: 消息内容（纯文本或针对特殊消息的描述）
        :param t: 时间戳（精确到秒，来源于微信消息中的 CreateTime 参数以及 WxMsg 的 ts 字段）
        """
        self.sender = sender
        self.text = text
        self.t = t

    def __str__(self):
        text = self.text.replace('\n', ' ')
        return f'{self.sender}: {text}'

    @staticmethod
    def clean(text: str) -> str:
        """ 去除消息中的表情和标点符号 """
        # 去掉中文标点
        text = re.sub(r'[，。！？；：、（）《》【】“”‘’—…]', '', text)
        # 去掉英文标点
        text = re.sub(r'[,.!?;:(){}"\'\[\]<>]', '', text)
        # 去掉表情（由英文方括号括起，中间是中英的文字）
        text = re.sub(r'\[[a-zA-Z]{2-9}\]', '', text)
        return text

    def pure_text(self):
        return self.clean(self.text)


class ChatWindow:
    """ 维护单个聊天会话（群或联系人）的状态 """

    def __init__(self, history: Optional[List[SingleRecord]]=None, max_history: int = 100):
        self.max_history = max_history

        history: List[SingleRecord] = history or []
        self._history = history[:max_history]

    def append(self, sender: str, text: str, t: int = 0):
        """ 添加消息

        :param sender: 发送者的昵称
        :param text: 消息内容
        :param t: 时间戳
        """
        if len(self._history) >= self.max_history:
            self._history.pop(0)
        self._history.append(SingleRecord(sender, text, t))

    def sort(self):
        """ 按照时间戳排序 """
        self._history.sort(key=lambda x: x.t)

    def extend(self, other: 'ChatWindow'):
        """ 扩展历史记录 """
        if len(self._history) + len(other._history) > self.max_history:
            self._history = self._history[-(self.max_history - len(other._history)):]
        else:
            self._history.extend(other._history)

    def empty(self):
        """ 是否为空 """
        return not self._history

    def latest_n(self, n: int):
        return ChatWindow(self._history[-n:]) if n < len(self._history) else self

    def __str__(self):
        return '\n'.join([str(x) for x in self._history])

    def __len__(self):
        return len(self._history)

    def __iter__(self):
        return iter(self._history)

    def __getitem__(self, item):
        return self._history[item]


class ContextManager:
    """ 上下文管理器 """

    def __init__(self):
        self._contexts = {}

    def get_context(self, key: str) -> ChatWindow:
        """ 获取上下文 """
        if key not in self._contexts:
            self._contexts[key] = ChatWindow()
        return self._contexts[key]

    def push_message(self, contact: str, sender: str, text: str, t: int):
        """ 存储消息

        :param contact: 联系人或群聊的 wxid
        :param sender: 发送者的群昵称或联系人昵称
        :param text: 消息内容（纯文本或针对特殊消息的描述）
        :param t: 时间戳（精确到秒，来源于微信消息中的 CreateTime 参数以及 WxMsg 的 ts 字段）
        """
        if contact in self._contexts:
            self._contexts[contact].append(sender, text, t)
        else:
            self._contexts[contact] = ChatWindow()
            self._contexts[contact].append(sender, text, t)
