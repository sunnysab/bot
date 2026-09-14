""" 群友画像的运行时读取与 prompt 渲染。

画像文件由 build_profiles.py 离线生成，见 memory/profiles.json。
机器人用发送者昵称查出对应的人，把风格摘要注入 prompt，让回复「认识人」。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

SUFFIX = re.compile(r'[（(\[【][^）)\]】]{0,20}[）)\]】]\s*$')


def normalize_name(name: str) -> str:
    """ 去掉群昵称尾部的括注和空格，得到匹配用的基名。 """
    cleaned = (name or '').strip()
    while cleaned:
        stripped = SUFFIX.sub('', cleaned).strip()
        if stripped == cleaned:
            break
        cleaned = stripped
    return cleaned


@dataclass(frozen=True)
class Profile:
    """ 一个群友的风格摘要。 """

    wxid: str
    names: tuple[str, ...] = ()
    sticker_ratio: float = 0.0
    avg_len: float = 0.0
    short_ratio: float = 0.0
    laugh_ratio: float = 0.0
    phrases: tuple[str, ...] = ()
    topics: tuple[str, ...] = ()
    samples: tuple[str, ...] = ()

    @property
    def name(self) -> str:
        return self.names[0] if self.names else self.wxid

    def render(self) -> str:
        """ 渲染成几行 prompt 注记。 """
        style = f'平均 {self.avg_len:.0f} 字/条，{self.short_ratio:.0%} 是短句'
        if self.laugh_ratio >= 0.03:
            style += '，爱哈哈'
        if self.sticker_ratio >= 0.15:
            style += '，表情包大户'

        lines = [f'【群友】{self.name}', f'说话：{style}']
        if self.phrases:
            lines.append('口头禅：' + '、'.join(self.phrases))
        if self.topics:
            lines.append('常聊：' + '、'.join(self.topics))
        if self.samples:
            lines.append('说过：' + ' / '.join(f'"{sample}"' for sample in self.samples))
        return '\n'.join(lines)


@dataclass
class ProfileStore:
    """ 按昵称查群友画像。 """

    people: dict[str, Profile] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._by_name: dict[str, Profile] = {}
        for profile in self.people.values():
            for name in profile.names:
                for key in (name, normalize_name(name)):
                    if key:
                        self._by_name.setdefault(key, profile)

    def __len__(self) -> int:
        return len(self.people)

    @classmethod
    def load(cls, path: str | Path) -> ProfileStore | None:
        """ 读画像文件；不存在或格式不对就返回 None（不影响机器人启动）。 """
        file = Path(path)
        try:
            data = json.loads(file.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return None

        people: dict[str, Profile] = {}
        for wxid, raw in (data.get('people') or {}).items():
            people[wxid] = Profile(
                wxid=wxid,
                names=tuple(raw.get('names') or [wxid]),
                sticker_ratio=raw.get('sticker_ratio', 0.0),
                avg_len=raw.get('avg_len', 0.0),
                short_ratio=raw.get('short_ratio', 0.0),
                laugh_ratio=raw.get('laugh_ratio', 0.0),
                phrases=tuple(raw.get('phrases') or ()),
                topics=tuple(raw.get('topics') or ()),
                samples=tuple(raw.get('samples') or ()),
            )
        return cls(people=people)

    def find(self, name: str) -> Profile | None:
        """ 精确匹配，其次去括注匹配，最后退化成包含匹配。 """
        if not name:
            return None
        if (profile := self._by_name.get(name)) is not None:
            return profile
        base = normalize_name(name)
        if base and (profile := self._by_name.get(base)) is not None:
            return profile
        for key, profile in self._by_name.items():
            if len(key) >= 2 and key in name:
                return profile
        return None

    def render_for(self, names: Iterable[str], limit: int = 3) -> str:
        """ 给最近发言者渲染画像块，同一个人只渲染一次。 """
        seen: set[str] = set()
        blocks: list[str] = []
        for name in names:
            profile = self.find(name)
            if profile is None or profile.wxid in seen:
                continue
            seen.add(profile.wxid)
            blocks.append(profile.render())
            if len(blocks) >= limit:
                break
        return '\n\n'.join(blocks)


def _self_check() -> None:
    assert normalize_name('扶风（意向做上门女婿）') == '扶风'
    assert normalize_name('sunnysab（白天喊我写论文）') == 'sunnysab'
    assert normalize_name('普通人') == '普通人'

    store = ProfileStore(people={
        'wxid_a': Profile('wxid_a', names=('豆浆', '孔书贤'), avg_len=6.2, short_ratio=0.28,
                          laugh_ratio=0.05, phrases=('蛙趣', '我服了'), topics=('上班',), samples=('这死班我真干不动了',)),
        'wxid_b': Profile('wxid_b', names=('扶风（意向做上门女婿）', '扶风')),
    })
    assert store.find('豆浆') is not None
    assert store.find('扶风') is not None
    assert store.find('扶风（打螺丝）') is not None      # 去括注后匹配
    assert store.find('查无此人') is None

    block = store.render_for(['豆浆', '豆浆', '扶风', '查无此人'])
    assert block.count('【群友】') == 2
    assert '口头禅：蛙趣、我服了' in block
    print('profile self-check ok')


if __name__ == '__main__':
    _self_check()
