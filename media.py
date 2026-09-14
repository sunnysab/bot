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

    kind: str  # 'image' | 'emoji'
    md5: str
    description: str | None = None

    @property
    def placeholder(self) -> str:
        """ 写进历史记录的占位文本。 """
        label = '图片' if self.kind == 'image' else '表情'
        return f'[{label}：{self.description}]' if self.description else f'[{label}]'

    @property
    def needs_description(self) -> bool:
        return not self.description and bool(self.md5)


PLACEHOLDER = re.compile(r'^\[(图片|表情|语音|视频|位置|链接|文件|合并转发|小程序)([：:][^\]]*)?\]$')


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
    m = re.search(rf'{name}\s*=\s*"(.*?)"', xml)
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


def parse_media(content: str) -> Media | None:
    """ 解析图片 / 表情消息的 XML 载荷。非媒体消息返回 None。 """
    if '<emoji' in content:
        return Media('emoji', attr(strip_prefix(content), 'md5'), parse_emoji_description(content))
    if '<img' in content:
        return Media('image', attr(strip_prefix(content), 'md5'))
    return None


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
    from context import MessageKind

    kind = MessageKind.of(msg_type)
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
        return '[视频]', None
    if kind is MessageKind.VOICE:
        return '[语音]', None
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

    # 媒体占位符不能被当成要说的话发出去
    assert is_placeholder('[表情：欢迎]') and is_placeholder('[图片]') and is_placeholder('[位置：海淀区]')
    assert is_placeholder('  [表情：欢迎]  ')
    assert not is_placeholder('欢迎')
    assert not is_placeholder('[表情：欢迎] 也太离谱了')     # 只是开头像，不当占位
    assert not is_placeholder('') and not is_placeholder('[不是占位符]')
    assert not is_placeholder('我[表情]了')
    print('ok')
