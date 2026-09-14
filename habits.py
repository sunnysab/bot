""" 群里的特色用语（梗 / 口头禅 / 缩写）的运行时查询与渲染。

数据由 build_habits.py 离线挖出，见 memory/habits.json。

只在对得上时才注入——当前消息里出现了某个用语，才把它的真实用例贴进 prompt。
对不上就什么都不加，不硬塞，也不做「每次都塞一堆梗」的事。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

MAX_MATCHES = 3


@dataclass(frozen=True)
class Habit:
    """ 一条群用语。 """

    phrase: str
    count: int = 0
    samples: tuple[str, ...] = ()

    def render(self) -> str:
        lines = [f'「{self.phrase}」——群里出现 {self.count} 次，这么说：']
        lines.extend(f'  · {sample}' for sample in self.samples)
        return '\n'.join(lines)


@dataclass
class HabitStore:
    """ 按当前消息内容命中群用语。 """

    habits: tuple[Habit, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        # 长词优先：避免短词把长词的位置挤掉（"我服了" 优先于 "我"）
        self._sorted = tuple(sorted(self.habits, key=lambda habit: -len(habit.phrase)))

    def __len__(self) -> int:
        return len(self.habits)

    @classmethod
    def load(cls, path: str | Path) -> HabitStore | None:
        """ 读群用语文件；不存在或格式不对就返回 None（不影响机器人启动）。 """
        try:
            data = json.loads(Path(path).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return None

        habits = tuple(
            Habit(phrase=raw.get('phrase', ''), count=raw.get('count', 0),
                  samples=tuple(raw.get('samples') or ()))
            for raw in (data.get('habits') or [])
            if raw.get('phrase')
        )
        return cls(habits=habits)

    def match(self, text: str, limit: int = MAX_MATCHES) -> list[Habit]:
        if not text:
            return []
        return [habit for habit in self._sorted if habit.phrase in text][:limit]

    def render(self, text: str, limit: int = MAX_MATCHES) -> str:
        """ 命中才渲染；没命中返回空串。 """
        matched = self.match(text, limit)
        if not matched:
            return ''
        return '群里这些说法是这么用的：\n' + '\n'.join(habit.render() for habit in matched)


def _self_check() -> None:
    import tempfile

    store = HabitStore(habits=(
        Habit('笑死', 371, ('笑死，你们说女生有二十多个',)),
        Habit('我服了', 240, ('我服了，洗完澡打算吹头',)),
        Habit('555', 566, ('怎么 sunny 哥哥秒接电话 555',)),
    ))
    assert store.match('这画面笑死我了')[0].phrase == '笑死'
    assert store.match('跟我没关系') == []
    assert store.render('平静的一句话') == ''
    assert '我服了' in store.render('我服了，又加班')

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / 'habits.json'
        path.write_text(json.dumps({'version': 1, 'habits': [
            {'phrase': '蛙趣', 'count': 126, 'samples': ['蛙趣 你的ID和小红书一样的吧']}]}), encoding='utf-8')
        loaded = HabitStore.load(path)
        assert loaded.match('蛙趣，真的假的')[0].count == 126

    assert HabitStore.load('/no/such/file.json') is None
    print('habits self-check ok')


if __name__ == '__main__':
    _self_check()
