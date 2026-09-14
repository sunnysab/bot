""" 表情包：发一张表情所需的全部元数据。

relay 的 send_emoji 要完整元数据（md5 + cdnurl + encrypturl + aeskey + len），
而 search_emoji 返回的正好就是这个形状——**所以搜到的直接就能发**，
不用像 MaiBot 那样拼 5x5 图再拿视觉模型挑。

群历史导出的表情只有文件名里的 md5（`emojis/20260213/1770997053_a56b....gif`），
没有下载地址，所以只能当「这个群的梗」的热度权重；等 relay 真收到这条表情、
把它带完整 XML 缓存下来，它才变成可发送的。`count` 就是那个热度。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Any

# 同一张表情在群里用过多少次，粗略分档——只在挑「本群的梗」时当排序依据
HOT = 20
WARM = 5

# 表情库的描述。跟 ai.DESCRIBE_PROMPT 不是一回事：
# 那个是读历史用的（[表情：摊倒]），越短越好；这个是给决策者挑用的，
# 必须说清楚「什么场合发它」，否则只有「高兴」两个字，模型没法拿当前局面去对。
EMOJI_DESCRIBE_PROMPT = (
    '这是一张聊天里用的表情包。用一行说清楚两件事，格式必须是「情绪|使用场景」：\n'
    '情绪：这张图传达什么情绪或语气，不超过 6 个字\n'
    '使用场景：什么情况下适合发它，不超过 25 个字，要具体到对方说了什么、我是什么心态\n'
    '例：无语|别人说了很离谱的话，想吐槽又懒得打字的时候\n'
    '例：狗头|刚说了句反话或者骚话，怕对方当真，补一张缓一下\n'
    '只输出这一行，不要解释，不要引号。')


def parse_description(text: str) -> tuple[str, str]:
    """ 拆开模型给的「情绪|使用场景」。没按格式来也不能丢：整段当情绪。 """
    cleaned = ' '.join((text or '').replace('\n', ' ').split()).strip('「」"\'。')
    if '|' not in cleaned:
        return cleaned[:20], ''
    emotion, _, scene = cleaned.partition('|')
    return emotion.strip()[:20], scene.strip()[:60]


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
    count: int = 0            # 群里用过多少次；0 = 不是从群里偷的

    @property
    def sendable(self) -> bool:
        """ 能不能发出去。没有下载地址的收件人取不到图，发了也是白条。 """
        return bool(self.md5 and self.cdnurl)

    @property
    def heat(self) -> str:
        if self.count >= HOT:
            return '群里常用'
        if self.count >= WARM:
            return '群里用过'
        return ''

    def to_dict(self) -> dict[str, Any]:
        """ relay 的 emoji 参数格式（字段名 len 不是 length）。 """
        data = asdict(self)
        data['len'] = data.pop('length')
        # 热度和场景都是我们自己的事，不发给 relay
        data.pop('count', None)
        data.pop('scene', None)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> Emoji:
        data = data or {}
        return cls(
            md5=str(data.get('md5') or data.get('Md5') or ''),
            desc=str(data.get('desc') or data.get('title') or data.get('word') or ''),
            length=int(data.get('len') or data.get('size') or 0),
            cdnurl=str(data.get('cdnurl') or data.get('cdnUrl') or data.get('url') or ''),
            encrypturl=str(data.get('encrypturl') or data.get('encryptUrl') or ''),
            aeskey=str(data.get('aeskey') or data.get('aesKey') or ''),
            thumburl=str(data.get('thumburl') or data.get('thumbUrl') or ''),
            width=int(data.get('width') or 400),
            height=int(data.get('height') or 400),
            count=int(data.get('count') or 0),
            scene=str(data.get('scene') or ''),
        )

    def merge(self, other: Emoji) -> Emoji:
        """ 合并同一张表情的两份记录。

        元数据（下载地址这些）以拿到的那份为准；热度取大的——历史统计永远不会比
        实时观察少。谁有描述用谁的，因为描述可能来自识图也可能是商店给的。
        """
        return replace(
            self if self.sendable else other,
            desc=self.desc or other.desc,
            scene=self.scene or other.scene,
            count=max(self.count, other.count),
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

    # 转回 relay 要的形状：字段名是 len，且不带上我们自己加的 count
    payload = emoji.to_dict()
    assert payload['len'] == 348401 and 'length' not in payload and 'count' not in payload
    assert payload['md5'].startswith('a56b') and payload['aeskey'].startswith('df54')
    assert Emoji.from_dict(payload) == replace(emoji, count=0)

    # 只有 md5（历史导出的那批）发不出去，只能当热度
    history = Emoji(md5='b' * 32, count=7)
    assert not history.sendable and history.heat == '群里用过'

    # 合并：能发的那份说了算，热度取大的，描述谁有算谁的
    live = Emoji(md5='b' * 32, cdnurl='http://cdn/z', aeskey='k', length=12)
    merged = history.merge(live)
    assert merged.sendable and merged.count == 7 and merged.cdnurl == 'http://cdn/z'
    assert merged.desc == ''
    assert live.merge(Emoji(md5='b' * 32, desc='无语', count=99)).desc == '无语'
    assert live.merge(Emoji(md5='b' * 32, count=99)).count == 99
    # 反向合并也不能把已有的下载地址丢掉
    assert live.merge(history).cdnurl == 'http://cdn/z'

    assert Emoji.from_dict(None).md5 == ''
    assert not Emoji(md5='x').sendable
    assert Emoji(md5='x', cdnurl='u').render(2) == '2. (没有描述)'
    assert Emoji(md5='x', desc='狗头', count=6).render(1) == '1. 狗头 [群里用过]'

    # 场景是给决策者看的，候选行里要带上
    assert Emoji(md5='x', desc='无语', scene='别人说了很离谱的话').render(3) == \
        '3. 无语｜别人说了很离谱的话'
    assert 'scene' not in Emoji(md5='x', desc='无语', scene='s').to_dict()

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

    print('emojis self-check ok')


if __name__ == '__main__':
    _self_check()
