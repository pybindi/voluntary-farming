"""Optional Rich mining dashboard. Rendering never runs in the safety loop."""
from collections import deque
from contextlib import contextmanager
import logging
import queue
import re
import sys
import threading
import time

try:
    from rich import box
    from rich.console import Console, Group
    from rich.live import Live
    from rich.panel import Panel
    from rich.progress_bar import ProgressBar
    from rich.table import Table
    from rich.text import Text
except ImportError:
    Console = None


ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')
BG = '#0b1220'
CYAN = '#67e8f9'
MUTED = '#94a3b8'
GREEN = '#86efac'
AMBER = '#fcd34d'
RED = '#fda4af'


def metric(label, value, caption, accent=CYAN):
    return Panel(Group(Text(label, style=f'bold {MUTED}'), Text(value, style=f'bold {accent}'),
                       Text(caption, style=MUTED, overflow='ellipsis', no_wrap=True)),
                 border_style='#334155', style=f'on {BG}', padding=(0, 1), box=box.ROUNDED)


def duration(seconds):
    seconds = max(0, int(seconds))
    return f'{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02}'


class DashboardState:
    def __init__(self):
        self.status = 'Preparing · miner has not started'
        self.sample = {}
        self.pool = '—'
        self.cap = None
        self.stop_temp = 70
        self.seconds = 0
        self.started = None
        self.finished = None
        self.speed = '—'
        self.accepted = 0
        self.rejected = 0
        self.speed_history = deque(maxlen=24)
        self.lines = deque(maxlen=60)
        self.partial = ''

    def update(self, values):
        for key, value in values.items():
            setattr(self, key, value)

    def feed(self, text):
        # Chunk boundaries may split ANSI escapes, Unicode or entire log lines.
        self.partial += text
        parts = re.split(r'\r\n|[\r\n]', self.partial)
        self.partial = parts.pop()[-8192:]
        for part in parts:
            line = ANSI.sub('', part).strip()
            if not line:
                continue
            self.lines.append(line[:500])
            match = re.search(r'Average speed \([^)]*\):\s*([\d.]+)\s*Mh/s', line, re.I)
            if match:
                self.speed = match[1] + ' MH/s'
                self.speed_history.append(float(match[1]))
                if self.finished is None and self.status.startswith(('Process', 'Mining')):
                    self.status = 'Mining · hashrate reported by miner'
            if 'share accepted' in line.lower():
                self.accepted += 1
            if 'share rejected' in line.lower():
                self.rejected += 1

    def render(self, now=None, log_rows=10, width=110, compact=False):
        now = time.monotonic() if now is None else now
        end = now if self.finished is None else self.finished
        elapsed = max(0, end - self.started) if self.started is not None else 0
        remaining = max(0, self.seconds - elapsed)
        sample = self.sample
        temp = sample.get('temp')
        temperature = '—' if temp is None else f'{temp:.0f} °C'
        temp_style = RED if temp is not None and temp >= self.stop_temp - 5 else GREEN
        status_style = GREEN if self.finished is not None and self.status.startswith('Stopped') else CYAN
        if any(word in self.status.lower() for word in ('interrupted', 'error', 'stopping')):
            status_style = AMBER
        brand = Text('VOLUNTARY', style=f'bold {CYAN}')
        brand.append(' / FARMING', style='bold white')
        brand.append('    ETC · ETCHASH', style=MUTED)
        header = Group(brand, Text(self.status, style=status_style, overflow='ellipsis', no_wrap=True))
        items = [header]
        if compact:
            items.append(Text(f'{sample.get("name", "NVIDIA GPU")}  |  {self.speed}  |  {temperature}', style=CYAN))
            items.append(Text(f'GPU cap: {sample.get("power", "—")} W  ·  stop at {self.stop_temp:g} °C', style=MUTED))
            items.append(Text(f'Left {duration(remaining)}  ·  elapsed {duration(elapsed)}  ·  A/R {self.accepted}/{self.rejected}'))
        else:
            trend = 'Waiting for measurements'
            if self.speed_history:
                levels = '▁▂▃▄▅▆▇█'
                peak = max(self.speed_history) or 1
                trend = ''.join(levels[min(7, int(value / peak * 7))] for value in self.speed_history)
            cards = [metric('HASHRATE', self.speed, trend),
                     metric('GPU CORE', temperature, f'Stop at {self.stop_temp:g} °C', temp_style),
                     metric('GPU POWER CAP', f'{sample.get("power", "—")} W', 'Not whole-PC power draw', AMBER),
                     metric('ACCEPTED / REJECTED', f'{self.accepted} / {self.rejected}', 'This session · screen count', GREEN)]
            count = 4 if width >= 110 else 2
            metrics = Table.grid(expand=True, padding=(0, 1))
            for _ in range(count):
                metrics.add_column(ratio=1)
            for start in range(0, len(cards), count):
                metrics.add_row(*cards[start:start + count])
            items.extend([Text(''), metrics])
            session = Table.grid(expand=True)
            session.add_column(ratio=1)
            session.add_column(justify='right')
            session.add_row(Text('SESSION', style=f'bold {MUTED}'), Text('TIME LEFT  ' + duration(remaining), style=f'bold {CYAN}'))
            session.add_row(Text(f'Elapsed {duration(elapsed)}  /  limit {duration(self.seconds)}', style=MUTED),
                            Text('No automatic process restart', style=MUTED))
            session_bar = ProgressBar(total=max(1, self.seconds), completed=min(elapsed, self.seconds),
                                      style='#1e293b', complete_style='#0891b2', finished_style=GREEN)
            items.append(Panel(Group(session, session_bar), border_style='#334155',
                               style=f'on {BG}', box=box.ROUNDED, padding=(0, 1)))
            connection = Text(sample.get('name', 'NVIDIA GPU'), style='bold white')
            connection.append('  /  ' + self.pool, style=MUTED)
            items.append(connection)
        log_rows = max(1, log_rows)
        rows = list(self.lines)[-log_rows:]
        tail = ANSI.sub('', self.partial).strip()
        if tail:
            rows = (rows + [tail[:500]])[-log_rows:]
        log = Table.grid(expand=True, padding=(0, 1))
        log.add_column(width=7, no_wrap=True)
        log.add_column(ratio=1, overflow='ellipsis', no_wrap=True)
        for line in rows:
            lowered = line.lower()
            if any(word in lowered for word in ('error', 'failed', 'rejected')):
                tag, style = 'WARN', RED
            elif 'accepted' in lowered:
                tag, style = 'SHARE', GREEN
            elif 'speed' in lowered:
                tag, style = 'SPEED', CYAN
            elif 'new job' in lowered:
                tag, style = 'JOB', MUTED
            else:
                tag, style = 'INFO', '#cbd5e1'
            log.add_row(Text(tag, style=f'bold {style}'), Text(line, style=style))
        if not rows:
            log.add_row(Text('WAIT', style=AMBER), Text('Waiting for output. Approve the power helper UAC prompt if shown.', style=MUTED))
        items.append(Panel(log, title=Text(' EVENT STREAM ', style=MUTED), title_align='left',
                           border_style='#334155', style=f'on {BG}', box=box.ROUNDED, padding=(0, 1)))
        footer = Text(' Ctrl+C ', style=f'bold {BG} on {CYAN}')
        footer.append('  stop mining and restore the power cap', style=MUTED)
        items.extend([footer, Text('Shares ≠ payouts · Screen counts are not your pool balance', style=MUTED)])
        if not compact:
            items.append(Text('Full log: data/mining/miner.log', style='#64748b'))
        return Panel(Group(*items), border_style='#155e75', style=f'on {BG}', padding=(0, 1),
                     box=box.ROUNDED, subtitle=Text('voluntary author support · pybindi', style='#64748b'))


class DashboardHandler(logging.Handler):
    def __init__(self, dashboard):
        super().__init__()
        self.dashboard = dashboard

    def emit(self, record):
        self.dashboard.write(self.format(record) + '\n')


class MiningConsole:
    def __init__(self, console=None):
        self.console = console if console is not None else (Console() if Console is not None else None)
        self.active = self.console is not None and self.console.is_terminal
        self.events = queue.Queue(maxsize=256)
        self.stopped = threading.Event()
        self.thread = None

    def __enter__(self):
        if self.active:
            self.thread = threading.Thread(target=self._run, daemon=True, name='mining-dashboard')
            self.thread.start()
        return self

    def __exit__(self, *args):
        self.stopped.set()
        if self.thread is not None:
            self.thread.join(timeout=0.3)

    @contextmanager
    def logging(self):
        logger = logging.getLogger()
        previous = list(logger.handlers)
        if self.active:
            handler = DashboardHandler(self)
            handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s', '%H:%M:%S'))
            logger.handlers = [item for item in previous if isinstance(item, logging.FileHandler)] + [handler]
        try:
            yield
        finally:
            logger.handlers = previous

    def _send(self, kind, value):
        if self.active:
            try:
                self.events.put_nowait((kind, value))
            except queue.Full:
                pass  # Only screen data is expendable; raw logging is separate.

    def update(self, **values):
        self._send('update', values)

    def write(self, text):
        if self.active:
            self._send('text', text)
        else:
            sys.stdout.write(text)

    def flush(self):
        if not self.active:
            sys.stdout.flush()

    def _run(self):
        state = DashboardState()
        try:
            # All terminal I/O, including Live shutdown, belongs to this daemon.
            with Live(console=self.console, auto_refresh=False, redirect_stdout=False,
                      redirect_stderr=False, vertical_overflow='crop') as live:
                while True:
                    for _ in range(256):
                        try:
                            kind, value = self.events.get_nowait()
                        except queue.Empty:
                            break
                        if kind == 'update':
                            state.update(value)
                        else:
                            state.feed(value)
                    width, height = self.console.width, self.console.height
                    compact = width < 64 or height < (24 if width >= 110 else 29)
                    overhead = 13 if compact else 23 if width >= 110 else 28
                    rows = max(1, min(12, height - overhead))
                    live.update(state.render(log_rows=rows, width=width, compact=compact), refresh=True)
                    if self.stopped.is_set() and self.events.empty():
                        break
                    self.stopped.wait(0.2)
        except Exception:
            # Rendering failure must never prevent workload shutdown/recovery.
            pass


if __name__ == '__main__':
    if Console is None:
        sys.exit('Install Rich: python -m pip install -r requirements-mining.txt')
    demo = DashboardState()
    demo.update(dict(status='DEMO · sample data · no GPU workload', pool='etc.2miners.com:11010',
                     sample=dict(name='NVIDIA GeForce RTX 5060', temp=51, power=123),
                     seconds=590, started=100))
    demo.feed('Average speed (15s): 51.37 Mh/s\nGPU 0: Share accepted (28 ms)\n')
    demo.status = 'DEMO · sample data · no GPU workload'
    console = Console()
    console.print(demo.render(now=220, width=console.width, compact=console.width < 64))
