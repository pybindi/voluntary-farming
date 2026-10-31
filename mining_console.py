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


def duration(seconds):
    seconds = max(0, int(seconds))
    return f'{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02}'


class DashboardState:
    def __init__(self):
        self.status = 'Подготовка · майнер ещё не запущен'
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
                if self.finished is None and self.status.startswith(('Процесс', 'Майнинг')):
                    self.status = 'Майнинг · получен хешрейт от майнера'
            if 'share accepted' in line.lower():
                self.accepted += 1
            if 'share rejected' in line.lower():
                self.rejected += 1

    def render(self, now=None, log_rows=10):
        now = time.monotonic() if now is None else now
        elapsed = max(0, (self.finished or now) - self.started) if self.started is not None else 0
        remaining = max(0, self.seconds - elapsed)
        sample = self.sample
        temp = sample.get('temp')
        temperature = '—' if temp is None else f'{temp:.0f} °C'
        temp_style = 'red bold' if temp is not None and temp >= self.stop_temp - 5 else 'green'
        header = Text('GPU MINING', style='bold cyan')
        header.append('  /  ETC · ETCHASH', style='dim')
        grid = Table.grid(expand=True, padding=(0, 2))
        grid.add_column(ratio=1)
        grid.add_column(ratio=1)
        grid.add_row(Text(sample.get('name', 'NVIDIA GPU'), style='bold white'), Text(self.status, style='cyan'))
        grid.add_row(Text('Скорость: ' + self.speed, style='bold cyan'),
                     Text(f'Шары A/R: {self.accepted} / {self.rejected}', style='green'))
        grid.add_row(Text('Ядро: ' + temperature, style=temp_style),
                     Text(f'Лимит GPU: {sample.get("power", "—")} Вт', style='yellow'))
        grid.add_row(Text(f'Прошло: {duration(elapsed)}'), Text(f'До остановки: {duration(remaining)}'))
        grid.add_row(Text('Пул: ' + self.pool, style='dim'),
                     Text(f'Защита: {self.stop_temp:g} °C · без перезапуска', style='dim'))
        progress = ProgressBar(total=max(1, self.seconds), completed=min(elapsed, self.seconds),
                               complete_style='cyan', finished_style='green')
        rows = list(self.lines)[-log_rows:]
        tail = ANSI.sub('', self.partial).strip()
        if tail:
            rows = (rows + [tail[:500]])[-log_rows:]
        log = Text()
        for line in rows:
            lowered = line.lower()
            style = ('red' if any(word in lowered for word in ('error', 'failed', 'rejected')) else
                     'green' if 'accepted' in lowered else 'white')
            log.append(line + '\n', style=style)
        if not rows:
            log.append('Ожидание сообщений. Подтвердите UAC, если он появился.', style='dim')
        return Group(
            Panel(Group(header, Text(''), grid, Text(''), progress), border_style='cyan', box=box.ROUNDED),
            Panel(log, title='Сообщения майнера и контроллера', border_style='bright_black', box=box.ROUNDED),
            Text('Ctrl+C — остановить и восстановить мощность  •  Полный журнал: data/mining/miner.log', style='dim'),
            Text('Шары ≠ выплата. Доход и баланс кошелька здесь не рассчитываются.', style='dim'),
        )


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
                    rows = max(1, min(12, self.console.height - 16))
                    live.update(state.render(log_rows=rows), refresh=True)
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
    demo.update(dict(status='ДЕМО · без запуска GPU', pool='etc.2miners.com:11010',
                     sample=dict(name='NVIDIA GeForce RTX 5060', temp=51, power=123),
                     seconds=590, started=100))
    demo.feed('Average speed (15s): 51.37 Mh/s\nGPU 0: Share accepted (28 ms)\n')
    demo.status = 'ДЕМО · без запуска GPU'
    Console().print(demo.render(now=220))
