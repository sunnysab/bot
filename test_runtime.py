""" Runtime 热重载自检：**改配置不能把运行状态清掉**。

这是 P2 的验收条件——面板上改 TYPO_RATE 之后，正在进行的专注会话必须还在
（状态栏的轮数不能归零）。所以这里不去断言「配置生效了」（那太容易），
而是先造出运行状态，再热重载，然后检查那些状态有没有被顺手清掉。
"""

import asyncio
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from config import Config
from runtime import ReplyScope, Runtime


def make_config(tmp: str, **overrides) -> Config:
    """ 全部指到临时目录，别碰仓库里的 media_cache.db / memory/ 那些文件。 """
    base = Path(tmp)
    return Config(deepseek_key='test-key',
                  media_cache_path=str(base / 'media.db'),
                  moment_path=str(base / 'moments.db'),
                  emoji_path=str(base / 'emojis.json'),
                  profile_path=str(base / 'profiles.json'),
                  habit_path=str(base / 'habits.json'),
                  scope_path=str(base / 'reply_scope.json'),
                  trace_path=str(base / 'trace.jsonl'),
                  **overrides)


class TestRuntimeReload(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.runtime = Runtime(make_config(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def seed_state(self):
        """ 造出运行状态：一个专注会话 + 两次沉默 + 刚发过一张表情。 """
        rt = self.runtime
        rt.focus.enter('测试群', 1000.0, '手动')
        rt.focus.sessions['测试群'].rounds = 5
        rt.backoff.record('测试群', spoke=False, now=1000.0, history=[])
        rt.backoff.record('测试群', spoke=False, now=1001.0, history=[])
        rt.emoji_policy.note('some-md5', 1000.0)

    def test_apply_keeps_focus_backoff_and_emoji_cooldown(self):
        self.seed_state()
        before = self.runtime.focus.sessions['测试群']
        old_plugin = self.runtime.chat_plugin

        stale = self.runtime.apply(replace(self.runtime.config, typo_rate=0.0,
                                           focus_enter_activity=20, enable_focus=False))

        self.assertEqual(stale, [])
        # 专注会话还在，轮数没归零
        self.assertIs(self.runtime.focus.sessions['测试群'], before)
        self.assertEqual(self.runtime.focus.sessions['测试群'].rounds, 5)
        # 常数是就地改的
        self.assertEqual(self.runtime.focus.enter_activity, 20)
        self.assertFalse(self.runtime.focus.enabled)
        # 退避计数还在（还剩 15s）
        self.assertEqual(self.runtime.backoff.pending(1001.0), {'测试群': 15.0})
        # 表情冷却还在
        self.assertIn('刚发过', self.runtime.emoji_policy.refuses('some-md5', 1001.0))
        # 插件链换了（不重建的话配置改了不生效）
        self.assertIsNot(self.runtime.chat_plugin, old_plugin)
        self.assertEqual(self.runtime.bot.typo_rate, 0.0)
        self.assertEqual(self.runtime.chat_plugin.policy.threshold,
                         self.runtime.config.reply_necessity_threshold)
        # 共享对象确实传下去了，不是新造的一个
        self.assertIs(self.runtime.chat_plugin.policy.focus, self.runtime.focus)
        self.assertIs(self.runtime.chat_plugin.policy.backoff, self.runtime.backoff)
        self.assertIs(self.runtime.chat_plugin.emoji_policy, self.runtime.emoji_policy)

    def test_history_load_matches_model_window(self):
        """ 启动载入的条数要跟模型窗口（reply_context_length）对齐：载 30 条的话，
        模型该看到的前半段是空的，里面的图也就永远没机会被描述。 """
        calls = []

        async def fake_load(window_size=0):
            calls.append(window_size)

        self.runtime.bot.load_context = fake_load
        asyncio.run(self.runtime._load_history())
        self.assertEqual(calls, [self.runtime.config.reply_context_length])

    def test_apply_reports_fields_that_need_a_process_restart(self):
        stale = self.runtime.apply(replace(self.runtime.config, daemon_port=1234, typo_rate=0.0))
        self.assertEqual(stale, ['daemon_port'])      # 热重载管不着传输层

    def test_dry_run_toggle_reaches_both_objects(self):
        """ 演习开关：Bot 决定写不写历史/发不发事件，Wechat 决定真发还是只打日志。 """
        self.runtime.apply(replace(self.runtime.config, dry_run=True))
        self.assertTrue(self.runtime.bot.dry_run)
        self.assertTrue(self.runtime.bot.wechat.dry_run)

    def test_default_muted_contact_is_idempotent(self):
        """ 每次热重载都会重装插件链——静音不能越装越长。 """
        for _ in range(3):
            self.runtime.apply(self.runtime.config)
        plugins = self.runtime.bot._plugins.get_plugins_for_contact('研究生摆烂群')
        self.assertEqual(len(plugins), 1, plugins)
        self.assertTrue(self.runtime.bot.is_muted('研究生摆烂群'))
        self.assertFalse(self.runtime.bot.is_muted('测试群'))
    def test_reply_scope_controls_who_gets_replies(self):
        """ 面板上的「允许回复」：模式 + 名单要真落到插件链上，而且要活过重启。 """
        rt = self.runtime
        # 默认 mode='all'：种子名单里的静音，其余回
        self.assertTrue(rt.bot.is_muted('研究生摆烂群'))
        self.assertFalse(rt.bot.is_muted('测试群'))

        # 显式关掉一个 → 立刻静音；热重载重装插件链后不能把它忘掉
        rt.reply_scope.set_allowed('测试群', False)
        rt.install_scope()
        self.assertTrue(rt.bot.is_muted('测试群'))
        rt.apply(rt.config)
        self.assertTrue(rt.bot.is_muted('测试群'))

        # 切白名单：没表过态的一律不回，勾上的才回
        rt.reply_scope.set_mode('allow')
        rt.reply_scope.set_allowed('测试群', True)
        rt.install_scope()
        self.assertFalse(rt.bot.is_muted('测试群'))
        self.assertTrue(rt.bot.is_muted('没勾过的群'))

        # 落盘：光是新建一个对象就能读回来，进程重启也不丢
        again = ReplyScope(rt.reply_scope.path)
        self.assertEqual(again.mode, 'allow')
        self.assertTrue(again.allows('测试群'))
        self.assertFalse(again.allows('没勾过的群'))


if __name__ == '__main__':
    unittest.main()
