""" 专注模式（HFC / focus chat）：群里聊得正热的时候，机器人盯着这个群水一会儿。

MaiBot 那套 focus_chat 拆了七八个处理器（观察器、工作记忆、自我认知、动作决策器、
表达器……），这里不抄——我们的流水线本来就够用，缺的只是「什么时候别省那次模型调用」。

所以在既有门控上加一层状态机，只改三件事：
  - **进入条件**：活动窗口里别人说了够多话（默认 15 条），说明这会儿聊得起来
  - **专注期间**：门控直接放行、不再连续沉默退避——每条都过决策者，参与感更强
  - **退出条件**：这么久没新消息（默认 5 分钟）、或者单次专注到头了（默认 30 分钟），
    退出后还有一段冷却，免得刚出去又立刻被活动度拽回来（来回横跳）

**工作记忆暂缺**：MaiBot 会另起一次调用把当前话题压成工作记忆，我们直接用已有的
滑动窗口 + 中期记忆，够模型看，省一次调用。
ponytail: 真觉得专注期间「聊到一半忘了前面」再加一个话题摘要器，别提前上。
"""

from __future__ import annotations

from dataclasses import dataclass

from loguru import logger

FOCUS_ENTER_ACTIVITY = 15      # 活动窗口内别人的消息数，够热就进
FOCUS_IDLE_SECONDS = 300.0     # 这么久没有新消息就自己出来
FOCUS_MAX_SECONDS = 1800.0     # 单次专注最长多久（花钱的上限）
FOCUS_COOLDOWN_SECONDS = 300.0  # 自动退出后这么久内不再自动进（防横跳）


@dataclass
class FocusSession:
    """ 一次专注。 """

    started: float
    last_message: float
    reason: str
    rounds: int = 0

    def expired(self, now: float, idle: float, maximum: float) -> str:
        """ 该退出了返回原因，还早返回空串。 """
        if now - self.last_message >= idle:
            return 'idle'
        if now - self.started >= maximum:
            return 'maximum'
        return ''


class FocusMode:
    """ 按群维护专注状态。所有时间都用 epoch 秒，和 history 里的时间戳同源。 """

    def __init__(self, enabled: bool = True, enter_activity: int = FOCUS_ENTER_ACTIVITY,
                 idle: float = FOCUS_IDLE_SECONDS, maximum: float = FOCUS_MAX_SECONDS,
                 cooldown: float = FOCUS_COOLDOWN_SECONDS):
        self.enabled = enabled
        self.enter_activity = enter_activity
        self.idle = idle
        self.maximum = maximum
        self.cooldown = cooldown
        self.sessions: dict[str, FocusSession] = {}
        self._blocked_until: dict[str, float] = {}

    def tick(self, contact: str, now: float, activity: int = 0) -> bool:
        """ 每条消息过一遍：该进就进、该出就出，返回这一轮算不算专注。 """
        session = self.sessions.get(contact)
        if session is None:
            if not self.enabled or activity < self.enter_activity:
                return False
            if now < self._blocked_until.get(contact, 0.0):
                return False
            self.enter(contact, now, f'活跃度 {activity} 条')
            session = self.sessions[contact]
        else:
            reason = session.expired(now, self.idle, self.maximum)
            if reason:
                self.exit(contact, now, reason)
                return False
        session.last_message = now
        session.rounds += 1
        return True

    def enter(self, contact: str, now: float, reason: str = '手动') -> None:
        self.sessions[contact] = FocusSession(started=now, last_message=now, reason=reason)
        logger.info(f'进入专注模式：{contact}（{reason}）')

    def exit(self, contact: str, now: float, reason: str, cooldown: bool = True) -> None:
        if self.sessions.pop(contact, None) is None:
            return
        if cooldown:
            self._blocked_until[contact] = now + self.cooldown
        logger.info(f'退出专注模式：{contact}（{reason}）')

    def describe(self, contact: str) -> str:
        """ 注入 prompt 的一句话：现在在专注，可以比平时主动。 """
        session = self.sessions.get(contact)
        if session is None:
            return ''
        rounds = session.rounds
        return (f'你正在专注跟这个群聊（已经聊了 {rounds} 轮），'
                f'可以比平时更主动一点，但别硬凑话题，接不上就闭嘴。')


def _self_check() -> None:
    now = 1000.0
    focus = FocusMode(enter_activity=3, idle=100.0, maximum=1000.0, cooldown=50.0)

    # 不够热不进去
    assert focus.tick('g', now, activity=2) is False
    assert 'g' not in focus.sessions

    # 够热就进，进去之后哪怕只有一条消息也算专注
    assert focus.tick('g', now, activity=3) is True
    assert focus.sessions['g'].reason == '活跃度 3 条'
    assert focus.tick('g', now + 10, activity=0) is True
    assert focus.sessions['g'].rounds == 2

    # 闲太久自己出来，而且有冷却期不能立刻再进
    assert focus.tick('g', now + 10 + 100, activity=0) is False   # 1110 退出，冷却到 1160
    assert 'g' not in focus.sessions
    assert focus.tick('g', now + 130, activity=99) is False        # 1130，还在冷却
    assert focus.tick('g', now + 170, activity=99) is True         # 1170，冷却过了

    # 单次专注封顶：到点自动退出
    capped = FocusMode(enter_activity=1, idle=10_000.0, maximum=60.0, cooldown=0.0)
    assert capped.tick('g', 0.0, activity=1) is True
    assert capped.tick('g', 59.0, activity=1) is True
    assert capped.tick('g', 60.0, activity=1) is False
    assert 'g' not in capped.sessions

    # 关掉就永远不进
    off = FocusMode(enabled=False, enter_activity=1, cooldown=0.0)
    assert off.tick('g', 0.0, activity=99) is False

    # 手动进入 / 手动退出不进冷却
    manual = FocusMode(enter_activity=99, cooldown=50.0)
    manual.enter('g', 0.0)
    assert 'g' in manual.sessions and '专注' in manual.describe('g')
    manual.exit('g', 1.0, '手动', cooldown=False)
    assert manual.tick('g', 2.0, activity=99) is True   # 手动退出没有冷却

    print('focus self-check ok')


if __name__ == '__main__':
    _self_check()
