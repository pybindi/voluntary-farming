"""Regenerate the README image with illustrative data; never touches GPU or network."""
import io
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mining_console import DashboardState
from rich.console import Console
from rich.terminal_theme import TerminalTheme


state = DashboardState()
state.update(dict(status='DEMO · illustrative data · no GPU workload',
                  pool='etc.2miners.com:11010',
                  sample=dict(name='NVIDIA GeForce RTX 5060', temp=51, power=123),
                  seconds=32400, started=100))
for speed in (48.3, 49.8, 50.9, 51.2, 50.7, 51.4, 51.1, 51.3):
    state.feed(f'Average speed (15s): {speed:.2f} Mh/s\n')
state.feed('GPU 0: Share accepted (28 ms)\n'
           'New job received: Epoch 854 / ETCHASH\n'
           'GPU 0: Share accepted (24 ms)\n')
console = Console(file=io.StringIO(), record=True, width=120, force_terminal=True,
                  color_system='truecolor', legacy_windows=False)
console.print(state.render(now=7300, width=120, log_rows=5))
theme = TerminalTheme((11, 18, 32), (203, 213, 225),
                      [(11, 18, 32), (253, 164, 175), (134, 239, 172), (252, 211, 77),
                       (103, 232, 249), (196, 181, 253), (103, 232, 249), (203, 213, 225)])
svg = console.export_svg(title='Voluntary Farming / DEMO - no GPU workload', theme=theme)
Path(__file__).with_name('dashboard.svg').write_text(
    '\n'.join(line.rstrip() for line in svg.splitlines()) + '\n', encoding='utf-8')
print('Generated docs/dashboard.svg (illustrative sample data).')
