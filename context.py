from __future__ import annotations

import hashlib
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
    """ 微信消息的语义分类。数值来自 daemon，是全仓唯一一处 msg_type 映射。 """

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


def is_system_notice(msg: Message) -> bool:
    """ 这条是不是系统通知（拍一拍、设备同步、好友请求……）。

    与「能不能回」无关：拍一拍是社交信号，照常回应。它只回答「能不能引用」——
    微信的引用框会把原话贴出来，引用一条系统通知就成了对着系统提示说话。
    """
    return getattr(msg, 'pat', None) is not None or MessageKind.of(msg.msg_type) in SYSTEM_KINDS


def format_time(t: int) -> str:
    """ 绝对时间，如 '09-14 14:32:07'。

    用绝对时间而非相对时间：历史消息的文本必须跨轮稳定，否则前缀会变，
    击穿 KV cache 的前缀复用，每轮都要重新计算全部 token。
    带秒：同一分钟里谁先谁后模型能排出来，「刚刚」「刚才」才有依据。
    """
    return datetime.fromtimestamp(t).strftime('%m-%d %H:%M:%S')


def format_minute(t: int) -> str:
    """ 分钟粒度的绝对时间，如 '09-14 14:32'。中期记忆摘要用——秒是噪声。 """
    return datetime.fromtimestamp(t).strftime('%m-%d %H:%M')


# 聊天记录里「新消息从这里开始」的分隔线。模型没有「未读」这个概念，靠它才知道边界
NEW_MESSAGES = '----以下是新消息----'

_HANDLE_DIGITS = '0123456789abcdefghijklmnopqrstuvwxyz'
_HANDLE_LENGTH = 6


def message_handle(svrid: int) -> str:
    """ 一条消息给模型看的短标签，如 `#4k2p0z`。

    取 svrid 的 sha1 摘要（前 8 字节）的末 6 位 36 进制，**不是 svrid 本身的低位**：
    低位可能是连续计数器，相邻消息会拿到相邻标签，撞车就变成结构性问题而不是概率问题。
    6 位 base36 ≈ 31 bit，一个 60 条的窗口里几乎撞不了；所以它够格当引用键——
    `（引用 #xxxx）` 的注解、模型回复时的 `ref` 参数都靠它。
    """
    if not svrid:
        return ''
    n = int.from_bytes(hashlib.sha1(str(svrid).encode()).digest()[:8], 'big')
    out = ''
    for _ in range(_HANDLE_LENGTH):
        n, rest = divmod(n, 36)
        out = _HANDLE_DIGITS[rest] + out
    return '#' + out


class SingleRecord:
    """ 单条记录 """

    def __init__(self, sender: str, text: str, t: int, media: Media | None = None,
                 download: Callable[[], Awaitable[bytes]] | None = None, is_self: bool = False,
                 msg_id: int = 0, media_id: int = 0, handle: str = ''):
        """ 维护单个会话中的记录.

        :param sender: 发送者的群昵称或者联系人昵称
        :param text: 消息内容（纯文本或针对特殊消息的描述）
        :param t: 时间戳（精确到秒，来源于微信消息中的 CreateTime 参数以及 WxMsg 的 ts 字段）
        :param media: 媒体描述（图片/表情），可为 None
        :param download: 媒体下载入口，供 describer 后台识图、以及内联给模型；描述就绪后原地回写 text
        :param is_self: 是否为机器人自己发的消息，transcript 里标记为「我」
        :param msg_id: 这条消息在 daemon 里的 64 位 svrid
        :param media_id: 要内联给模型的媒体的 svrid（自己有图=msg_id，引用图=ref_svrid），0=没有
        :param handle: 媒体在 transcript 里的短标签（`#4k2p0z`），没有可内联媒体时为空
        """
        self.sender = sender
        self.text = text
        self.t = t
        self.media = media
        self.download = download
        self.is_self = is_self
        self.msg_id = msg_id
        self.media_id = media_id
        self.handle = handle

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
        # 按 svrid 去重：daemon 重连重放、启动加载和实时推送叠在一起，都会把同一条消息塞两遍。
        # 0 = 没有身份（自己刚发出去、测试造的），不去重
        self._by_id: dict[int, SingleRecord] = {record.msg_id: record
                                                for record in self._history if record.msg_id}

    def _insert(self, record: SingleRecord) -> SingleRecord:
        """ 把一条记录放进窗口；同 svrid 已经在里面就原样返回旧的。 """
        if record.msg_id and record.msg_id in self._by_id:
            return self._by_id[record.msg_id]
        if len(self._history) >= self.max_history:
            evicted = self._history.pop(0)
            self._by_id.pop(evicted.msg_id, None)
            if self._on_evict:
                self._on_evict(evicted)  # 滑出窗口，交给中期记忆压缩
        self._history.append(record)
        if record.msg_id:
            self._by_id[record.msg_id] = record
        return record

    def append(self, sender: str, text: str, t: int = 0, media: Media | None = None,
               download: Callable[[], Awaitable[bytes]] | None = None, is_self: bool = False,
               msg_id: int = 0, media_id: int = 0, handle: str = '') -> SingleRecord:
        """ 添加消息

        :param sender: 发送者的昵称
        :param text: 消息内容
        :param t: 时间戳
        :param media: 图片/表情描述
        :param download: 媒体下载入口
        :param is_self: 是否为机器人自己发的消息
        :param msg_id: 这条消息的 daemon svrid
        :param media_id: 要内联给模型的媒体的 svrid（见 SingleRecord）
        :param handle: 媒体在 transcript 里的短标签
        """
        record = SingleRecord(sender, text, t, media, download, is_self, msg_id, media_id, handle)
        return self._insert(record)

    def forget(self, record: SingleRecord) -> bool:
        """ 抹掉一条记录（撤回自己说过的话用）。

        按**对象身份**找，不按内容——同一句话可能说过两遍，按内容会删错那条。
        返回是否真的删掉了。
        """
        for index, item in enumerate(self._history):
            if item is record:
                del self._history[index]
                self._by_id.pop(item.msg_id, None)
                return True
        return False

    def sort(self):
        """ 按照时间戳排序 """
        self._history.sort(key=lambda x: x.t)

    def extend(self, other: ChatWindow):
        """ 扩展历史记录。同 svrid 的跳过——重放 / 重复加载都不能让一行出现两次。 """
        for record in other._history:
            self._insert(record)
        self.sort()

    def latest_n(self, n: int) -> ChatWindow:
        """ 返回最近 n 条的副本，避免插件直接改到真实历史。 """
        if n >= len(self._history):
            return ChatWindow(list(self._history), max_history=self.max_history)
        return ChatWindow(self._history[-n:], max_history=n)

    def __str__(self):
        return self.transcript()

    def transcript(self, fresh: tuple[int, ...] = ()) -> str:
        """ 渲染给模型看的聊天记录。

        每条带绝对时间（如 [09-14 14:32:07]）、消息短标签（`#4k2p0z`，回复时用它指定引用哪条）
        和「我」标记：模型据此判断消息时间、以及自己说过什么。历史行的文本跨轮不变，
        保住 KV cache 前缀。

        `fresh` 是这一轮要处理的几条消息的 msg_id（按时间序）：它们前面画一条分隔线。
        模型没有「未读」这个概念，不给边界它就会重新捡起已经晾过的话题。
        """
        first = fresh[0] if fresh else 0
        lines = []
        for record in self._history:
            if first and record.msg_id == first:
                lines.append(NEW_MESSAGES)
            who = '我' if record.is_self else record.sender
            body = record.text.replace('\n', ' ')
            prefix = f'[{format_time(record.t)}] ' if record.t else ''
            tag = f'{message_handle(record.msg_id)} ' if record.msg_id else ''
            lines.append(f'{prefix}{tag}{who}: {body}')
        return '\n'.join(lines)

    def ref_candidates(self, limit: int = 4) -> str:
        """ 给模型看的「ref 能填哪几个」候选，新→旧。

        整段记录里每个 #标签都摆着，模型挑错是常事——尤其最后一条是表情/图片时，
        容易引着那个标签去讲上一个话题。只列最近几条，把 ref 变成「从这几个里挑一个」。
        """
        recent = [record for record in self._history if record.msg_id][-limit:]
        if not recent:
            return '  （这一轮没有能引用的消息，ref 别填）'
        lines = []
        for record in reversed(recent):
            who = '我' if record.is_self else record.sender
            lines.append(f'  · {message_handle(record.msg_id)} {who}: '
                         f"{record.text.replace(chr(10), ' ')[:24]}")
        return '\n'.join(lines)

    def find_by_handle(self, handle: str) -> SingleRecord | None:
        """ 按 transcript 里的短标签（`#4k2p0z`）找一条记录，用于「引用哪条」。

        找不到、或标签撞了（同一窗口多条命中）都返回 None——引用错了比不引用更糟。
        """
        target = handle.strip().lower().lstrip('#')
        if not target:
            return None
        hits = [record for record in self._history
                if record.msg_id and message_handle(record.msg_id)[1:] == target]
        return hits[0] if len(hits) == 1 else None

    def shows(self, svrid: int, visible: int = 0) -> bool:
        """ 这条 svrid 会出现在模型这一轮看到的 transcript 里吗。

        transcript 是 `latest_n(visible)`，且当前这条还没进窗口，所以被引的那条得落在
        现有记录的最后 `visible - 1` 条里，写成 `（引用 #xxxx）` 模型才找得到它。
        `visible <= 0` = 不设限（整个窗口）。
        """
        if not svrid:
            return False
        tail = self._history if visible <= 0 else self._history[-(visible - 1):]
        return any(record.msg_id == svrid for record in tail)

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
                     is_self: bool = False, msg_id: int = 0, media_id: int = 0,
                     handle: str = '') -> SingleRecord:
        """ 存储消息

        :param contact: 联系人或群聊的 wxid
        :param sender: 发送者的群昵称或联系人昵称
        :param text: 消息内容（纯文本或针对特殊消息的描述）
        :param t: 时间戳（精确到秒，来源于微信消息中的 CreateTime 参数以及 WxMsg 的 ts 字段）
        :param media: 图片/表情描述
        :param download: 媒体下载入口
        :param is_self: 是否为机器人自己发的消息
        :param msg_id: 这条消息的 daemon svrid
        :param media_id: 要内联给模型的媒体的 svrid（见 SingleRecord）
        :param handle: 媒体在 transcript 里的短标签
        :return: 新插入的记录对象，可后续原地更新（如媒体描述就绪后回写）
        """
        return self.get_context(contact).append(sender, text, t, media, download, is_self,
                                                msg_id, media_id, handle)

    def sizes(self) -> dict[str, int]:
        """ 每个会话当前窗口里有多少条。面板要显示「模型此刻能看到多少」。 """
        return {key: len(window) for key, window in self._contexts.items()}


@dataclass(slots=True)
class MessageContext:
    """ 插件处理一条消息时所需的全部输入。

    history 的生命周期（加载 / 写入）由 Bot 负责，插件只读；插件不直接接触传输层。
    """

    msg: Message
    history: ChatWindow
    self_name: str
    contact: str
    # 管理后台「手动回复这条」：跳过门控 / 退避 / 频率闸，而且必须开口。
    # 人已经替你决定了要说，这里再拦一次就是把人的操作吞掉
    forced: bool = False
    # 这一轮要处理的**新消息**（同一个块里的那几条，按时间序）。单个消息到达时就是
    # 只有 msg 一条。门控按整块判、聊天记录里从第一条开始画分隔线都靠它
    fresh: tuple[Message, ...] = ()
    # 后台「试回一次」：链路照跑，但不留痕——不写回复轨迹，也不发面板状态
    replay: bool = False

    @property
    def kind(self) -> MessageKind:
        return MessageKind.of(self.msg.msg_type)

    @property
    def is_text(self) -> bool:
        return self.kind is MessageKind.TEXT


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
    window.append('张三', '在吗', 1000, msg_id=111)
    window.append('Bot', '在', 1060, is_self=True)
    window.append('张三', '帮我看看', 4600)
    lines = str(window).split('\n')
    assert lines[0] == f'[01-01 08:16:40] {message_handle(111)} 张三: 在吗', lines
    assert lines[1].endswith('我: 在'), lines                      # 自己那条没有 svrid，不打标签
    assert all(re.match(r'\[\d\d-\d\d \d\d:\d\d:\d\d\] ', line) for line in lines), lines
    # 新消息分隔线：只在这一轮要处理的那几条之前画一条；旧消息（重放 / 手动回复）一条不画
    assert NEW_MESSAGES not in str(window)
    marked = ChatWindow()
    marked.append('张三', '旧的', 1000, msg_id=111)
    marked.append('Bot', '我回过了', 1060, is_self=True)
    marked.append('张三', '新的一句', 4600, msg_id=222)
    assert NEW_MESSAGES not in str(marked), str(marked)          # 不传 fresh = 全是旧消息
    fresh = marked.transcript(fresh=(222,)).split('\n')
    assert fresh[-2] == NEW_MESSAGES and fresh[-1].endswith('张三: 新的一句'), fresh
    # 块里有几条就从第一条之前画；msg_id=0（没有身份）画不了线，不该乱插
    assert marked.transcript(fresh=(111, 222)).split('\n')[0] == NEW_MESSAGES
    assert NEW_MESSAGES not in marked.transcript(fresh=(0,))
    # 引用哪条：按标签找回原消息；撞了 / 找不到就返回 None
    assert window.find_by_handle(message_handle(111)).text == '在吗'
    assert window.find_by_handle('#nope') is None and window.find_by_handle('') is None
    # `（引用 #xxxx）` 只在被引那条还落在可见范围里时才给。窗口里 3 条，
    # 模型看到 4 条（含当前这条）时 111 还在，只看到 3 条时就滑出去了
    assert window.shows(111, 4) and window.shows(111) and not window.shows(111, 3)
    assert not window.shows(0) and not window.shows(999)

    # 短标签：6 位、稳定、0 是「没有」
    assert message_handle(0) == '' and message_handle(5677413158710008087).startswith('#')
    assert len(message_handle(5677413158710008087)) == 7
    assert message_handle(1) != message_handle(2)
    assert message_handle(5677413158710008087) == message_handle(5677413158710008087)

    # 撤回：按对象身份删，删完 transcript 里就没有了
    kept = window[len(window) - 1]
    assert window.forget(window[1]) is True
    assert '我: 在' not in str(window)
    assert window.forget(kept) is True and len(window) == 1
    assert window.forget(kept) is False          # 删过的不再删

    # 同一条消息塞两遍（daemon 重放 / 启动重复加载）只算一条，重放的那次把原记录还回去
    once = ChatWindow()
    first = once.append('张三', '在吗', 1000, msg_id=111)
    assert once.append('张三', '在吗', 1000, msg_id=111) is first
    assert len(once) == 1
    # extend 同样按 svrid 去重；没有 svrid 的消息（自己发的、测试造的）不参与
    other = ChatWindow()
    other.append('张三', '在吗', 1000, msg_id=111)
    other.append('张三', '又来了', 1000, msg_id=222)
    other.append('我', '在', 1001, is_self=True)
    once.extend(other)
    assert len(once) == 3, [record.text for record in once]
    assert str(once).count('在吗') == 1
    print('ok')
