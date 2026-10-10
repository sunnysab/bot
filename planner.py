""" 决策结果：决策者（工具模式）把「说什么」装成一个 Plan，交给表达者。

Plan 只承载判断、不承载台词：`speak` 决定这一轮开不开口，`analysis` / `intent` /
`emotion` 写进请求正文，让表达者动笔时眼前就有它。

它以前还有个 JSON 解析入口（单轮 JSON 决策），那条路已经删掉——现在进到这里的
只有工具调用的结果。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Plan:
    """ 决策者的判断。 """

    speak: bool
    analysis: str = ''
    intent: str = ''
    emotion: str = ''
    emoji: str = ''            # 本轮同时要发的表情（描述）；写进决策块让表达者避开重复

    def render(self) -> str:
        """ 渲染成给表达者看的参考文本；不发言时返回空串。 """
        if not self.speak:
            return ''
        lines = []
        if self.analysis:
            lines.append(f'局面：{self.analysis}')
        if self.intent:
            lines.append(f'想表达：{self.intent}')
        if self.emotion:
            lines.append(f'情绪：{self.emotion}')
        if self.emoji:
            lines.append(f'同时会发一张表情：{self.emoji}（文字不要重复这个情绪）')
        return '\n'.join(lines)


def _self_check() -> None:
    plan = Plan(speak=True, analysis='在吐槽加班', intent='附和一句', emotion='无奈')
    assert plan.render().splitlines()[0] == '局面：在吐槽加班'
    assert '想表达：附和一句' in plan.render() and '情绪：无奈' in plan.render()

    # 不发言 / 没内容都渲染成空串
    assert Plan(speak=False, analysis='接不上').render() == ''
    assert Plan(speak=True).render() == ''

    # 表情写进决策块，让表达者知道文字别重复这个情绪
    assert '同时会发一张表情：无语' in Plan(speak=True, intent='附和', emoji='无语').render()

    print('planner self-check ok')


if __name__ == '__main__':
    _self_check()
