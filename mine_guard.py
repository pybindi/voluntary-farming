"""Opt-in Windows GPU mining supervisor. No downloads or automatic restarts."""
from __future__ import annotations

import argparse
import codecs
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data' / 'mining'
JOURNAL = DATA / 'power-restore.json'
LOG = logging.getLogger('mining')


class GuardError(RuntimeError):
    pass


def number(value, low, high, name):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise GuardError(f'{name} must be between {low} and {high}')
    return value


def load_config(path=None):
    c = json.loads((path or ROOT / 'mining_config.json').read_text(encoding='utf-8'))
    if type(c.get('enabled')) is not bool:
        raise GuardError('enabled must be true or false')
    if not re.fullmatch(r'GPU-[0-9a-fA-F-]{36}', c.get('gpu_uuid', '')):
        raise GuardError('Set a specific gpu_uuid')
    number(c.get('power_limit_w'), 1, 123, 'power_limit_w')
    number(c.get('stop_temperature_c'), 40, 70, 'stop_temperature_c')
    number(c.get('max_hours'), 1 / 60, 9, 'max_hours')
    number(c.get('poll_seconds'), 0.5, 2, 'poll_seconds')
    if c.get('algorithm') != 'ETCHASH' or c.get('payout_coin') != 'ETC':
        raise GuardError('Initial template supports ETCHASH with ETC payouts only')
    return c


def ready_command(c):
    if not c['enabled']:
        raise GuardError('Mining is disabled. Complete setup before setting enabled=true')
    pool = c.get('pool', '')
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9.-]*:[0-9]{1,5}', pool):
        raise GuardError('Set a verified ETC pool TLS endpoint as hostname:port')
    if not 1 <= int(pool.rsplit(':', 1)[1]) <= 65535:
        raise GuardError('Invalid pool port')
    if not re.fullmatch(r'0x[0-9a-fA-F]{40}', c.get('wallet', '')):
        raise GuardError('Set your ETC payout address (0x + 40 hex characters), not TON')
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,32}', c.get('worker', '')):
        raise GuardError('Invalid worker name')
    digest = c.get('miner_sha256', '')
    if not re.fullmatch(r'[0-9a-fA-F]{64}', digest):
        raise GuardError('Set the SHA256 of the reviewed official lolMiner.exe')
    exe = (ROOT / c.get('miner_path', '')).resolve()
    if exe.name.lower() != 'lolminer.exe' or not exe.is_file():
        raise GuardError('Official lolMiner.exe is missing; nothing is downloaded automatically')
    if hashlib.sha256(exe.read_bytes()).hexdigest() != digest.lower():
        raise GuardError('Miner SHA256 mismatch')
    # One physical NVIDIA GPU is required, so lolMiner device 0 is unambiguous.
    return [str(exe), '--algo', 'ETCHASH', '--pool', pool,
            '--user', c['wallet'] + '.' + c['worker'], '--tls', 'on',
            '--devices', '0', '--watchdog', 'exit',
            '--tstop', str(c['stop_temperature_c']), '--tstart', '0']


class Nvidia:
    def __init__(self):
        self.exe = shutil.which('nvidia-smi')
        if not self.exe:
            raise GuardError('nvidia-smi not found')

    def call(self, args):
        try:
            return subprocess.run([self.exe, *args], check=True, capture_output=True,
                                  text=True, timeout=3).stdout.strip()
        except subprocess.CalledProcessError as exc:
            if exc.returncode == 4:
                raise GuardError('nvidia-smi: insufficient permissions (exit code 4); GPU control requires administrator rights') from exc
            raise GuardError(f'nvidia-smi failed (exit code {exc.returncode})') from exc
        except (subprocess.SubprocessError, OSError) as exc:
            raise GuardError('nvidia-smi failed or timed out') from exc

    def sample(self, uuid):
        text = self.call(['--query-gpu=uuid,name,power.min_limit,power.max_limit,power.limit,temperature.gpu',
                          '--format=csv,noheader,nounits'])
        rows = [line.split(',') for line in text.splitlines() if line.strip()]
        if len(rows) != 1 or len(rows[0]) != 6 or rows[0][0].strip() != uuid:
            raise GuardError('Expected exactly one NVIDIA GPU with the configured UUID')
        row = [v.strip() for v in rows[0]]
        try:
            low, high, power, temp = map(float, row[2:])
        except ValueError as exc:
            raise GuardError('GPU telemetry is unavailable') from exc
        if (not all(math.isfinite(v) for v in (low, high, power, temp))
                or not 0 < low <= power <= high or not 0 <= temp <= 120):
            raise GuardError('Invalid GPU telemetry')
        return dict(uuid=row[0], name=row[1], low=low, high=high, power=power, temp=temp)

    def set_power(self, uuid, watts):
        self.call(['-i', uuid, '-pl', str(watts)])


class PowerHelper:
    """One UAC prompt for a power-only helper; miner stays unelevated."""
    def __init__(self):
        self.folder = None

    def start(self, gpu_uuid, target, original):
        powershell = str(Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe')
        policy = subprocess.run([powershell, '-NoProfile', '-NonInteractive',
                                 '-ExecutionPolicy', 'RemoteSigned', '-Command', 'Get-ExecutionPolicy'],
                                check=True, capture_output=True, text=True, timeout=5).stdout.strip()
        if policy in ('Restricted', 'AllSigned'):
            raise GuardError(f'PowerShell policy {policy} blocks the unsigned local power helper; policy was not changed')
        self.folder = DATA / ('power-session-' + uuid.uuid4().hex)
        self.folder.mkdir(parents=True, exist_ok=False)
        script = ROOT / 'mining_power.ps1'
        args = ['-NoProfile', '-ExecutionPolicy', 'RemoteSigned', '-File', str(script), '-OwnerPid', str(os.getpid()),
                '-GpuUuid', gpu_uuid, '-TargetWatts', str(target),
                '-OriginalWatts', str(original), '-SessionDirectory', str(self.folder)]
        # Launch only the local power script with UAC, never the miner or Python.
        api = ctypes.WinDLL('shell32', use_last_error=True)
        api.ShellExecuteW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                     wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_int]
        api.ShellExecuteW.restype = ctypes.c_ssize_t
        result = api.ShellExecuteW(None, 'runas', powershell, subprocess.list2cmdline(args), str(ROOT), 1)
        if result <= 32:
            raise GuardError('Power helper UAC request failed or was cancelled')
        self.wait_for('ready', 60)

    def wait_for(self, state, timeout):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if (self.folder / (state + '.json')).exists():
                return
            error = self.folder / 'error.json'
            if error.exists():
                raise GuardError('Power helper: ' + json.loads(error.read_text(encoding='utf-8-sig'))['message'])
            time.sleep(0.1)
        raise GuardError('Power helper timed out; check its window and recovery journal')

    def stop(self):
        if self.folder is not None:
            (self.folder / 'stop').touch()
            self.wait_for('restored', 15)


@contextmanager
def instance_lock(path=None):
    """OS releases the byte lock after crashes; the file itself is never deleted."""
    path = path or DATA / 'guard.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        if os.name != 'nt':
            raise GuardError('Windows is required')
        import msvcrt
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise GuardError('Another mining guard is active') from exc
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


class WindowsJob:
    """Kill-on-close Job Object, assigned before the helper may launch a miner."""
    def __init__(self):
        if os.name != 'nt':
            raise GuardError('Windows Job Objects are required')
        self.api = ctypes.WinDLL('kernel32', use_last_error=True)
        api = self.api
        api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        api.CreateJobObjectW.restype = wintypes.HANDLE
        api.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        api.SetInformationJobObject.restype = wintypes.BOOL
        api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        api.AssignProcessToJobObject.restype = wintypes.BOOL
        api.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        api.TerminateJobObject.restype = wintypes.BOOL
        api.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                wintypes.DWORD, ctypes.c_void_p]
        api.QueryInformationJobObject.restype = wintypes.BOOL
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        api.CloseHandle.restype = wintypes.BOOL

        class Basic(ctypes.Structure):
            _fields_ = [('process_time', ctypes.c_int64), ('job_time', ctypes.c_int64),
                        ('flags', wintypes.DWORD), ('min_working_set', ctypes.c_size_t),
                        ('max_working_set', ctypes.c_size_t), ('active_processes', wintypes.DWORD),
                        ('affinity', ctypes.c_size_t), ('priority', wintypes.DWORD),
                        ('scheduling', wintypes.DWORD)]

        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in
                        ('read_ops', 'write_ops', 'other_ops', 'read_bytes', 'write_bytes', 'other_bytes')]

        class Extended(ctypes.Structure):
            _fields_ = [('basic', Basic), ('io', IO), ('process_memory', ctypes.c_size_t),
                        ('job_memory', ctypes.c_size_t), ('peak_process', ctypes.c_size_t),
                        ('peak_job', ctypes.c_size_t)]

        self.handle = api.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = Extended()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            self.close()
            raise GuardError('Cannot set kill-on-close protection')

    def assign(self, process):
        if not self.api.AssignProcessToJobObject(self.handle, wintypes.HANDLE(int(process._handle))):
            raise GuardError('Cannot isolate the miner in a Windows Job Object')

    def close(self):
        if self.handle:
            if not self.api.CloseHandle(self.handle):
                raise GuardError('Cannot close the mining Job Object')
            self.handle = None

    def terminate_and_wait(self):
        if not self.api.TerminateJobObject(self.handle, 1):
            raise GuardError('Cannot terminate the mining job; power recovery deferred')

        class Accounting(ctypes.Structure):
            _fields_ = [(name, ctypes.c_int64) for name in ('user', 'kernel', 'period_user', 'period_kernel')]
            _fields_ += [(name, wintypes.DWORD) for name in ('faults', 'total', 'active', 'terminated')]

        end = time.monotonic() + 5
        while True:
            info = Accounting()
            if not self.api.QueryInformationJobObject(self.handle, 1, ctypes.byref(info),
                                                      ctypes.sizeof(info), None):
                raise GuardError('Cannot verify mining job shutdown; power recovery deferred')
            if info.active == 0:
                return
            if time.monotonic() >= end:
                raise GuardError('Mining job shutdown timed out; power recovery deferred')
            time.sleep(0.05)


class MinerOutput:
    """Drain the pipe to disk; a slow console must not block draining or shutdown."""
    def __init__(self, pipe, logfile, console):
        self.pipe = pipe
        self.logfile = os.fdopen(os.dup(logfile.fileno()), 'ab', buffering=0)
        self.console = console
        self.pending = queue.Queue(maxsize=256)
        self.done = threading.Event()
        self.error = None
        self.reader = threading.Thread(target=self._drain, daemon=True)
        self.display = threading.Thread(target=self._display, daemon=True)

    def start(self):
        self.reader.start()
        self.display.start()

    def _drain(self):
        try:
            with self.pipe, self.logfile:
                while chunk := self.pipe.read1(4096):
                    self.logfile.write(chunk)
                    try:
                        self.pending.put_nowait(chunk)
                    except queue.Full:
                        # Only the screen copy may be dropped; the log remains complete.
                        pass
        except Exception as exc:
            self.error = str(exc)
        finally:
            self.done.set()

    def _display(self):
        decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        try:
            while True:
                try:
                    chunk = self.pending.get(timeout=0.1)
                except queue.Empty:
                    if self.done.is_set():
                        break
                    continue
                self.console.write(decoder.decode(chunk))
                self.console.flush()
            self.console.write(decoder.decode(b'', final=True))
            self.console.flush()
        except Exception:
            # Closed/broken console: keep logging and supervising the workload.
            pass

    def finish(self):
        # Never wait indefinitely for terminal I/O during power recovery.
        if self.reader.ident is not None:
            self.reader.join(timeout=2)
        if self.display.ident is not None:
            self.display.join(timeout=0.2)


class OwnedMiner:
    def __init__(self, command, logfile, *, console=None):
        self.job = WindowsJob()
        self.process = None
        self.output = None
        try:
            # The helper blocks on stdin; no GPU code runs before job assignment.
            self.process = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), '_child', json.dumps(command)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                cwd=str(Path(command[0]).parent), creationflags=subprocess.BELOW_NORMAL_PRIORITY_CLASS)
            self.job.assign(self.process)
            self.output = MinerOutput(self.process.stdout, logfile, sys.stdout if console is None else console)
            self.output.start()
            self.process.stdin.write(b'GO\n')
            self.process.stdin.flush()
            self.process.stdin.close()
        except BaseException:
            if self.process is not None:
                if self.process.stdin and not self.process.stdin.closed:
                    self.process.stdin.close()
                self.process.kill()
                self.process.wait(timeout=5)
            self.job.close()
            if self.output is not None:
                self.output.finish()
            elif self.process is not None and self.process.stdout is not None:
                self.process.stdout.close()
            raise

    def poll(self):
        if self.output.error is not None:
            raise GuardError('Miner output logging failed: ' + self.output.error)
        return self.process.poll()

    def stop(self):
        # Wait for all owned descendants, not just the helper, before power recovery.
        try:
            self.job.terminate_and_wait()
        finally:
            self.job.close()
            if self.output is not None:
                self.output.finish()
        self.process.wait(timeout=5)


def write_journal(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    with tmp.open('w', encoding='utf-8') as handle:
        json.dump(state, handle)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def restore_power(gpu, path=JOURNAL):
    state = json.loads(path.read_text(encoding='utf-8'))
    sample = gpu.sample(state['uuid'])
    old = number(state.get('original_power_w'), sample['low'], sample['high'], 'original_power_w')
    if abs(sample['power'] - old) > 0.5:
        gpu.set_power(state['uuid'], old)
        if abs(gpu.sample(state['uuid'])['power'] - old) > 0.5:
            raise GuardError('Original power limit was not restored; journal retained')
    path.unlink()
    LOG.info('Original GPU power limit restored: %.1f W', old)


def supervise(c, command, gpu, spawn, *, journal=JOURNAL, power_helper=None,
              monotonic=time.monotonic, wall=time.time, sleep=time.sleep, display=None):
    def show(**values):
        if display is not None:
            try:
                display.update(**values)
            except Exception:
                pass  # Presentation cannot change safety or restoration behavior.
    if journal.exists():
        raise GuardError('Unfinished power recovery exists. Run MINING.bat restore first')
    sample = gpu.sample(c['gpu_uuid'])
    original_power = sample['power']
    target = c['power_limit_w']
    if not sample['low'] <= target <= sample['high']:
        raise GuardError('Requested power cap is not supported by this GPU')
    if sample['temp'] >= c['stop_temperature_c']:
        raise GuardError('GPU is already too hot; not starting')
    miner = None
    # Reserve time for a bounded sensor query and teardown, within the 9h ceiling.
    deadline_seconds = c['max_hours'] * 3600 - 10
    begin, begin_wall = monotonic(), wall()
    show(sample=sample, cap=target, stop_temp=c['stop_temperature_c'], pool=c.get('pool', '—'),
         seconds=deadline_seconds, started=begin, status='Waiting for UAC · configuring power cap')
    write_journal(journal, {'uuid': c['gpu_uuid'], 'original_power_w': sample['power']})
    try:
        if power_helper is not None:
            power_helper.start(c['gpu_uuid'], target, sample['power'])
        elif abs(sample['power'] - target) > 0.5:
            gpu.set_power(c['gpu_uuid'], target)
        sample = gpu.sample(c['gpu_uuid'])
        if abs(sample['power'] - target) > 0.5 or sample['temp'] >= c['stop_temperature_c']:
            raise GuardError('Power cap or temperature preflight failed; not starting')
        miner = spawn(command)
        show(sample=sample, status='Process started · waiting for hashrate')
        LOG.info('Mining helper started (EXE startup not yet confirmed): cap %.1f W; stop at %.1f C; max %.2f h',
                 target, c['stop_temperature_c'], c['max_hours'])
        while True:
            elapsed = max(monotonic() - begin, wall() - begin_wall)
            if elapsed >= deadline_seconds:
                LOG.info('Session time limit reached')
                return 'time_limit'
            code = miner.poll()
            if code is not None:
                raise GuardError(f'Miner exited with code {code}; no automatic restart')
            sample = gpu.sample(c['gpu_uuid'])
            show(sample=sample)
            if sample['temp'] >= c['stop_temperature_c']:
                raise GuardError(f'Thermal stop: {sample["temp"]} C')
            if abs(sample['power'] - target) > 0.5:
                raise GuardError('GPU power cap changed; stopping')
            sleep(min(c['poll_seconds'], max(0, deadline_seconds - elapsed)))
    finally:
        # Stop the workload before restoring the larger original power limit.
        show(status='Stopping miner · power recovery not yet confirmed')
        if miner is not None:
            miner.stop()
        if power_helper is not None:
            power_helper.stop()
        restore_power(gpu, journal)
        show(status='Stopped · original power cap restored',
             sample=dict(sample, power=original_power), finished=monotonic())


def main():
    if len(sys.argv) == 3 and sys.argv[1] == '_child':
        if sys.stdin.buffer.readline() != b'GO\n':
            return 1
        return subprocess.call(json.loads(sys.argv[2]), stdin=subprocess.DEVNULL)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', nargs='?', default='check', choices=['check', 'trial', 'run', 'restore'])
    args = parser.parse_args()
    DATA.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s',
                        handlers=[logging.StreamHandler(), logging.FileHandler(DATA / 'guard.log', encoding='utf-8')])
    try:
        with instance_lock():
            gpu = Nvidia()
            if args.command == 'restore':
                if not JOURNAL.exists():
                    print('No saved power setting to restore.')
                    return 0
                restore_power(gpu)
                return 0
            c = load_config()
            sample = gpu.sample(c['gpu_uuid'])
            if args.command == 'check':
                print(json.dumps(sample, indent=2))
                if JOURNAL.exists():
                    raise GuardError('Power recovery needed: MINING.bat restore')
                command = ready_command(c)
                if not sample['low'] <= c['power_limit_w'] <= sample['high']:
                    raise GuardError('Power cap not supported')
                print('Setup checks passed. No GPU settings changed and no miner launched.')
                return 0
            if args.command == 'trial':
                c = dict(c, enabled=True, max_hours=10 / 60)
            if ctypes.windll.shell32.IsUserAnAdmin():
                raise GuardError('Run MINING.bat normally, not as administrator; only the power helper needs UAC')
            command = ready_command(c)
            from mining_console import MiningConsole
            with MiningConsole() as screen, screen.logging(), (DATA / 'miner.log').open('ab', buffering=0) as logfile:
                try:
                    supervise(c, command, gpu, lambda cmd: OwnedMiner(cmd, logfile, console=screen),
                              power_helper=PowerHelper(), display=screen)
                except BaseException:
                    screen.update(status='Session interrupted · check the controller log', finished=time.monotonic())
                    raise
            return 0
    except KeyboardInterrupt:
        LOG.warning('Stopped by Ctrl+C')
        return 130
    except (GuardError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        LOG.error('%s', exc)
        if JOURNAL.exists():
            LOG.error('Recovery journal retained: run MINING.bat restore; do not resume mining yet')
        return 1


if __name__ == '__main__':
    sys.exit(main())
