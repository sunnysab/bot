""" 表情包：发一张表情所需的全部元数据。

relay 的 send_emoji 要完整元数据（md5 + cdnurl + encrypturl + aeskey + len），
而 search_emoji 返回的正好就是这个形状——**所以搜到的直接就能发**，
不用像 MaiBot 那样拼 5x5 图再拿视觉模型挑。

表情库**没有离线快照**：群里收到一条表情就入库，`count` 是运行期数出来的
「这个群用过它几次」。历史导出的那 38836 条早就不要了——只有 md5 没有下载地址，
发了也是白条，而它唯一的用处（热度权重）现在由运行时自己数。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

# 同一张表情在群里用过多少次，粗略分档——只在挑「本群的梗」时当排序依据
HOT = 20
WARM = 5

# 表情库的描述。跟 ai.DESCRIBE_PROMPT 不是一回事：
# 那个是读历史用的（[表情：摊倒]），越短越好；这个是给决策者挑用的，
# 必须说清楚「什么场合发它」，否则只有「高兴」两个字，模型没法拿当前局面去对。
# 表情场景的识图 prompt。
#
# **短就是好——这是实测出来的，不是拍的。** 原因：这个中转的模型（deepseek 4.1）是 thinking
# 模型，推理也算在 max_tokens 里，而**推理长度跟 prompt 的啰嗦程度直接相关**：
# 原来那份带两个长例子的 prompt，同一批表情里 reasoning 在 56 ~ >2048 之间乱跳，
# 1024 会截断（三张里一张 empty）、2048 也可能截断；
# 换成下面这个短版之后 3/3 成功，reasoning 降到 259~459，512 预算就够，
# 延迟从 6~13s 降到 4~6.5s。质量不变（三张拿人眼对过）。
#
# `enable_thinking: false` 这条路试过，**这个中转直接无视**（照样把预算烧光），
# 所以只能从「少让它想」下手。
EMOJI_DESCRIBE_PROMPT = (
    '看这张表情包，输出一行：情绪|使用场景\n'
    '情绪≤6字；使用场景≤20字，说清什么情况下会发它。\n'
    '只输出这一行，不要分析。')


def parse_description(text: str) -> tuple[str, str]:
    """ 拆开模型给的「情绪|使用场景」。没按格式来也不能丢：整段当情绪。 """
    cleaned = ' '.join((text or '').replace('\n', ' ').split()).strip('「」"\'。')
    if '|' not in cleaned:
        return cleaned[:20], ''
    emotion, _, scene = cleaned.partition('|')
    return emotion.strip()[:20], scene.strip()[:60]


# ---- 从收到的表情消息里偷 ----

LIBRARY_LIMIT = 400      # 库上限，超过就按热度砍尾巴
SEARCH_LIMIT = 8         # 一次搜索最多给模型看几张


def parse_emoji_xml(content: str) -> Emoji | None:
    """ 把收到的表情消息 XML 解析成可发送的 Emoji。

    这一步是「偷表情」的全部关键：微信的表情消息本身就带 cdnurl/aeskey/len，
    所以**群里收到的表情当场就能原样发回去**，不用再找图、不用视觉模型。
    desc 属性是 base64 的 protobuf，解出来就是「摊倒」这类中文标签。
    """
    from media import attr, parse_emoji_description, strip_prefix

    if '<emoji' not in content:
        return None
    xml = strip_prefix(content)
    emoji = Emoji(
        md5=attr(xml, 'md5'),
        desc=parse_emoji_description(content) or '',
        length=int(attr(xml, 'len') or 0),
        cdnurl=attr(xml, 'cdnurl'),
        encrypturl=attr(xml, 'encrypturl'),
        aeskey=attr(xml, 'aeskey'),
        thumburl=attr(xml, 'thumburl'),
        width=int(attr(xml, 'width') or 0) or 400,
        height=int(attr(xml, 'height') or 0) or 400)
    return emoji if emoji.md5 else None


class EmojiLibrary:
    """ 本地表情库：md5 -> 描述 / 场景 / 元数据 / 群里用过多少次。

    全部来自运行时：群里收到的表情（带完整 XML，能原样发回去）、以及联网搜到的
    （当缓存）。热度也是运行时数出来的——每次在群里看到一次就加一，所以
    「本群的梗」会随群里的用法自己长出来，不靠任何离线重跑。
    """

    def __init__(self, entries: dict[str, Emoji] | None = None):
        self._entries: dict[str, Emoji] = dict(entries or {})

    def __len__(self) -> int:
        return len(self._entries)

    def __bool__(self) -> bool:
        """ 永远为真。

        len() 也会参与真值判断，空库会被当成假，于是「没配表情库」和
        「库还是空的」就分不出来了——偷表情永远不触发。这里必须盖住。
        """
        return True

    def get(self, md5: str) -> Emoji | None:
        return self._entries.get(md5)

    def remove(self, md5: str) -> bool:
        """ 删一张。面板上看到场景描述写得离谱的可以直接抹掉（描述是花钱识图来的）。 """
        return self._entries.pop(md5, None) is not None

    def items(self) -> list[Emoji]:
        """ 全部表情，热度高的在前（面板浏览用）。 """
        return sorted(self._entries.values(), key=lambda item: item.count, reverse=True)

    @property
    def sendable_count(self) -> int:
        """ 现在真能发出去的有几张——剩下的只是在等 relay 收到一次。 """
        return sum(1 for item in self._entries.values() if item.sendable)

    @property
    def scene_count(self) -> int:
        """ 已经写清「什么场合发它」的有几张。 """
        return sum(1 for item in self._entries.values() if item.scene)

    @classmethod
    def load(cls, path: str | Path) -> EmojiLibrary:
        """ 读不到、读坏了都当空库——表情库不是关键路径，不能因此起不来。 """
        try:
            raw = json.loads(Path(path).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return cls()
        entries = {}
        for item in raw if isinstance(raw, list) else (raw or {}).values():
            emoji = Emoji.from_dict(item)
            if emoji.md5:
                entries[emoji.md5] = emoji
        return cls(entries)

    def save(self, path: str | Path) -> None:
        keep = self._trimmed()
        Path(path).write_text(
            json.dumps([item.to_dict() for item in keep], ensure_ascii=False, indent=1),
            encoding='utf-8')

    def record(self, emoji: Emoji | None, used: bool = False) -> bool:
        """ 记一笔，返回库内容是否变了（变了才值得落盘）。

        used=True 表示「群里又有人发了这一张」——热度是数出来的，加一。
        联网搜到的结果只当缓存，不加热度（那是表情商店在用，不是群里在用）。
        """
        if emoji is None or not emoji.md5:
            return False
        existing = self._entries.get(emoji.md5)
        if existing is None:
            self._entries[emoji.md5] = replace(emoji, count=emoji.count + 1) if used else emoji
            return True
        merged = existing.merge(emoji, bump=used)
        if merged == existing:
            return False
        self._entries[emoji.md5] = merged
        return True

    def search(self, keyword: str, limit: int = SEARCH_LIMIT) -> list[Emoji]:
        """ 按描述/场景找能发的表情，热度高的在前。 """
        keyword = (keyword or '').strip()
        if not keyword:
            return []
        hits = [item for item in self._entries.values()
                if item.sendable and _mentions(item, keyword)]
        hits.sort(key=lambda item: item.count, reverse=True)
        return hits[:limit]

    def _trimmed(self) -> list[Emoji]:
        """ 能发的优先，其次经常用的——库里只留最有价值的那些。 """
        items = sorted(self._entries.values(),
                       key=lambda item: (item.sendable, item.count), reverse=True)
        return items[:LIBRARY_LIMIT]


def _mentions(emoji: Emoji, keyword: str) -> bool:
    """ 双向包含：搜「无语」能中 desc=「无语」，搜「有点无语」也能中。 """
    for field in (emoji.desc, emoji.scene):
        if field and (keyword in field or field in keyword):
            return True
    return False


class EmojiPolicy:
    """ 发表情的节制：刚发过就先别发，同一张别短时间重复。

    模型看不到自己上次是什么时候发的表情，这件事只能由程序管。
    """

    def __init__(self, cooldown: float = 300.0, repeat: float = 1800.0):
        self.cooldown = cooldown
        self.repeat = repeat
        self._last_sent = 0.0
        self._recent: dict[str, float] = {}

    def refuses(self, md5: str, now: float) -> str:
        """ 不能发就返回理由（发给模型看），能发返回空串。 """
        if now - self._last_sent < self.cooldown:
            return f'刚发过表情（{int(now - self._last_sent)} 秒前），这一轮先别发。'
        sent_at = self._recent.get(md5)
        if sent_at is not None and now - sent_at < self.repeat:
            return '这张最近发过了，换一张或者干脆用文字。'
        return ''

    def note(self, md5: str, now: float) -> None:
        self._last_sent = now
        self._recent = {key: at for key, at in self._recent.items() if now - at < self.repeat}
        self._recent[md5] = now


@dataclass(frozen=True)
class Emoji:
    """ 一张表情。字段和 relay 的 EmojiItem 对齐。 """

    md5: str
    desc: str = ''
    scene: str = ''            # 什么场合发它；planner 就是拿当前局面跟这个对
    length: int = 0
    cdnurl: str = ''
    encrypturl: str = ''
    aeskey: str = ''
    thumburl: str = ''
    width: int = 400
    height: int = 400
    count: int = 0            # 群里用过多少次；运行时每看到一次加一

    @property
    def sendable(self) -> bool:
        """ 能不能发出去。

        只要 md5 + 下载地址。relay 发表情走 `/sendemoji`（CGI 68），服务端拿 md5 自己
        补齐元数据，不需要本地有 len/aeskey——商店搜回来的那些（len 恒为 0）也能发。
        """
        return bool(self.md5 and self.cdnurl)

    @property
    def heat(self) -> str:
        if self.count >= HOT:
            return '群里常用'
        if self.count >= WARM:
            return '群里用过'
        return ''

    def to_relay(self) -> dict[str, Any]:
        """ relay 的 emoji 参数格式（字段名 len 不是 length）。

        热度和场景只在本地有意义，不发给 relay。
        """
        data = asdict(self)
        data['len'] = data.pop('length')
        data.pop('count', None)
        data.pop('scene', None)
        return data

    def to_dict(self) -> dict[str, Any]:
        """ 落盘用：全字段保留，能从 from_dict 完整读回来。 """
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> Emoji:
        data = data or {}
        return cls(
            md5=str(data.get('md5') or data.get('Md5') or ''),
            desc=str(data.get('desc') or data.get('title') or data.get('word') or ''),
            length=int(data.get('len') or data.get('length') or data.get('size') or 0),
            cdnurl=str(data.get('cdnurl') or data.get('cdnUrl') or data.get('url') or ''),
            encrypturl=str(data.get('encrypturl') or data.get('encryptUrl') or ''),
            aeskey=str(data.get('aeskey') or data.get('aesKey') or ''),
            thumburl=str(data.get('thumburl') or data.get('thumbUrl') or ''),
            width=int(data.get('width') or 400),
            height=int(data.get('height') or 400),
            count=int(data.get('count') or 0),
            scene=str(data.get('scene') or ''),
        )

    def merge(self, other: Emoji, bump: bool = False) -> Emoji:
        """ 合并同一张表情的两份记录。

        元数据（下载地址这些）以拿到的那份为准；热度取大的再加这一笔。
        谁有描述用谁的，因为描述可能来自识图也可能是商店给的。
        """
        return replace(
            self if self.sendable else other,
            desc=self.desc or other.desc,
            scene=self.scene or other.scene,
            count=max(self.count, other.count) + (1 if bump else 0),
        )

    def render(self, index: int) -> str:
        """ 给模型看的一行候选。只有文字——模型的活是挑场景，不是看图。 """
        label = self.desc or '(没有描述)'
        if self.scene:
            label = f'{label}｜{self.scene}'
        parts = [f'{index}. {label}']
        if self.heat:
            parts.append(f'[{self.heat}]')
        return ' '.join(parts)


def _self_check() -> None:
    emoji = Emoji.from_dict({
        'md5': 'a56b036e6416b4206c6e14eb3264de53',
        'len': 348401,
        'cdnurl': 'http://cdn/x',
        'encrypturl': 'http://cdn/y',
        'aeskey': 'df548bd0439e4ae08338050b6a34cac8',
        'desc': '高兴',
        'width': 300,
        'height': 300,
        'count': 30,
    })
    assert emoji.sendable and emoji.desc == '高兴' and emoji.heat == '群里常用'

    # 转回 relay 要的形状：字段名是 len，且不带上我们自己加的 count/scene
    payload = emoji.to_relay()
    assert payload['len'] == 348401 and 'length' not in payload and 'count' not in payload
    assert 'scene' not in payload
    assert payload['md5'].startswith('a56b') and payload['aeskey'].startswith('df54')
    # 落盘格式要能完整往返
    assert Emoji.from_dict(emoji.to_dict()) == emoji

    # 没有下载地址就发不出去，只能当热度
    history = Emoji(md5='b' * 32, count=7)
    assert not history.sendable and history.heat == '群里用过'

    # 合并：能发的那份说了算，热度取大的，描述谁有算谁的
    live = Emoji(md5='b' * 32, cdnurl='http://cdn/z', aeskey='k', length=12)
    merged = history.merge(live)
    assert merged.sendable and merged.count == 7 and merged.cdnurl == 'http://cdn/z'
    assert merged.desc == ''
    assert live.merge(Emoji(md5='b' * 32, desc='无语', count=99)).desc == '无语'
    assert live.merge(Emoji(md5='b' * 32, desc='无语', count=99)).count == 99
    # 群里又用了一次：只有 bump=True 才加一
    assert live.merge(history, bump=True).count == 8
    assert live.merge(history).count == 7
    # 反向合并也不能把已有的下载地址丢掉
    assert live.merge(history).cdnurl == 'http://cdn/z'

    assert Emoji.from_dict(None).md5 == ''
    assert not Emoji(md5='x').sendable
    assert Emoji(md5='x', cdnurl='u').render(2) == '2. (没有描述)'
    assert Emoji(md5='x', desc='狗头', count=6).render(1) == '1. 狗头 [群里用过]'

    # 场景是给决策者看的，候选行里要带上
    assert Emoji(md5='x', desc='无语', scene='别人说了很离谱的话').render(3) == \
        '3. 无语｜别人说了很离谱的话'
    assert 'scene' not in Emoji(md5='x', desc='无语', scene='s').to_relay()

    # 描述拆解：不按格式来也不能丢
    assert parse_description('无语|别人说了很离谱的话，想吐槽又懒得打字的时候') == \
        ('无语', '别人说了很离谱的话，想吐槽又懒得打字的时候')
    assert parse_description('「高兴」') == ('高兴', '')
    assert parse_description('  开心 \n') == ('开心', '')
    assert parse_description('') == ('', '')
    assert parse_description('a|b|c')[1] == 'b|c'
    # 超长截断，不能让一条破描述把 prompt 撑爆
    assert len(parse_description('x' * 80)[0]) == 20
    assert len(parse_description('a|' + 'y' * 200)[1]) == 60

    # 从群里的表情消息偷：微信的表情 XML 自带 cdnurl/aeskey，当场就能原样发回去
    stolen = parse_emoji_xml(
        '<msg><emoji md5="' + 'a' * 32 + '" len="348401" cdnurl="http://cdn/x" '
        'encrypturl="http://cdn/y" aeskey="key" width="300" height="300" '
        'desc="ChEKB2RlZmF1bHQSBuaRiuWAkg==" /></msg>')
    assert stolen is not None and stolen.sendable
    assert stolen.desc == '摊倒'          # base64 protobuf 解出来就是中文标签
    assert stolen.length == 348401 and stolen.aeskey == 'key'
    # 拼在 XML 前的 'wxid_xxx:' 前缀要能剃掉
    assert parse_emoji_xml('wxid_x:\n<msg><emoji md5="m" cdnurl="u" /></msg>').md5 == 'm'
    assert parse_emoji_xml('<msg><emoji cdnurl="u" /></msg>') is None   # 没 md5
    assert parse_emoji_xml('<msg><img md5="m" /></msg>') is None
    assert parse_emoji_xml('hello') is None

    library = EmojiLibrary()
    assert len(library) == 0 and library  # 空库仍然是真值，否则分不清「没配」和「空」
    assert len(library.search('摊倒')) == 0
    # 群里看到一次就地入库：热度 1，没有下载地址就发不出去
    assert library.record(Emoji(md5='h' * 32), used=True) is True
    assert library.get('h' * 32).count == 1
    assert library.search('摊倒') == []              # 发不出去的不给模型挑
    # 又看到一次：热度加一；不传 used 的（联网搜到的缓存）不加热度
    assert library.record(Emoji(md5='h' * 32), used=True) is True
    assert library.get('h' * 32).count == 2
    assert library.record(Emoji(md5='h' * 32)) is False
    assert library.get('h' * 32).count == 2
    # 真收到一次就补上了元数据，随之变得可搜可发
    library.record(stolen)
    assert library.get('h' * 32).sendable is False
    library.record(Emoji(md5=stolen.md5, desc='摊倒', scene='累到瘫在椅子上时发', cdnurl='http://cdn/x'))
    assert library.get(stolen.md5).count == 0        # 这条只是入库缓存，不算群里用过
    assert len(library.search('摊倒')) == 1
    assert len(library.search('累到瘫')) == 1        # 场景也能搜中
    assert library.search('') == [] and library.search('无关词') == []
    assert len(library.search('摊倒', limit=0)) == 0

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / 'emojis.json'
        library.save(path)
        reloaded = EmojiLibrary.load(path)
        assert len(reloaded) == 2 and reloaded.get(stolen.md5).sendable
        assert reloaded.get(stolen.md5).scene == '累到瘫在椅子上时发'
        assert reloaded.get(stolen.md5).desc == '摊倒'
    assert EmojiLibrary.load('/no/such/file.json').get('x') is None

    # 面板浏览（热度排序）与删除
    assert {e.md5 for e in library.items()} == {'h' * 32, stolen.md5}
    assert [e.md5 for e in library.items()][0] == 'h' * 32      # 热度 2 > 0，排前面
    assert library.remove('没这个') is False
    assert library.remove('h' * 32) is True
    assert library.get('h' * 32) is None and len(library) == 1

    # 节制：刚发过不让发，同一张短期不重复
    policy = EmojiPolicy(cooldown=300, repeat=1800)
    assert policy.refuses('a', 1000.0) == ''
    policy.note('a', 1000.0)
    assert '刚发过表情' in policy.refuses('b', 1100.0)
    assert policy.refuses('b', 1400.0) == ''        # 过了冷却
    assert '最近发过' in policy.refuses('a', 1400.0)
    assert policy.refuses('b', 3000.0) == ''        # 过了重复期

    print('emojis self-check ok')


if __name__ == '__main__':
    _self_check()
