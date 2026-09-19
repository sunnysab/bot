""" 分会话人设覆盖：某个群/某个人用另一套人设，其余会话继承全局。

存一份 JSON：`{会话显示名: {字段: 值}}`。字段只允许是 `persona.OVERRIDE_FIELDS`
里的那几个——**值留空 = 删掉这条覆盖、回落全局**，所以面板上清空输入框就是「继承」。

为什么用显示名当键：和 `ReplyScope`（允许回复名单）同一个口径，插件链也是按显示名
分发的。显示名改了覆盖就丢——和名单一样的已知弱点，先接受，要稳就得换 peer 键 + 一层映射。

为什么不 per-contact 建插件实例：人设只是渲染 prompt 时读的一份数据，共享一个
`ChatPlugin` 每次现取就够；建实例会把 profiles/habits/tracer/工具全复制一遍。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from loguru import logger

from persona import OVERRIDE_FIELDS, Persona, apply_overrides


class PersonaStore:
    """ 分会话人设覆盖，落盘一个 JSON。 """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._data: dict[str, dict] = {}
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding='utf-8'))
                if isinstance(loaded, dict):
                    self._data = {str(chat): self._clean(values)
                                  for chat, values in loaded.items() if isinstance(values, dict)}
            except (OSError, ValueError) as e:  # noqa: BLE001 —— 文件坏了不能拖垮启动
                logger.warning(f'分会话人设读不出来（{self.path}），按全局来: {e}')

    def get(self, contact: str) -> dict:
        """ 这个会话的覆盖；没有就返回空字典。 """
        return dict(self._data.get(contact) or {})

    def persona_for(self, contact: str, base: Persona) -> Persona:
        return apply_overrides(base, self._data.get(contact))

    def set(self, contact: str, values: dict) -> dict:
        """ 整份替换这个会话的覆盖。空值字段被丢掉（= 继承全局）。 """
        if not contact:
            raise ValueError('会话不能为空')
        cleaned = self._clean(values)
        if cleaned:
            self._data[contact] = cleaned
        else:
            self._data.pop(contact, None)
        self.save()
        return cleaned

    def remove(self, contact: str) -> bool:
        removed = self._data.pop(contact, None) is not None
        if removed:
            self.save()
        return removed

    def to_dict(self) -> dict:
        return {chat: dict(values) for chat, values in self._data.items()}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + '.tmp')
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=1), encoding='utf-8')
        os.replace(tmp, self.path)

    @staticmethod
    def _clean(values: dict) -> dict:
        """ 只留认识的字段，且丢掉空值。未知字段直接拒——面板拼错名字要当场知道。 """
        unknown = sorted(set(values) - set(OVERRIDE_FIELDS))
        if unknown:
            raise ValueError(f'不认识的人设字段：{", ".join(unknown)}')
        out: dict = {}
        for key, value in values.items():
            if value is None or value == '' or value == []:
                continue
            if key == 'style_probability':
                number = float(value)
                if not 0.0 <= number <= 1.0:
                    raise ValueError(f'style_probability 要在 0~1 之间，收到 {number}')
                out[key] = number
            elif key == 'aliases':
                out[key] = list(value) if isinstance(value, (list, tuple)) else str(value)
            else:
                out[key] = str(value)
        return out


def _self_check() -> None:
    import tempfile

    base = Persona()
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / 'personas.json'
        store = PersonaStore(path)
        assert store.get('某群') == {} and store.persona_for('某群', base) is base

        store.set('某群', {'personality': '是只猫', 'aliases': '阿猫|阿狗'})
        assert store.get('某群') == {'personality': '是只猫', 'aliases': '阿猫|阿狗'}
        persona = store.persona_for('某群', base)
        assert persona.personality == '是只猫' and persona.aliases == ('阿猫', '阿狗')
        assert persona.reply_style == base.reply_style

        # 重开一次：落盘要能读回来
        assert PersonaStore(path).get('某群')['personality'] == '是只猫'

        # 覆盖只作用于这个会话
        assert store.persona_for('另一个群', base) is base

        # 空值 = 删掉这条覆盖；清空全部 = 整个会话回全局
        store.set('某群', {'personality': '是只猫', 'aliases': ''})
        assert store.get('某群') == {'personality': '是只猫'}
        store.set('某群', {})
        assert store.get('某群') == {} and not path.read_text(encoding='utf-8').strip('{}\n ')

        # 不认识的字段 / 越界的概率要当场拒
        for bad in ({'nope': 1}, {'style_probability': 2.0}):
            try:
                store.set('某群', bad)
                raise AssertionError(f'{bad} 应该被拒')
            except ValueError:
                pass
        print('personas self-check ok')


if __name__ == '__main__':
    _self_check()
