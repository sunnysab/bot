from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from weixin_bot import Message

    from media import Media


class MessageKind(IntEnum):
    """ 微信消息的语义分类。数值来自 relay，是全仓唯一一处 msg_type 映射。 """

    OTHER = 0
    TEXT = 1
    IMAGE = 3
    VOICE = 34
    FRIEND_REQUEST = 37
    VIDEO = 43
    EMOJI = 47
    LOCATION = 48
    APPMSG = 49  # 链接 / 引用 / 文件 / 小程序
    HANDOFF = 51  # HandOffMaster 设备同步
    SYSTEM = 10000
    SYS_RECOMMEND = 10002  # 表情推荐等系统消息

    @classmethod
    def of(cls, msg_type: int) -> MessageKind:
        try:
            return cls(msg_type)
        except ValueError:
            return cls.OTHER


# 不应进入业务逻辑的消息类型（系统 / 同步类）
SYSTEM_KINDS = frozenset({MessageKind.FRIEND_REQUEST, MessageKind.HANDOFF,
                          MessageKind.SYSTEM, MessageKind.SYS_RECOMMEND})


def _format_time(t: int) -> str:
    """ 绝对时间，如 '09-14 14:32'。

    用绝对时间而非相对时间：历史消息的文本必须跨轮稳定，否则前缀会变，
    击穿 KV cache 的前缀复用，每轮都要重新计算全部 token。
    """
    return datetime.fromtimestamp(t).strftime('%m-%d %H:%M')


class SingleRecord:
    """ 单条记录 """

    def __init__(self, sender: str, text: str, t: int, media: Media | None = None,
                 download: Callable[[], Awaitable[bytes]] | None = None, is_self: bool = False):
        """ 维护单个会话中的记录.

        :param sender: 发送者的群昵称或者联系人昵称
        :param text: 消息内容（纯文本或针对特殊消息的描述）
        :param t: 时间戳（精确到秒，来源于微信消息中的 CreateTime 参数以及 WxMsg 的 ts 字段）
        :param media: 媒体描述（图片/表情），可为 None
        :param download: 媒体下载入口，供 describer 后台识图；描述就绪后原地回写 text
        :param is_self: 是否为机器人自己发的消息，transcript 里标记为「我」
        """
        self.sender = sender
        self.text = text
        self.t = t
        self.media = media
        self.download = download
        self.is_self = is_self

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

    def __init__(self, history: list[SingleRecord] | None=None, max_history: int = 100,
                 on_evict: Callable[[SingleRecord], None] | None = None):
        self.max_history = max_history
        self._on_evict = on_evict

        history: list[SingleRecord] = history or []
        self._history = history[:max_history]

    def append(self, sender: str, text: str, t: int = 0, media: Media | None = None,
               download: Callable[[], Awaitable[bytes]] | None = None, is_self: bool = False) -> SingleRecord:
        """ 添加消息

        :param sender: 发送者的昵称
        :param text: 消息内容
        :param t: 时间戳
        :param media: 图片/表情描述
        :param download: 媒体下载入口
        :param is_self: 是否为机器人自己发的消息
        """
        if len(self._history) >= self.max_history:
            evicted = self._history.pop(0)
            if self._on_evict:
                self._on_evict(evicted)  # 滑出窗口，交给中期记忆压缩
        record = SingleRecord(sender, text, t, media, download, is_self)
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
        """ 渲染给模型看的 transcript。

        每条带绝对时间（如 [09-14 14:32]）和「我」标记：模型据此判断消息时间、
        以及自己说过什么。历史行的文本跨轮不变，保住 KV cache 前缀。
        """
        lines = []
        for record in self._history:
            who = '我' if record.is_self else record.sender
            body = record.text.replace('\n', ' ')
            prefix = f'[{_format_time(record.t)}] ' if record.t else ''
            lines.append(f'{prefix}{who}: {body}')
        return '\n'.join(lines)

    def __len__(self):
        return len(self._history)

    def __iter__(self):
        return iter(self._history)

    def __getitem__(self, item):
        return self._history[item]


class ContextManager:
    """ 上下文管理器 """

    def __init__(self, on_evict: Callable[[str, SingleRecord], None] | None = None):
        """ :param on_evict: 消息滑出窗口时的回调，入参为（会话 key, 记录） """
        self._contexts = {}
        self._on_evict = on_evict

    def get_context(self, key: str) -> ChatWindow:
        """ 获取上下文 """
        if key not in self._contexts:
            callback = None
            if self._on_evict:
                callback = lambda record, key=key: self._on_evict(key, record)  # noqa: E731
            self._contexts[key] = ChatWindow(on_evict=callback)
        return self._contexts[key]

    def push_message(self, contact: str, sender: str, text: str, t: int, media: Media | None = None,
                     download: Callable[[], Awaitable[bytes]] | None = None,
                     is_self: bool = False) -> SingleRecord:
        """ 存储消息

        :param contact: 联系人或群聊的 wxid
        :param sender: 发送者的群昵称或联系人昵称
        :param text: 消息内容（纯文本或针对特殊消息的描述）
        :param t: 时间戳（精确到秒，来源于微信消息中的 CreateTime 参数以及 WxMsg 的 ts 字段）
        :param media: 图片/表情描述
        :param download: 媒体下载入口
        :param is_self: 是否为机器人自己发的消息
        :return: 新插入的记录对象，可后续原地更新（如媒体描述就绪后回写）
        """
        return self.get_context(contact).append(sender, text, t, media, download, is_self)


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
    assert MessageKind.of(34) is MessageKind.VOICE
    assert MessageKind.of(43) is MessageKind.VIDEO
    assert MessageKind.of(47) is MessageKind.EMOJI
    assert MessageKind.of(48) is MessageKind.LOCATION
    assert MessageKind.of(49) is MessageKind.APPMSG
    assert MessageKind.of(10000) is MessageKind.SYSTEM
    assert MessageKind.of(2) is MessageKind.OTHER
    assert MessageKind.of(-1) is MessageKind.OTHER
    assert MessageKind.HANDOFF in SYSTEM_KINDS

    # 自检：transcript 带绝对时间（跨轮稳定）和「我」标记
    window = ChatWindow()
    window.append('张三', '在吗', 1000)
    window.append('Bot', '在', 1060, is_self=True)
    window.append('张三', '帮我看看', 4600)
    lines = str(window).split('\n')
    assert lines[0].endswith('张三: 在吗'), lines
    assert lines[1].endswith('我: 在'), lines
    assert all(re.match(r'\[\d\d-\d\d \d\d:\d\d\] ', line) for line in lines), lines
    print('ok')
