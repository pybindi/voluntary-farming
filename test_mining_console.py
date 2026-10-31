"""Dashboard checks without GPU access, network, or mining."""
import io
import logging
import queue
import threading
import time
import unittest
from unittest.mock import Mock, patch

import mining_console as ui


class StateTests(unittest.TestCase):
    def test_chunked_ansi_and_share_counts(self):
        state = ui.DashboardState()
        state.feed('\x1b[3')
        state.feed('2mAverage speed (15s): 51.')
        state.feed('37 Mh/s\x1b[0m\rGPU 0: Share accepted (28 ms)\n')
        state.feed('GPU 0: Share rejected\n')
        self.assertEqual(state.speed, '51.37 MH/s')
        self.assertEqual((state.accepted, state.rejected), (1, 1))
        self.assertNotIn('\x1b', ''.join(state.lines))

    def test_unterminated_and_long_output_is_bounded(self):
        state = ui.DashboardState()
        state.feed('x' * 100000)
        self.assertEqual(len(state.partial), 8192)
        state.feed('\n' + 'line\n' * 1000)
        self.assertEqual(len(state.lines), 60)

    @unittest.skipIf(ui.Console is None, 'Rich not installed')
    def test_render_is_literal_and_contains_limits_and_timer(self):
        state = ui.DashboardState()
        state.update(dict(sample=dict(name='RTX 5060', temp=51, power=123), seconds=590, started=100))
        state.feed('[link=https://bad.example]literal[/link]\n')
        output = io.StringIO()
        ui.Console(file=output, width=110, force_terminal=False).print(state.render(now=220))
        text = output.getvalue()
        for expected in ('RTX 5060', '51 °C', '123 W', '00:02:00', '00:07:50', '70 °C', '[link='):
            self.assertIn(expected, text)

    def test_redirected_console_has_plain_output(self):
        output = io.StringIO()
        console = Mock(is_terminal=False)
        with patch.object(ui.sys, 'stdout', output), ui.MiningConsole(console) as screen:
            screen.write('raw output\n')
            screen.flush()
            self.assertIsNone(screen.thread)
        self.assertEqual(output.getvalue(), 'raw output\n')

    @unittest.skipIf(ui.Console is None, 'Rich not installed')
    def test_layout_fits_wide_standard_and_compact_windows(self):
        state = ui.DashboardState()
        state.update(dict(sample=dict(name='RTX 5060', temp=51, power=123), seconds=590, started=100))
        state.feed(('GPU 0: Share accepted (28 ms) ' + 'x' * 300 + '\n') * 12)
        for width, height, compact, rows in [(120, 35, False, 12), (80, 30, False, 2),
                                             (80, 24, True, 8), (60, 22, True, 4)]:
            with self.subTest(width=width):
                output = io.StringIO()
                ui.Console(file=output, width=width, force_terminal=False).print(
                    state.render(now=220, width=width, compact=compact, log_rows=rows))
                lines = output.getvalue().splitlines()
                self.assertLessEqual(len(lines), height)
                self.assertTrue(all(len(line) <= width for line in lines))
                self.assertIn('Ctrl+C', output.getvalue())
                self.assertIn('51 °C', output.getvalue())

    def test_speed_history_is_bounded_and_uses_only_reported_values(self):
        state = ui.DashboardState()
        for value in range(100):
            state.feed(f'Average speed (15s): {value}.00 Mh/s\n')
        self.assertEqual(len(state.speed_history), 24)
        self.assertEqual(state.speed_history[-1], 99)

    @unittest.skipIf(ui.Console is None, 'Rich not installed')
    def test_hot_temperature_and_unknown_data_are_not_hidden(self):
        state = ui.DashboardState()
        state.sample = dict(temp=69, power=123)
        render = state.render(now=0)
        output = io.StringIO()
        ui.Console(file=output, width=120, force_terminal=True, color_system='truecolor').print(render)
        self.assertIn('69 °C', output.getvalue())
        self.assertIn('Stop at 70 °C', ui.ANSI.sub('', output.getvalue()))

    def test_missing_rich_falls_back(self):
        with patch.object(ui, 'Console', None):
            self.assertFalse(ui.MiningConsole().active)

    @unittest.skipIf(ui.Console is None, 'Rich not installed')
    def test_blocked_renderer_cannot_block_context_exit(self):
        entered, release = threading.Event(), threading.Event()
        def blocked(*args, **kwargs):
            entered.set()
            release.wait(5)
            return ui.Text('done')
        console = ui.Console(file=io.StringIO(), force_terminal=True)
        with patch.object(ui.DashboardState, 'render', side_effect=blocked):
            screen = ui.MiningConsole(console)
            try:
                screen.__enter__()
                self.assertTrue(entered.wait(2))
                begin = time.monotonic()
                screen.__exit__(None, None, None)
                self.assertLess(time.monotonic() - begin, 0.8)
            finally:
                release.set()
                screen.thread.join(timeout=2)

    def test_late_miner_message_cannot_overwrite_restored_status(self):
        state = ui.DashboardState()
        state.update(dict(status='Stopped · original power cap restored', finished=200))
        state.feed('Average speed (15s): 51.37 Mh/s\n')
        self.assertTrue(state.status.startswith('Stopped'))

    def test_queue_never_blocks_when_full(self):
        screen = ui.MiningConsole(Mock(is_terminal=True))
        screen.events = queue.Queue(maxsize=1)
        screen.write('first')
        begin = time.monotonic()
        for _ in range(1000):
            screen.update(status='anything')
        self.assertLess(time.monotonic() - begin, 0.5)

    @unittest.skipIf(ui.Console is None, 'Rich not installed')
    def test_live_output_and_logging_handlers_are_restored(self):
        output = io.StringIO()
        console = ui.Console(file=output, force_terminal=True, width=100, height=35)
        previous = list(logging.getLogger().handlers)
        with ui.MiningConsole(console) as screen, screen.logging():
            screen.update(status='TEST STATUS')
            screen.write('Average speed (15s): 51.00 Mh/s\n')
        self.assertEqual(logging.getLogger().handlers, previous)
        self.assertFalse(screen.thread.is_alive())
        self.assertIn('51.00 MH/s', output.getvalue())


if __name__ == '__main__':
    unittest.main(verbosity=2)
