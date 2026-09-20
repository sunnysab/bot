""" 微信消息 XML 载荷解析：图片 / 表情 / 位置 / 引用 / 链接 / 文件 / 小程序。

表情消息（msg_type=47）的 desc 属性是 base64 编码的 protobuf，结构为 { locale: 描述 }，
实测能解出「摊倒」「啧啧」「哇」这类中文含义，无需调模型。
拿不到 desc 的表情（以及所有图片）只能靠 vision 描述，见 describer.py。
"""

import base64
import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Media:
    """ 一条媒体消息的可缓存表示。 """

    kind: str  # 'image' | 'emoji' | 'voice' | 'video'
    md5: str
    description: str | None = None
    # 源 XML 里有没有**明文可下载的 URL**。
    #
    # 为什么要先判这个：relay 的 `download_message` 只认 `cdnurl`/`cdnurlbigimg` 里的
    # 明文 URL。而入站图片的 XML 里只有 `cdnbigimgurl`/`cdnmidimgurl`
    # 那种**加密 fileid**（`305f0201...`）——实测 30 张图片 30 张没有 cdnurl。
    # 拿不到就别去试：每次尝试都是一次注定失败的 RPC + relay 一条 WARN 日志。
    downloadable: bool = False
    size: int = 0            # 字节数（图片/语音/视频都能从 XML 里读到）
    duration: int = 0        # 秒（语音/视频）

    @property
    def label(self) -> str:
        return KIND_LABEL.get(self.kind, self.kind)

    @property
    def placeholder(self) -> str:
        """ 写进历史 / 给模型看的占位文本。 """
        suffix = f' {self.duration}″' if self.duration else ''
        if self.description:
            return f'[{self.label}{suffix}：{self.description}]'
        return f'[{self.label}{suffix}]'

    @property
    def needs_description(self) -> bool:
        return not self.description and bool(self.md5)

    @property
    def describable(self) -> bool:
        """ 值不值得排一次识图：缺描述、且字节真拿得到。 """
        return self.needs_description and self.downloadable


PLACEHOLDER = re.compile(r'^\[(图片|表情|语音|视频|位置|链接|文件|合并转发|小程序)(\s*\d+\u2033)?([：: ][^\]]*)?\]$')

# 占位符里的标签。语音会带上时长（`[语音 3″]`）——模型知道时长比只知道「有语音」有用；
# 转写成功后写成 `[语音 3″：文本]`，所以时长后面还允许跟一个 `：…`。
# 发送前用 PLACEHOLDER 拦下这类文本，否则模型会把历史里的占位符当话说出去
KIND_LABEL = {'image': '图片', 'emoji': '表情', 'voice': '语音', 'video': '视频'}


def is_placeholder(text: str) -> bool:
    """ 整句就是个媒体占位符（`[表情：欢迎]`）？

    这种文本只应该出现在**历史**里。但模型看到历史会照抄，插件也可能把历史原文当队形
    发出来——真发出去群里看到的就是一串方括号。所以发送前统一拦一道。
    """
    return bool(PLACEHOLDER.match((text or '').strip()))


def strip_prefix(content: str) -> str:
    """ relay 有时在 XML 前拼一行 'wxid_xxx:'，截到第一个 '<'。 """
    start = content.find('<')
    return content[start:] if start > 0 else content


def attr(xml: str, name: str) -> str:
    # 左边卡一个边界：不卡的话 `length` 会先匹配上 `voicelength`，
    # `md5` 会匹配上 `androidmd5`
    m = re.search(rf'(?:^|[\s<]){name}\s*=\s*"(.*?)"', xml)
    return m.group(1) if m else ''


def tag(xml: str, name: str) -> str:
    """ 取第一个 <name>...</name> 的文本（appmsg 顶层字段在前，refermsg 在后）。 """
    m = re.search(rf'<{name}[^>]*>(.*?)</{name}>', xml, re.S)
    return m.group(1).strip() if m else ''


def _varint(buf: bytes, i: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        b = buf[i]
        i += 1
        value |= (b & 0x7F) << shift
        if not b & 0x80:
            return value, i
        shift += 7


def _pb_fields(buf: bytes) -> list[bytes]:
    """ 极简 protobuf：返回顶层所有 length-delimited 字段的原始字节。 """
    fields: list[bytes] = []
    i = 0
    while i < len(buf):
        tag_, i = _varint(buf, i)
        wire = tag_ & 7
        if wire == 2:
            length, i = _varint(buf, i)
            fields.append(buf[i:i + length])
            i += length
        elif wire == 0:
            _, i = _varint(buf, i)
        else:  # desc 里用不到其它 wire type
            break
    return fields


def parse_emoji_description(content: str) -> str | None:
    """ 取表情消息的中文描述，如 '摊倒'。取不到返回 None。 """
    if '<emoji' not in content:
        return None
    desc = attr(strip_prefix(content), 'desc')
    if not desc:
        return None
    try:
        raw = base64.b64decode(desc + '=' * (-len(desc) % 4))
        entries = _pb_fields(raw)
        key_values = _pb_fields(entries[0]) if entries else []
        text = key_values[1].decode('utf-8') if len(key_values) > 1 else ''
    except Exception:
        return None
    return text or None


# relay 的 `cdn::find_attr` 找的是**字面量** `cdnurl="`（等号两边不能有空格），
# 所以这里必须跟它**一样严格**，否则就是一次次注定失败的 RPC。
#
# 实测群里 19 条带 cdnurl 的表情，15 条写的是 `cdnurl = "http...`（另一种客户端的
# XML 风格），relay 看不见 → 报「无 cdnurl」。等 relay 把 find_attr 改成容忍空格之后，
# 这里也要跟着放宽（见 notes/未完成.md）。
# 表情/图片 XML 里的明文 http 直链。等号两边允许空白：客户端确实会写成 `cdnurl = "…"`，
# relay 的 `cdn::find_attr` 也得认这种写法（两边口径必须一致，否则识图会白排一次必失败的下载）
PLAIN_URL = re.compile(r'cdnurl\s*=\s*"https?://')


def parse_media(content: str) -> Media | None:
    """ 解析图片 / 表情消息的 XML 载荷。非媒体消息返回 None。 """
    if '<emoji' in content:
        xml = strip_prefix(content)
        return Media('emoji', attr(xml, 'md5'), parse_emoji_description(content),
                     downloadable=bool(PLAIN_URL.search(content)),
                     size=_int(attr(xml, 'length')))
    if '<img' in content:
        xml = strip_prefix(content)
        return Media('image', attr(xml, 'md5'),
                     downloadable=bool(PLAIN_URL.search(content)),
                     size=_int(attr(xml, 'length')))
    return None


def parse_voice(content: str) -> Media | None:
    """ 语音（msg_type=34）。字节拿不到（relay 不存），但**时长在 XML 里**，
    微信自己也只显示时长。 """
    if '<voicemsg' not in content:
        return None
    xml = strip_prefix(content)
    return Media('voice', '', duration=_seconds(attr(xml, 'voicelength')),
                 size=_int(attr(xml, 'length')))


def parse_video(content: str) -> Media | None:
    """ 视频（msg_type=43）。属性名在不同客户端上有差异，多试几个。

    playlength 实测就是**秒**（94 → 15MB，4 → 1.1MB），和语音的毫秒不一样。
    """
    if '<videomsg' not in content:
        return None
    xml = strip_prefix(content)
    for name in ('playlength', 'videolength'):
        seconds = _int(attr(xml, name))
        if seconds:
            return Media('video', '', duration=seconds, size=_int(attr(xml, 'length')))
    return Media('video', '')


def _int(value: str) -> int:
    try:
        return int(value or 0)
    except ValueError:
        return 0


def _seconds(value: str) -> int:
    """ voicelength 存的是**毫秒**（3000 = 3 秒）。视频的 playlength 是秒，别用这个。 """
    return max(1, round(_int(value) / 1000)) if _int(value) > 0 else 0


def parse_location(content: str) -> str:
    """ 位置消息 → '[位置：海淀区京新高速]'。 """
    xml = strip_prefix(content)
    label = attr(xml, 'label') or attr(xml, 'poiname') or attr(xml, 'poiCategoryTips')
    return f'[位置：{label}]' if label else '[位置]'


def parse_appmsg(content: str) -> str:
    """ appmsg（msg_type=49）→ 引用 / 链接 / 文件 / 小程序 的可读文本。

    历史上这里只处理引用(type=57)，遇到链接会把原始 XML 漏进历史，故统一收敛到此处。
    """
    from message import parse_reference_message  # message.py 是叶子模块，无循环

    xml = strip_prefix(content)
    appmsg_type = tag(xml, 'type')
    title = tag(xml, 'title')
    if appmsg_type == '57':
        try:
            parsed = parse_reference_message(content)
            return parsed['content'] + f'（引用前文的消息：{parsed["referred_message"]}）'
        except Exception:
            return '[引用消息]'
    if appmsg_type == '6':
        return f'[文件] {title}' if title else '[文件]'
    if appmsg_type in ('33', '36'):
        return f'[小程序] {title}' if title else '[小程序]'
    label = tag(xml, 'appname') or '链接'
    return f'[{label}] {title}' if title else '[分享消息]'


def render(msg_type: int, content: str, text: str) -> tuple[str, Media | None]:
    """ 把一条消息渲染成历史记录文本（+ 可选媒体描述）。

    统一 live 与 history 两条路径的载荷处理，避免一边丢弃一边漏 XML。
    """
    from context import SYSTEM_KINDS, MessageKind

    kind = MessageKind.of(msg_type)
    if kind in SYSTEM_KINDS:
        # 51 / 10000 / 10002 的正文是控制 XML（`<op id=…>`），不该漏给任何调用方
        return '[系统消息]', None
    if kind is MessageKind.TEXT:
        return text, None
    if kind is MessageKind.APPMSG:
        return parse_appmsg(content), None
    if kind in (MessageKind.IMAGE, MessageKind.EMOJI):
        media = parse_media(content)
        if media:
            return media.placeholder, media
        return ('[图片]' if kind is MessageKind.IMAGE else '[表情]'), None
    if kind is MessageKind.LOCATION:
        return parse_location(content), None
    if kind is MessageKind.VIDEO:
        media = parse_video(content)
        return (media.placeholder, media) if media else ('[视频]', None)
    if kind is MessageKind.VOICE:
        media = parse_voice(content)
        return (media.placeholder, media) if media else ('[语音]', None)
    return '', None


if __name__ == '__main__':
    # 自检：真实 relay 数据的 desc（含多语言条目），以及各种解析失败的兜底
    assert parse_emoji_description('<msg><emoji desc="ChEKB2RlZmF1bHQSBuWVp+WVpw==" /></msg>') == '啧啧'
    assert parse_emoji_description('<msg><emoji desc="ChEKB2RlZmF1bHQSBuaRiuWAkg==" /></msg>') == '摊倒'
    assert parse_emoji_description(
        '<msg><emoji desc="CgwKBXpoX2NuEgPlk4cKCQoFemhfdHcSAAoLCgdkZWZhdWx0EgA=" /></msg>') == '哇'
    assert parse_emoji_description('<msg><emoji desc="" /></msg>') is None
    assert parse_emoji_description('<msg><emoji /></msg>') is None
    assert parse_emoji_description('not xml') is None
    assert parse_emoji_description('<msg><img md5="abc" /></msg>') is None

    image = parse_media('wxid_x:\n<msg><img md5="f9bfba97" /></msg>')
    assert image == Media('image', 'f9bfba97') and image.placeholder == '[图片]', image
    assert image.needs_description
    emoji = parse_media('<msg><emoji md5="m" desc="ChEKB2RlZmF1bHQSBuaRiuWAkg==" /></msg>')
    assert emoji == Media('emoji', 'm', '摊倒') and emoji.placeholder == '[表情：摊倒]', emoji
    assert not emoji.needs_description
    blank = parse_media('<msg><emoji md5="m" desc="" /></msg>')
    assert blank.placeholder == '[表情]' and blank.needs_description, blank
    assert parse_media('hello world') is None
    assert parse_media('<msg><img /></msg>').needs_description is False

    # 「拿得到字节吗」得从 XML 就判出来：入站图片的 XML 里只有加密 fileid，
    # 没有明文 http cdnurl，硬试只会上演一次注定失败的 RPC
    assert image.downloadable is False and image.describable is False
    enc = parse_media('<msg><img md5="m" aeskey="k" cdnbigimgurl="305f020100044b" /></msg>')
    assert enc.downloadable is False, enc
    plain = parse_media('<msg><img md5="m" cdnurl="http://wxapp.tc.qq.com/x" /></msg>')
    assert plain.downloadable is True and plain.describable is True, plain
    # 带空格的写法 relay 现在也认，所以这里要认为能下（见 PLAIN_URL）
    spaced_dl = parse_media('<msg><img md5="m" cdnurl = "http://x" /></msg>')
    assert spaced_dl.downloadable is True, spaced_dl
    # 表情：有 cdnurl 的才值得排识图；带 desc 的本来就不用描述
    sticker = parse_media('<msg><emoji md5="m" cdnurl="http://x" /></msg>')
    assert sticker.downloadable is True and sticker.describable is True
    # 带空格的写法和 relay 的宽容口径对齐——两边不一致会白跑一次必失败的 RPC
    spaced = parse_media('<msg><emoji md5="m" cdnurl = "http://x" /></msg>')
    assert spaced.downloadable is True, spaced
    tagged = parse_media('<msg><emoji md5="m" desc="ChEKB2RlZmF1bHQSBuWVp+WVpw==" cdnurl="http://x" /></msg>')
    assert tagged.describable is False, '带标签的表情不需要识图'

    # 语音 / 视频：字节拿不到，但时长和大小在 XML 里；占位符要带上时长
    voice = parse_voice('<msg><voicemsg begin="0" end="1" voicelength="3000" length="1234" /></msg>')
    assert voice.kind == 'voice' and voice.duration == 3 and voice.size == 1234, voice
    assert voice.placeholder == '[语音 3″]', voice.placeholder
    assert voice.needs_description is False and voice.describable is False
    assert parse_voice('<msg><voicemsg voicelength="0" /></msg>').placeholder == '[语音]'
    assert parse_video('<msg><videomsg playlength="15" /></msg>').placeholder == '[视频 15″]'
    assert parse_video('<msg><videomsg /></msg>').placeholder == '[视频]'
    # 带时长的占位符也得被拦下来（这是发到群里的最后一道防线）
    assert is_placeholder('[语音 3″]') and is_placeholder('[视频 15″]')
    assert is_placeholder('[语音 3″：我的麦好像有点炸]')      # 转写过的那条也得拦住
    assert is_placeholder('[图片：一只橘猫]') and not is_placeholder('[语音 3″] 顺便说一句')
    assert render(34, '<msg><voicemsg voicelength="3000" /></msg>', 'x')[0] == '[语音 3″]'

    # 位置 / 视频 / 语音 / 链接 / 文件 / 引用
    assert parse_location('<msg><location label="海淀区京新高速" poiname="海淀·上郡" /></msg>') == '[位置：海淀区京新高速]'
    assert parse_location('<msg><location /></msg>') == '[位置]'
    assert parse_appmsg('<msg><appmsg><title>一篇文章</title><type>5</type><appname>某公众号</appname></appmsg></msg>') \
        == '[某公众号] 一篇文章'
    assert parse_appmsg('<msg><appmsg><title>报告.pdf</title><type>6</type></appmsg></msg>') == '[文件] 报告.pdf'
    assert parse_appmsg('<msg><appmsg><title>小程序名</title><type>33</type></appmsg></msg>') == '[小程序] 小程序名'
    quote = ('<msg><appmsg><title>回复内容</title><type>57</type><fromusername>wxid_me</fromusername>'
             '<refermsg><type>49</type><chatusr>wxid_alice</chatusr>'
             '<content>&lt;msg&gt;&lt;appmsg&gt;&lt;title&gt;被引用的内容&lt;/title&gt;'
             '&lt;/appmsg&gt;&lt;/msg&gt;</content></refermsg></appmsg></msg>')
    assert parse_appmsg(quote) == '回复内容（引用前文的消息：被引用的内容）', parse_appmsg(quote)
    assert render(34, '', '') == ('[语音]', None)
    assert render(43, '', '') == ('[视频]', None)
    assert render(1, 'ignored', '你好') == ('你好', None)
    assert render(2, 'x', 'x') == ('', None)
    # 会话列表的预览也走这里：51 的 <op> 控制 XML 不能漏出去（面板以前直接显示原文）
    assert render(51, "<msg><op id='11'><name>HandOffMaster</name></op></msg>",
                  "<msg><op id='11'><name>HandOffMaster</name></op></msg>") == ('[系统消息]', None)

    # 媒体占位符不能被当成要说的话发出去
    assert is_placeholder('[表情：欢迎]') and is_placeholder('[图片]') and is_placeholder('[位置：海淀区]')
    assert is_placeholder('  [表情：欢迎]  ')
    assert not is_placeholder('欢迎')
    assert not is_placeholder('[表情：欢迎] 也太离谱了')     # 只是开头像，不当占位
    assert not is_placeholder('') and not is_placeholder('[不是占位符]')
    assert not is_placeholder('我[表情]了')
    print('ok')
