""" 日期与日历。

给决策者的两件事：
1. `now_line()` —— 请求正文末尾注入的「现在」，补上聊天记录缺的年份和星期；
2. `calendar_text()` —— calendar 工具的实体逻辑，渲染目标所在整月的逐日农历。

农历走 lunar-python（纯 Python、零依赖，覆盖 1900-2100，含闰月 / 节气 / 传统节日）。
工具说明只写在 plugin.py 的 ToolSpec.description 里——模型在 tools 参数里就看得到，
系统提示里不必再抄一遍。
"""

from __future__ import annotations

import calendar
import time
from datetime import date, datetime

from lunar_python import Solar

MIN_YEAR = 1900          # lunar-python 的农历数据范围
MAX_YEAR = 2100
_WEEKDAYS = '一二三四五六日'


def _weekday(moment: date) -> str:
    return f'星期{_WEEKDAYS[moment.isoweekday() - 1]}'


def now_line(ts: float | None = None) -> str:
    """ 一行「现在」，如 `2026-10-05 14:32:07 星期一 · 农历八月廿五`。

    聊天记录里的时间戳只有 `MM-DD HH:MM:SS`，没有年份也没有星期；模型靠这行才知道
    此刻到底是什么时候。
    """
    moment = datetime.fromtimestamp(time.time() if ts is None else ts)
    lunar = Solar.fromYmd(moment.year, moment.month, moment.day).getLunar()
    return (f'{moment:%Y-%m-%d %H:%M:%S} {_weekday(moment)}'
            f' · 农历{lunar.getMonthInChinese()}月{lunar.getDayInChinese()}')


def calendar_text(when: str = '', today: date | None = None) -> str:
    """ calendar 工具的输出：目标所在整月的逐日农历。

    :param when: `2026-10-05`（某天）、`2026-10`（某月）；留空 = 今天
    :param today: 自检注入用；默认取当天
    :raises ValueError: 写法看不懂，或年份超出 1900-2100
    """
    today = today or date.today()
    month, target = _resolve(when, today)
    days = calendar.monthrange(month.year, month.month)[1]
    lines = [f'{month.year} 年 {month.month} 月（{_year_name(month)}，共 {days} 天）']
    lines.extend(_render_day(date(month.year, month.month, day), target, today)
                 for day in range(1, days + 1))
    return '\n'.join(lines)


def _resolve(when: str, today: date) -> tuple[date, date | None]:
    """ when → (要渲染的月份第一天, 要标记的目标日)；留空 = 今天所在的月。

    只认 `YYYY-MM-DD` / `YYYY-MM` 两种写法。相对词（明天、下周三）不解析——
    模型拿到整月日历自己找，多一种说法就多一段解析和一处会错的地方。
    """
    text = (when or '').strip()
    if not text:
        return today.replace(day=1), today
    if text.count('-') == 1:                        # 2026-10
        return _parse(text, '%Y-%m'), None
    day = _parse(text, '%Y-%m-%d')                  # 2026-10-05
    return day.replace(day=1), day


def _parse(text: str, fmt: str) -> date:
    try:
        moment = datetime.strptime(text, fmt).date()
    except ValueError:
        raise ValueError(f'看不懂「{text}」，用 2026-10-05 或 2026-10 这种写法。') from None
    if not MIN_YEAR <= moment.year <= MAX_YEAR:
        raise ValueError(f'只查得到 {MIN_YEAR} 到 {MAX_YEAR} 年。')
    return moment


def _year_name(month: date) -> str:
    """ 该月的农历年，如「丙午马年」。取月初：公历 1、2 月的农历年才是对的。 """
    lunar = Solar.fromYmd(month.year, month.month, month.day).getLunar()
    return f'{lunar.getYearInGanZhi()}{lunar.getYearShengXiao()}年'


def _render_day(day: date, target: date | None, today: date) -> str:
    solar = Solar.fromYmd(day.year, day.month, day.day)
    lunar = solar.getLunar()
    tags = [*lunar.getFestivals(), *solar.getFestivals()]
    if jieqi := lunar.getJieQi():
        tags.append(jieqi)
    if target == day:
        mark = '  ← 今天' if day == today else '  ← 你要查的'
    else:
        mark = ''
    suffix = f' · {" · ".join(tags)}' if tags else ''
    return (f'- {day:%m-%d} {_weekday(day)} 农历'
            f'{lunar.getMonthInChinese()}月{lunar.getDayInChinese()}{suffix}{mark}')


def _self_check() -> None:
    ts = datetime(2026, 10, 5, 14, 32, 7).timestamp()
    assert now_line(ts) == '2026-10-05 14:32:07 星期一 · 农历八月廿五', now_line(ts)

    month = calendar_text(today=date(2026, 10, 5))
    assert month.startswith('2026 年 10 月（丙午马年，共 31 天）'), month.splitlines()[0]
    assert len(month.splitlines()) == 32, len(month.splitlines())   # 标题 + 31 天
    assert '- 10-05 星期一 农历八月廿五' in month
    assert '  ← 今天' in month
    assert '国庆节' in month and '寒露' in month                     # 公历节日 + 节气

    # 指定某天 / 某月；只给月份时不标目标日
    day = calendar_text('2026-09-25')
    assert '2026 年 9 月' in day and '中秋节' in day and '  ← 你要查的' in day
    assert '←' not in calendar_text('2026-10')
    assert '闰六月初一' in calendar_text('2025-07')                  # 闰月
    assert calendar_text('1900-01').startswith('1900 年 1 月')       # 边界年份
    assert calendar_text('2100-12').startswith('2100 年 12 月')

    # 写法看不懂 / 超范围都要给一句人话（工具层只把它当成一条给模型看的结果）
    for bad in ('明天', '下周三', '2026/10/05', '2026-13-01', '1899-12', '2101-01'):
        try:
            calendar_text(bad)
        except ValueError as e:
            assert str(e), bad
        else:
            raise AssertionError(f'应该报错的输入没报错：{bad}')

    print('dates self-check ok')


if __name__ == '__main__':
    _self_check()
