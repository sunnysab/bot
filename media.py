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
        """ 值不值得排一次识图：缺描述、而且是 vision 看得见的东西。

        这里**不再看 XML 里有没有明文 cdnurl**。以前那是对的：daemon 只下 `cdnurl`
        里的明文 URL，而入站图片的 XML 里只有加密 fileid（实测 30/30 没有 cdnurl），
        硬试就是一次注定失败的 RPC。现在 daemon 按 msg_id 走 CDN 帧下载
        （`download_media_cdn`），加密 fileid 一样下得来——再卡这一条，结果就是
        **所有图片永远没描述**（media_cache.db 里 56 条全是表情，零图片）。
        视频/语音不送 vision：它们不是「画面里有什么」的问题。
        """
        return self.kind in ('image', 'emoji') and self.needs_description


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
    """ daemon 有时在 XML 前拼一行 'wxid_xxx:'，截到第一个 '<'。 """
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


def parse_media(content: str) -> Media | None:
    """ 解析图片 / 表情消息的 XML 载荷（含 md5 / 描述 / 大小），非媒体消息返回 None。 """
    if '<emoji' in content:
        xml = strip_prefix(content)
        return Media('emoji', attr(xml, 'md5'), parse_emoji_description(content),
                     size=_int(attr(xml, 'length')))
    if '<img' in content:
        xml = strip_prefix(content)
        return Media('image', attr(xml, 'md5'), size=_int(attr(xml, 'length')))
    return None


def parse_voice(content: str) -> Media | None:
    """ 语音（msg_type=34）。字节拿不到（daemon 不存），但**时长在 XML 里**，
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


def strip_sender(text: str) -> str:
    """ 去掉 daemon 在正文前拼的那行发送者 wxid（`wxid_xxx:`）。

    只认 `wxid_` 开头：引用里的正文可能是任何一句话，不能按「冒号前」一刀切
    （「12:30 开会」这种会被误伤）。
    """
    return re.sub(r'^wxid_\w+:\s*', '', text or '')


def add_handle(text: str, handle: str) -> str:
    """ 把短标签塞进媒体占位符：`[图片]` → `[图片 #4k2p]`。

    只认图片/表情——只有这两种会被内联给模型，别的占位符没标签可对。
    """
    if not handle:
        return text
    return re.sub(r'^(\[(?:图片|表情))', rf'\1 {handle}', text, count=1)


def parse_reference_media(content: str) -> Media | None:
    """ 引用消息里被引的那条是不是图片/表情？是就返回它的 Media。

    `render_referred` 已经把被引内容降级成 `[图片]`，md5 丢了；要「把引用的图也内联给
    模型」就得单独把 md5 捞回来。解析失败就当没有——引用退化成文字，不影响别的。
    """
    from message import parse_reference_message
    try:
        parsed = parse_reference_message(strip_prefix(content))
    except Exception:  # noqa: BLE001 —— XML 不规整是常事
        return None
    if (parsed.get('referred_message_type') or 0) not in (3, 47):  # 图片 / 表情
        return None
    raw = strip_sender(parsed.get('referred_message') or '')
    return parse_media(raw) if raw else None


def render_referred(parsed: dict, handle: str = '') -> str:
    """ 把被引的那条渲染成人话（供 `parse_appmsg` 和后台的引用块共用）。

    被引的那条**不一定是文字**：多数时候是图片、表情、链接，甚至又一串引用。
    以前直接把它当文本用，于是图片/表情的引用要么报错（没有 `<title>`），
    要么把一段 XML 原样塞给模型。这里统一走 `render`：图片→`[图片]`、
    表情→`[表情：摊倒]`、链接→`[链接] 标题`，嵌套引用会再走一层。

    ponytail: 嵌套引用逐层递归，深度就是「引用链」的长度（微信不会自己引用自己）。
    真要防可以把 `render` 加上 depth。
    """
    raw = strip_sender((parsed.get('referred_message') or '').strip())
    if not raw:
        return ''
    # type 1（文本）的正文就是它自己；有的样本连 type 都没有，也当文本读
    if (parsed.get('referred_message_type') or 1) <= 1:
        return raw
    text, media = render(parsed['referred_message_type'], raw, '')
    # 只有「这张图真会被内联」才给标签，否则 聊天记录里会挂一个没有附件的 `#abcd`
    return add_handle(text, handle) if (handle and media is not None and media.describable) else text


def parse_appmsg(content: str, quoted_handle: str = '') -> str:
    """ appmsg（msg_type=49）→ 引用 / 链接 / 文件 / 小程序 / 合并转发的可读文本。

    历史上这里只处理引用(type=57)，遇到链接会把原始 XML 漏进历史，故统一收敛到此处。
    """
    from message import parse_reference_message  # message.py 是叶子模块，无循环

    xml = strip_prefix(content)
    appmsg_type = tag(xml, 'type')
    title = tag(xml, 'title')
    if appmsg_type == '57':
        try:
            parsed = parse_reference_message(content)
        except Exception:  # noqa: BLE001 —— XML 不规整时退回占位符，不能让历史里漏 XML
            return '[引用消息]'
        quoted = render_referred(parsed, quoted_handle)
        note = f'（引用前文的消息：{quoted}）' if quoted else '（引用了前文的消息）'
        # 只引了个图、自己一个字没说：那这条记录就是那句注解
        return f'{parsed["content"]}{note}' if parsed['content'] else note
    if appmsg_type == '19':
        # 合并转发（聊天记录）：正文是 <des> 里的逐条摘要（`Y: …`）——title 只有
        # 「群聊的聊天记录」这种壳，不读 des 模型就只能看到一条莫名其妙的链接。
        des = tag(xml, 'des')
        return f'[合并转发] {des}' if des else '[合并转发]'
    if appmsg_type == '6':
        return f'[文件] {title}' if title else '[文件]'
    if appmsg_type in ('33', '36'):
        return f'[小程序] {title}' if title else '[小程序]'
    label = tag(xml, 'appname') or '链接'
    return f'[{label}] {title}' if title else '[分享消息]'


def render(msg_type: int, content: str, text: str, handle: str = '',
           quoted_handle: str = '') -> tuple[str, Media | None]:
    """ 把一条消息渲染成历史记录文本（+ 可选媒体描述）。

    统一 live 与 history 两条路径的载荷处理，避免一边丢弃一边漏 XML。
    handle / quoted_handle 是内联图片用的短标签：自己的图落在自己的占位符上，
    引用图落在引用注解里的那个占位符上——两处用同一个 svrid 生成的标签。
    """
    from context import SYSTEM_KINDS, MessageKind

    kind = MessageKind.of(msg_type)
    if kind in SYSTEM_KINDS:
        # 51 / 10000 / 10002 的正文是控制 XML（`<op id=…>`），不该漏给任何调用方
        return '[系统消息]', None
    if kind is MessageKind.TEXT:
        return text, None
    if kind is MessageKind.APPMSG:
        return parse_appmsg(content, quoted_handle), None
    if kind in (MessageKind.IMAGE, MessageKind.EMOJI):
        media = parse_media(content)
        if media:
            return add_handle(media.placeholder, handle if media.describable else ''), media
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
    # 自检：真实 daemon 数据的 desc（含多语言条目），以及各种解析失败的兜底
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
    tagged = parse_media('<msg><emoji md5="m" desc="ChEKB2RlZmF1bHQSBuWVp+WVpw==" cdnurl="http://x" /></msg>')
    assert parse_media('hello world') is None
    assert parse_media('<msg><img /></msg>').needs_description is False

    # 「值不值得识图」只看两件事：是 image/emoji、而且缺描述。**不看 XML 里有没有
    # 明文 cdnurl**——daemon 现在按 msg_id 走 CDN 帧下载，入站图片的加密 fileid
    # 一样下得来；卡着这一条的结果是所有图片永远没有描述。
    enc = parse_media('<msg><img md5="m" aeskey="k" cdnbigimgurl="305f020100044b" /></msg>')
    assert enc.needs_description and enc.describable, enc
    assert image.describable and blank.describable, (image, blank)
    # 带 desc 的表情不用识图；语音/视频不看画面，也不排
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
    # 合并转发：内容在 des 里，标题只有「群聊的聊天记录」
    record = ('<msg><appmsg><title>群聊的聊天记录</title>'
              '<des>Y: 你怎么天天@雨墨\nY: 你是不是想吸引她的注意力</des>'
              '<type>19</type></appmsg></msg>')
    assert parse_appmsg(record) == '[合并转发] Y: 你怎么天天@雨墨\nY: 你是不是想吸引她的注意力', parse_appmsg(record)
    assert parse_appmsg('<msg><appmsg><title>聊天记录</title><type>19</type></appmsg></msg>') == '[合并转发]'
    assert parse_appmsg('<msg><appmsg><title>小程序名</title><type>33</type></appmsg></msg>') == '[小程序] 小程序名'
    quote = ('<msg><appmsg><title>回复内容</title><type>57</type><fromusername>wxid_me</fromusername>'
             '<refermsg><type>49</type><chatusr>wxid_alice</chatusr>'
             '<content>&lt;msg&gt;&lt;appmsg&gt;&lt;title&gt;被引用的内容&lt;/title&gt;'
             '&lt;/appmsg&gt;&lt;/msg&gt;</content></refermsg></appmsg></msg>')
    assert parse_appmsg(quote) == '回复内容（引用前文的消息：[链接] 被引用的内容）', parse_appmsg(quote)

    # 被引的是图片 / 表情：refermsg 正文是它们的 XML（没有 title）。
    # 以前这两种是 AttributeError → 整条引用变成 `[引用消息]`
    def _quote(ref_type: int, inner: str, content: str = '这也太好看了') -> str:
        escaped = inner.replace('<', '&lt;').replace('>', '&gt;')
        return (f'<msg><appmsg><title>{content}</title><type>57</type>'
                f'<refermsg><type>{ref_type}</type><chatusr>wxid_bob</chatusr>'
                f'<content>{escaped}</content></refermsg></appmsg>'
                f'<fromusername>wxid_alice</fromusername></msg>')

    assert parse_appmsg(_quote(3, '<msg><img md5="2f8f1e" /></msg>')) \
        == '这也太好看了（引用前文的消息：[图片]）'
    assert parse_appmsg(_quote(47, '<msg><emoji md5="m" desc="ChEKB2RlZmF1bHQSBuaRiuWAkg==" /></msg>')) \
        == '这也太好看了（引用前文的消息：[表情：摊倒]）'
    assert parse_appmsg(_quote(34, '<msg><voicemsg voicelength="3000" /></msg>')) \
        == '这也太好看了（引用前文的消息：[语音 3″]）'
    # 引用里带的 'wxid_x:' 前缀要去掉（daemon 落库时拼的）
    assert parse_appmsg(_quote(1, 'wxid_bob:\n周末去爬山不')) == '这也太好看了（引用前文的消息：周末去爬山不）'
    # 自己一个字没说，只引了张图
    assert parse_appmsg(_quote(3, '<msg><img md5="2f8f1e" /></msg>', content='')) \
        == '（引用前文的消息：[图片]）'
    # 引用链：被引的又是条链接
    assert parse_appmsg(_quote(49, '<msg><appmsg><title>央视新闻</title><type>5</type></appmsg></msg>')) \
        == '这也太好看了（引用前文的消息：[链接] 央视新闻）'

    # 短标签：媒体行带上 handle，引用注解落在引用里的那个占位符上（同一 svrid → 同一标签）
    assert add_handle('[图片]', '#4k2p') == '[图片 #4k2p]'
    assert add_handle('[图片：一只猫]', '#4k2p') == '[图片 #4k2p：一只猫]'
    assert add_handle('[语音 3″]', '#4k2p') == '[语音 3″]'      # 不能内联的没标签
    assert add_handle('[图片]', '') == '[图片]'
    assert render(3, '<msg><img md5="m" /></msg>', '', handle='#4k2p')[0] == '[图片 #4k2p]'
    # 自带标签的表情不内联 → 不给标签（不然会挂一个没有附件的 `#abcd`）
    assert render(47, '<msg><emoji md5="m" desc="ChEKB2RlZmF1bHQSBuaRiuWAkg==" /></msg>', '',
                  handle='#4k2p')[0] == '[表情：摊倒]'
    assert render(3, '<msg><img /></msg>', '', handle='#4k2p')[0] == '[图片]'   # 没有 md5 就没得内联
    assert render(49, _quote(3, '<msg><img md5="2f8f1e" /></msg>'), '', quoted_handle='#4k2p')[0] \
        == '这也太好看了（引用前文的消息：[图片 #4k2p]）'
    assert parse_appmsg(_quote(3, '<msg><img md5="2f8f1e" /></msg>'), '#4k2p') \
        == '这也太好看了（引用前文的消息：[图片 #4k2p]）'

    # 引用里被引的是图片 → 捞得到 Media，供内联；文本引用没有
    assert parse_reference_media(_quote(3, '<msg><img md5="2f8f1e" /></msg>')) == Media('image', '2f8f1e')
    assert parse_reference_media(_quote(1, 'wxid_bob:\n周末去爬山不')) is None
    assert parse_reference_media('<msg><appmsg><title>x</title><type>57</type></appmsg></msg>') is None
    # 损坏的引用退占位符，不往历史里漏 XML
    assert parse_appmsg('<msg><appmsg><title>x</title><type>57</type><refermsg></refermsg></appmsg></msg>') \
        == 'x（引用了前文的消息）', parse_appmsg(
            '<msg><appmsg><title>x</title><type>57</type><refermsg></refermsg></appmsg></msg>')
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
