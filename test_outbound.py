""" OutboundQueue 自检：顺序、最小间隔、失败不中断。 """

import asyncio
import unittest

from outbound import OutboundQueue


class TestOutboundQueue(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sent: list[tuple[float, str]] = []

    async def _send(self, item):
        self.sent.append((asyncio.get_running_loop().time(), item.text))

    async def test_sends_in_order_and_respects_interval(self):
        queue = OutboundQueue(self._send, min_interval=0.05)
        await queue.start()
        for i in range(3):
            queue.put('u', f'm{i}')
        await asyncio.sleep(0.4)
        await queue.stop()

        self.assertEqual([text for _, text in self.sent], ['m0', 'm1', 'm2'])
        gaps = [b[0] - a[0] for a, b in zip(self.sent, self.sent[1:], strict=False)]
        self.assertTrue(all(g >= 0.045 for g in gaps), gaps)

    async def test_send_failure_does_not_break_worker(self):
        async def send(item):
            if item.text == 'boom':
                raise RuntimeError('relay down')
            await self._send(item)

        queue = OutboundQueue(send, min_interval=0)
        await queue.start()
        queue.put('u', 'boom')
        queue.put('u', 'ok')
        await asyncio.sleep(0.05)
        await queue.stop()

        self.assertEqual([text for _, text in self.sent], ['ok'])

    async def test_typing_delay_applied(self):
        queue = OutboundQueue(self._send, min_interval=0, typing_delay=lambda text: 0.05)
        await queue.start()
        queue.put('u', 'hi')
        await asyncio.sleep(0.02)
        self.assertEqual(self.sent, [])  # 延迟未到，尚未发送
        await asyncio.sleep(0.1)
        await queue.stop()
        self.assertEqual([text for _, text in self.sent], ['hi'])


if __name__ == '__main__':
    unittest.main()
