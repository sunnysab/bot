""" 人格配置：把「我是谁 / 怎么说话 / 怎么行动」拆开，分别喂给不同的 prompt。

- personality  → expressor（replyer）开头，定义身份和性格
- reply_style  → expressor，定义表达风格（句式、语气，而不是字数限制）
- behavior_style → planner，定义行动准则（何时参与、何时闭嘴）
- alternates   → expressor，按概率换一种临时风格，避免每条一个味儿

拆开的好处：改人设不用动代码，也不用把人设、行为、风格混在一段话里互相稀释。
"""

from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass(frozen=True)
class Persona:
    """ 机器人的固定人格 + 可随机切换的表达风格。 """

    personality: str = '是一个积极乐观、温暖阳光的女生，03 年出生。'
    reply_style: str = (
        '说话像微信打字：短，常省略主语或倒装，爱用语气词收尾（啊/吧/呢/嘛）。'
        '不滥用比喻和书面词，不分点，不用 markdown。'
        '偶尔用一个很短的词开头（哈哈 / 6 / ？），但不要每次都这样。')
    behavior_style: str = (
        '按自己的兴趣参与群聊：被 @ 或叫到名字就认真回应；'
        '没人点名时，话题接得上、说得出有意思的话才插一句，接不上就闭嘴。'
        '不硬找话题，不复述别人刚说过的话，不连着自说自话。')
    alternates: tuple[str, ...] = ()
    alternate_probability: float = 0.0
    # 群里对它的其他叫法；判断「是不是在叫我」和告诉模型自己叫什么都要用
    aliases: tuple[str, ...] = ()

    def identity(self, name: str) -> str:
        """ expressor 开头的人设段。 """
        also = f'，也有人叫你{"、".join(self.aliases)}' if self.aliases else ''
        return f'你的名字是{name}{also}。{self.personality}'

    def maybe_alternate(self, rng: random.Random | None = None) -> str:
        """ 按概率抽一条本次的临时风格；没抽中返回空串。 """
        if not self.alternates or self.alternate_probability <= 0:
            return ''
        rng = rng or random
        if rng.random() > self.alternate_probability:
            return ''
        return rng.choice(self.alternates)


def _self_check() -> None:
    persona = Persona()
    assert persona.identity('示例名') == '你的名字是示例名。是一个积极乐观、温暖阳光的女生，03 年出生。'
    nicknamed = Persona(aliases=('雨姐', '烟姐'))
    assert nicknamed.identity('雨墨蝉烟') == (
        '你的名字是雨墨蝉烟，也有人叫你雨姐、烟姐。是一个积极乐观、温暖阳光的女生，03 年出生。')
    # 没配备用风格时永远是空
    assert persona.maybe_alternate() == ''

    talkative = Persona(alternates=('用1-2个字回复', '带点翻译腔'), alternate_probability=1.0)
    assert talkative.maybe_alternate() in talkative.alternates

    never = Persona(alternates=('x',), alternate_probability=0.0)
    assert never.maybe_alternate() == ''

    import random as random_module
    seeded = random_module.Random(0)
    always = Persona(alternates=('only',), alternate_probability=1.0)
    assert always.maybe_alternate(seeded) == 'only'
    print('persona self-check ok')


if __name__ == '__main__':
    _self_check()
