from __future__ import annotations

import re
from dataclasses import dataclass
from enum import IntEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from weixin_bot import Message


class MessageKind(IntEnum):
    """ 微信消息的语义分类。数值来自 relay，是全仓唯一一处 msg_type 映射。 """

    OTHER = 0
    TEXT = 1
    IMAGE = 3
    FRIEND_REQUEST = 37
    EMOJI = 47
    QUOTE = 49
    SYSTEM = 10000

    @classmethod
    def of(cls, msg_type: int) -> MessageKind:
        try:
            return cls(msg_type)
        except ValueError:
            return cls.OTHER


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
        # 去掉 [图片]/[表情] 这类媒体占位标记，否则复读机会把它们当成正文跟风
        text = re.sub(r'\[[^\]]*\]', '', text)
        # 去掉中文标点
        text = re.sub(r'[，。！？；：、（）《》【】“”‘’—…]', '', text)
        # 去掉英文标点
        text = re.sub(r'[,.!?;:(){}"\'\[\]<>]', '', text)
        return text

    def pure_text(self):
        return self.clean(self.text)


class ChatWindow:
    """ 维护单个聊天会话（群或联系人）的状态 """

    def __init__(self, history: list[SingleRecord] | None=None, max_history: int = 100):
        self.max_history = max_history

        history: list[SingleRecord] = history or []
        self._history = history[:max_history]

    def append(self, sender: str, text: str, t: int = 0) -> SingleRecord:
        """ 添加消息

        :param sender: 发送者的昵称
        :param text: 消息内容
        :param t: 时间戳
        """
        if len(self._history) >= self.max_history:
            self._history.pop(0)
        record = SingleRecord(sender, text, t)
        self._history.append(record)
        return record

    def sort(self):
        """ 按照时间戳排序 """
        self._history.sort(key=lambda x: x.t)

    def extend(self, other: ChatWindow):
        """ 扩展历史记录 """
        if len(self._history) + len(other._history) > self.max_history:
            self._history = self._history[-(self.max_history - len(other._history)):]
        else:
            self._history.extend(other._history)
        self.sort()

    def empty(self):
        """ 是否为空 """
        return not self._history

    def latest_n(self, n: int) -> ChatWindow:
        """ 返回最近 n 条的副本，避免插件直接改到真实历史。 """
        if n >= len(self._history):
            return ChatWindow(list(self._history), max_history=self.max_history)
        return ChatWindow(self._history[-n:], max_history=n)

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

    def push_message(self, contact: str, sender: str, text: str, t: int) -> SingleRecord:
        """ 存储消息

        :param contact: 联系人或群聊的 wxid
        :param sender: 发送者的群昵称或联系人昵称
        :param text: 消息内容（纯文本或针对特殊消息的描述）
        :param t: 时间戳（精确到秒，来源于微信消息中的 CreateTime 参数以及 WxMsg 的 ts 字段）
        :return: 新插入的记录对象，可后续原地更新（如媒体描述就绪后回写）
        """
        return self.get_context(contact).append(sender, text, t)


@dataclass(slots=True)
class MessageContext:
    """ 插件处理一条消息时所需的全部输入。

    history 的生命周期（加载 / 写入）由 Bot 负责，插件只读；插件不直接接触传输层。
    """

    msg: Message
    history: ChatWindow
    self_name: str
    contact: str

    @property
    def kind(self) -> MessageKind:
        return MessageKind.of(self.msg.msg_type)

    @property
    def is_text(self) -> bool:
        return self.kind is MessageKind.TEXT

    @property
    def is_image(self) -> bool:
        return self.kind is MessageKind.IMAGE


if __name__ == '__main__':
    # 自检：已知类型映射到语义，未知类型落到 OTHER
    assert MessageKind.of(1) is MessageKind.TEXT
    assert MessageKind.of(3) is MessageKind.IMAGE
    assert MessageKind.of(47) is MessageKind.EMOJI
    assert MessageKind.of(49) is MessageKind.QUOTE
    assert MessageKind.of(10000) is MessageKind.SYSTEM
    assert MessageKind.of(2) is MessageKind.OTHER
    assert MessageKind.of(-1) is MessageKind.OTHER
    print('ok')
