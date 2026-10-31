"""No GPU writes, miner downloads, network requests, or real mining in these tests."""
import ctypes
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, Mock, patch

import mine_guard as g


class FakeGPU:
    def __init__(self):
        self.power = 145.0
        self.temp = 32.0
        self.writes = []
        self.samples = 0
        self.on_sample = None

    def sample(self, uuid):
        self.samples += 1
        if self.on_sample:
            self.on_sample(self)
        return dict(uuid=uuid, low=123, high=145, power=self.power, temp=self.temp)

    def set_power(self, uuid, value):
        self.writes.append(value)
        self.power = value


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.journal = self.root / 'restore.json'
        self.c = g.load_config()
        self.gpu = FakeGPU()
        self.miner = Mock()
        self.miner.poll.return_value = None
        self.spawn = Mock(return_value=self.miner)
        self.clock = 0.0

    def sleep(self, duration):
        self.clock += duration

    def run_guard(self):
        return g.supervise(self.c, ['fake'], self.gpu, self.spawn, journal=self.journal,
                           monotonic=lambda: self.clock, wall=lambda: self.clock, sleep=self.sleep)

    def assert_restored(self):
        self.miner.stop.assert_called_once()
        self.assertEqual(self.gpu.writes, [123, 145])
        self.assertFalse(self.journal.exists())

    def test_nine_hour_limit_without_nine_hour_workload(self):
        self.assertEqual(self.run_guard(), 'time_limit')
        self.assertLessEqual(self.clock, 9 * 3600)
        self.assert_restored()

    def test_broken_display_cannot_prevent_stop_or_restoration(self):
        display = Mock()
        display.update.side_effect = OSError('terminal unavailable')
        result = g.supervise(self.c, ['fake'], self.gpu, self.spawn, journal=self.journal,
                             monotonic=lambda: self.clock, wall=lambda: self.clock,
                             sleep=self.sleep, display=display)
        self.assertEqual(result, 'time_limit')
        self.assert_restored()

    def test_display_reports_original_power_after_verified_restore(self):
        display = Mock()
        self.gpu.power = 130
        g.supervise(self.c, ['fake'], self.gpu, self.spawn, journal=self.journal,
                    monotonic=lambda: self.clock, wall=lambda: self.clock,
                    sleep=self.sleep, display=display)
        final = display.update.call_args.kwargs
        self.assertEqual(final['sample']['power'], 130)
        self.assertIn('восстановлена', final['status'])

    def test_thermal_stop(self):
        def heat(gpu):
            if gpu.samples == 3:
                gpu.temp = 70
        self.gpu.on_sample = heat
        with self.assertRaisesRegex(g.GuardError, 'Thermal'):
            self.run_guard()
        self.assert_restored()

    def test_original_nondefault_power_is_preserved(self):
        self.gpu.power = 130
        self.assertEqual(self.run_guard(), 'time_limit')
        self.assertEqual(self.gpu.writes, [123, 130])
        self.assertFalse(self.journal.exists())

    def test_preset_target_can_run_without_power_write_permission(self):
        self.gpu.power = 123
        self.gpu.set_power = Mock(side_effect=g.GuardError('access denied'))
        self.assertEqual(self.run_guard(), 'time_limit')
        self.gpu.set_power.assert_not_called()
        self.miner.stop.assert_called_once()
        self.assertFalse(self.journal.exists())

    def test_power_helper_starts_before_miner_and_restores_after_stop(self):
        events = []
        helper = Mock()
        def start(uuid, target, original):
            events.append('cap')
            self.assertEqual(original, 145)
            self.gpu.power = target
        def stop():
            events.append('restore')
            self.gpu.power = 145
        helper.start.side_effect = start
        helper.stop.side_effect = stop
        def spawn(command):
            events.append('miner')
            self.assertEqual(self.gpu.power, 123)
            return self.miner
        self.miner.stop.side_effect = lambda: events.append('stop')
        result = g.supervise(self.c, ['fake'], self.gpu, spawn, journal=self.journal,
                             power_helper=helper, monotonic=lambda: self.clock,
                             wall=lambda: self.clock, sleep=self.sleep)
        self.assertEqual(result, 'time_limit')
        self.assertEqual(events, ['cap', 'miner', 'stop', 'restore'])
        self.assertEqual(self.gpu.writes, [])
        self.assertFalse(self.journal.exists())

    def test_power_helper_failure_blocks_miner(self):
        helper = Mock()
        helper.start.side_effect = g.GuardError('UAC cancelled')
        with self.assertRaisesRegex(g.GuardError, 'UAC cancelled'):
            g.supervise(self.c, ['fake'], self.gpu, self.spawn, journal=self.journal,
                        power_helper=helper)
        self.spawn.assert_not_called()
        helper.stop.assert_called_once()
        self.assertEqual(self.gpu.writes, [])

    def test_power_helper_restore_failure_keeps_journal(self):
        helper = Mock()
        helper.start.side_effect = lambda *args: setattr(self.gpu, 'power', 123)
        helper.stop.side_effect = g.GuardError('restore failed')
        self.miner.poll.return_value = 1
        with self.assertRaisesRegex(g.GuardError, 'restore failed'):
            g.supervise(self.c, ['fake'], self.gpu, self.spawn, journal=self.journal,
                        power_helper=helper)
        self.miner.stop.assert_called_once()
        self.assertTrue(self.journal.exists())
        self.assertEqual(self.gpu.power, 123)

    def test_power_helper_reads_status_without_running_powershell(self):
        helper = g.PowerHelper()
        helper.folder = self.root
        (self.root / 'ready.json').write_text('{}')
        helper.wait_for('ready', 1)
        (self.root / 'error.json').write_text(json.dumps({'message': 'write failed'}))
        with self.assertRaisesRegex(g.GuardError, 'write failed'):
            helper.wait_for('restored', 1)
        (self.root / 'restored.json').write_text('{}')
        helper.stop()
        self.assertTrue((self.root / 'stop').exists())

    def test_restricted_policy_fails_before_uac_or_session_creation(self):
        helper = g.PowerHelper()
        result = subprocess.CompletedProcess([], 0, stdout='Restricted\n')
        with patch.object(g.subprocess, 'run', return_value=result):
            with self.assertRaisesRegex(g.GuardError, 'policy Restricted'):
                helper.start(self.c['gpu_uuid'], 123, 145)
        self.assertIsNone(helper.folder)
        helper.stop()

    def test_process_only_policy_is_used_for_preflight_and_uac(self):
        helper = g.PowerHelper()
        api = Mock()
        api.ShellExecuteW.return_value = 33
        result = subprocess.CompletedProcess([], 0, stdout='RemoteSigned\n')
        with patch.object(g, 'DATA', self.root), \
             patch.object(g.subprocess, 'run', return_value=result) as run, \
             patch.object(g.ctypes, 'WinDLL', return_value=api), \
             patch.object(helper, 'wait_for'):
            helper.start(self.c['gpu_uuid'], 123, 145)
        args = run.call_args.args[0]
        self.assertEqual(args[args.index('-ExecutionPolicy') + 1], 'RemoteSigned')
        elevated_args = api.ShellExecuteW.call_args.args[3]
        self.assertIn('-ExecutionPolicy RemoteSigned -File', elevated_args)
        self.assertIn('mining_power.ps1', elevated_args)
        self.assertNotIn('lolMiner.exe', elevated_args)

    def test_sleep_resume_wall_time_counts_toward_limit(self):
        walls = iter([0, 0, 10 * 3600])
        result = g.supervise(self.c, ['fake'], self.gpu, self.spawn, journal=self.journal,
                             monotonic=lambda: self.clock, wall=lambda: next(walls), sleep=self.sleep)
        self.assertEqual(result, 'time_limit')
        self.assertEqual(self.clock, 2)
        self.assert_restored()

    def test_unsupported_power_cap_never_starts(self):
        self.c['power_limit_w'] = 100
        with self.assertRaisesRegex(g.GuardError, 'not supported'):
            self.run_guard()
        self.assertEqual(self.gpu.writes, [])
        self.spawn.assert_not_called()

    def test_telemetry_failure_stops_before_restoration(self):
        def fail(gpu):
            if gpu.samples == 3:
                raise g.GuardError('sensor failed')
        self.gpu.on_sample = fail
        self.miner.stop.side_effect = lambda: self.assertEqual(self.gpu.power, 123)
        with self.assertRaisesRegex(g.GuardError, 'sensor failed'):
            self.run_guard()
        self.assert_restored()

    def test_power_write_denied_never_starts_miner(self):
        def deny_once(uuid, power):
            if power == 123:
                raise g.GuardError('access denied')
            self.gpu.power = power
        self.gpu.set_power = Mock(side_effect=deny_once)
        with self.assertRaisesRegex(g.GuardError, 'access denied'):
            self.run_guard()
        self.spawn.assert_not_called()
        self.assertFalse(self.journal.exists())
        self.gpu.set_power.assert_called_once_with(self.c['gpu_uuid'], 123)

    def test_already_restored_limit_requires_no_write(self):
        g.write_journal(self.journal, {'uuid': self.c['gpu_uuid'], 'original_power_w': 145})
        self.gpu.set_power = Mock(side_effect=g.GuardError('access denied'))
        g.restore_power(self.gpu, self.journal)
        self.gpu.set_power.assert_not_called()
        self.assertFalse(self.journal.exists())

    def test_recovery_sensor_failure_retains_journal(self):
        g.write_journal(self.journal, {'uuid': self.c['gpu_uuid'], 'original_power_w': 145})
        self.gpu.sample = Mock(side_effect=g.GuardError('sensor failed'))
        with self.assertRaisesRegex(g.GuardError, 'sensor failed'):
            g.restore_power(self.gpu, self.journal)
        self.assertTrue(self.journal.exists())

    def test_power_drift_stops_mining(self):
        def drift(gpu):
            if gpu.samples == 3:
                gpu.power = 145
        self.gpu.on_sample = drift
        with self.assertRaisesRegex(g.GuardError, 'cap changed'):
            self.run_guard()
        self.miner.stop.assert_called_once()
        self.assertEqual(self.gpu.writes, [123])
        self.assertFalse(self.journal.exists())

    def test_miner_exit_is_not_restarted(self):
        self.miner.poll.return_value = 42
        with self.assertRaisesRegex(g.GuardError, 'code 42'):
            self.run_guard()
        self.spawn.assert_called_once()
        self.assert_restored()

    def test_ctrl_c_stops_and_restores(self):
        self.miner.poll.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.run_guard()
        self.assert_restored()

    def test_launch_failure_restores(self):
        self.spawn.side_effect = OSError('launch failed')
        with self.assertRaises(OSError):
            self.run_guard()
        self.assertEqual(self.gpu.power, 145)
        self.assertFalse(self.journal.exists())

    def test_failed_shutdown_does_not_raise_power(self):
        self.miner.poll.return_value = 1
        self.miner.stop.side_effect = g.GuardError('shutdown failed')
        with self.assertRaisesRegex(g.GuardError, 'shutdown failed'):
            self.run_guard()
        self.assertEqual(self.gpu.writes, [123])
        self.assertTrue(self.journal.exists())

    def test_failed_restore_keeps_recovery_journal(self):
        self.miner.poll.return_value = 1
        old_set = self.gpu.set_power
        def fail_restore(uuid, value):
            if value == 145:
                raise g.GuardError('restore failed')
            old_set(uuid, value)
        self.gpu.set_power = fail_restore
        with self.assertRaisesRegex(g.GuardError, 'restore failed'):
            self.run_guard()
        self.miner.stop.assert_called_once()
        self.assertTrue(self.journal.exists())

    def test_pending_recovery_blocks_new_session(self):
        g.write_journal(self.journal, {'uuid': self.c['gpu_uuid'], 'original_power_w': 145})
        with self.assertRaisesRegex(g.GuardError, 'recovery'):
            self.run_guard()
        self.spawn.assert_not_called()
        self.assertEqual(self.gpu.writes, [])
        g.restore_power(self.gpu, self.journal)
        self.assertFalse(self.journal.exists())

    def test_initial_hot_gpu_never_changes_settings(self):
        self.gpu.temp = 70
        with self.assertRaisesRegex(g.GuardError, 'too hot'):
            self.run_guard()
        self.assertEqual(self.gpu.writes, [])

    def test_configuration_hard_ceilings(self):
        for name, value in [('max_hours', 10), ('power_limit_w', 145),
                            ('stop_temperature_c', 80), ('poll_seconds', 10),
                            ('max_hours', float('nan')), ('max_hours', True)]:
            with self.subTest(name=name, value=value):
                config = dict(self.c, **{name: value})
                path = self.root / 'config.json'
                path.write_text(json.dumps(config))
                with self.assertRaises(g.GuardError):
                    g.load_config(path)

    def test_disabled_and_ton_wallet_are_blocked(self):
        with self.assertRaisesRegex(g.GuardError, 'disabled'):
            g.ready_command(self.c)
        self.c.update(enabled=True, pool='etc.example.org:5555', wallet='UQB_TON')
        with self.assertRaisesRegex(g.GuardError, 'ETC payout'):
            g.ready_command(self.c)

    def test_hash_validation_and_fixed_flags(self):
        exe = self.root / 'lolMiner.exe'
        exe.write_bytes(b'fake-not-executed')
        self.c.update(enabled=True, pool='etc.example.org:5555', wallet='0x' + 'a' * 40,
                      miner_path=str(exe), miner_sha256=hashlib.sha256(exe.read_bytes()).hexdigest())
        cmd = g.ready_command(self.c)
        self.assertIn('--watchdog', cmd)
        self.assertEqual(cmd[cmd.index('--watchdog') + 1], 'exit')
        self.assertEqual(cmd[cmd.index('--devices') + 1], '0')
        self.assertEqual(cmd[cmd.index('--tls') + 1], 'on')
        exe.write_bytes(b'tampered')
        with self.assertRaisesRegex(g.GuardError, 'mismatch'):
            g.ready_command(self.c)

    def test_invalid_sensor_values_fail_closed(self):
        gpu = object.__new__(g.Nvidia)
        for row in [f'{self.c["gpu_uuid"]}, RTX 5060, 123, 145, N/A, 32',
                    f'{self.c["gpu_uuid"]}, RTX 5060, 123, 145, 123, NaN', '']:
            gpu.call = Mock(return_value=row)
            with self.subTest(row=row), self.assertRaises(g.GuardError):
                gpu.sample(self.c['gpu_uuid'])

    def test_nvidia_permission_error_is_explicit(self):
        gpu = object.__new__(g.Nvidia)
        gpu.exe = 'fake-nvidia-smi'
        failure = subprocess.CalledProcessError(4, [gpu.exe])
        with patch.object(g.subprocess, 'run', side_effect=failure):
            with self.assertRaisesRegex(g.GuardError, 'insufficient permissions'):
                gpu.call(['-pl', '123'])


@unittest.skipUnless(os.name == 'nt', 'Windows process containment')
class WindowsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_duplicate_guard_lock(self):
        path = self.root / 'lock'
        with g.instance_lock(path):
            with self.assertRaises(g.GuardError):
                with g.instance_lock(path):
                    self.fail('second lock succeeded')
        with g.instance_lock(path):
            pass

    def test_job_shutdown_leaves_unrelated_process_alive(self):
        unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        self.addCleanup(unrelated.wait)
        self.addCleanup(unrelated.kill)
        with (self.root / 'log').open('ab') as logfile:
            owned = g.OwnedMiner([sys.executable, '-c', 'import time; time.sleep(60)'], logfile)
            try:
                self.assertIsNone(owned.poll())
            finally:
                owned.stop()
            self.assertIsNotNone(owned.poll())
            self.assertIsNone(unrelated.poll())

    def test_live_stdout_and_stderr_are_logged_before_exit(self):
        seen = threading.Event()
        screen = io.StringIO()
        def write(text):
            screen.write(text)
            if 'stderr-live' in screen.getvalue():
                seen.set()
        console = Mock(write=write)
        path = self.root / 'stream.log'
        workload = ("import os,time; os.write(1,b'stdout-live\\r'); "
                    "os.write(2,b'stderr-live'); time.sleep(60)")
        with path.open('ab', buffering=0) as logfile:
            owned = g.OwnedMiner([sys.executable, '-c', workload], logfile, console=console)
            try:
                self.assertTrue(seen.wait(5), 'output not displayed while child was alive')
                self.assertIsNone(owned.poll())
                self.assertEqual(path.read_bytes(), b'stdout-live\rstderr-live')
            finally:
                owned.stop()
            self.assertFalse(owned.output.reader.is_alive())
            self.assertFalse(owned.output.display.is_alive())

    def test_natural_exit_drains_final_output_and_preserves_log(self):
        screen = io.StringIO()
        path = self.root / 'stream.log'
        path.write_bytes(b'previous-session\n')
        with path.open('ab') as logfile:
            owned = g.OwnedMiner([sys.executable, '-c',
                                  "import os; os.write(1,'final: Привет'.encode('utf-8'))"],
                                 logfile, console=screen)
            try:
                self.assertEqual(owned.process.wait(timeout=5), 0)
            finally:
                owned.stop()
        self.assertEqual(screen.getvalue(), 'final: Привет')
        self.assertEqual(path.read_bytes(), b'previous-session\n' + 'final: Привет'.encode('utf-8'))

    def test_broken_console_does_not_break_logging(self):
        path = self.root / 'stream.log'
        console = Mock()
        console.write.side_effect = OSError('closed terminal')
        with path.open('ab') as logfile:
            owned = g.OwnedMiner([sys.executable, '-c',
                                  "import os; os.write(1,b'x'*262144)"], logfile, console=console)
            try:
                self.assertEqual(owned.process.wait(timeout=5), 0)
            finally:
                owned.stop()
        self.assertEqual(path.read_bytes(), b'x' * 262144)

    def test_logging_failure_is_reported_to_supervisor(self):
        path = self.root / 'stream.log'
        real_output = g.MinerOutput
        def output_with_failed_disk(*args):
            output = real_output(*args)
            output.logfile.close()
            output.logfile = MagicMock()
            output.logfile.write.side_effect = OSError('disk full')
            return output
        with path.open('ab') as logfile:
            with patch.object(g, 'MinerOutput', side_effect=output_with_failed_disk):
                owned = g.OwnedMiner([sys.executable, '-c',
                                      "import os,time; os.write(1,b'hello'); time.sleep(60)"],
                                     logfile, console=io.StringIO())
                try:
                    self.assertTrue(owned.output.done.wait(5))
                    with self.assertRaisesRegex(g.GuardError, 'logging failed: disk full'):
                        owned.poll()
                finally:
                    owned.stop()

    def test_blocked_console_does_not_block_pipe_or_shutdown(self):
        entered, release = threading.Event(), threading.Event()
        def write(text):
            entered.set()
            release.wait(10)
        console = Mock(write=write)
        path = self.root / 'stream.log'
        # More than both the pipe capacity and the bounded screen queue.
        size = 2 * 1024 * 1024
        with path.open('ab') as logfile:
            owned = g.OwnedMiner([sys.executable, '-c',
                                  f"import os; os.write(1,b'x'*{size})"], logfile, console=console)
            try:
                self.assertTrue(entered.wait(5))
                self.assertEqual(owned.process.wait(timeout=5), 0)
                begin = time.monotonic()
                owned.stop()
                self.assertLess(time.monotonic() - begin, 3)
                self.assertFalse(owned.output.reader.is_alive())
                self.assertEqual(path.stat().st_size, size)
            finally:
                release.set()
                owned.output.display.join(timeout=2)
                if owned.job.handle:
                    owned.stop()

    def test_parent_crash_kills_owned_workload(self):
        pidfile = self.root / 'pid'
        workload = ('import os,time,pathlib; '
                    f'pathlib.Path({str(pidfile)!r}).write_text(str(os.getpid())); time.sleep(60)')
        parent = f'''
import os, sys, time
import mine_guard as g
from pathlib import Path
with open({str(self.root / 'log')!r}, 'ab') as log:
    miner = g.OwnedMiner([sys.executable, '-c', {workload!r}], log)
    end = time.monotonic() + 8
    while not Path({str(pidfile)!r}).exists() and time.monotonic() < end:
        time.sleep(0.05)
    os._exit(0)
'''
        subprocess.run([sys.executable, '-c', parent], cwd=g.ROOT, check=True, timeout=15)
        self.assertTrue(pidfile.exists(), 'fake workload did not start')
        pid = int(pidfile.read_text())
        api = ctypes.WinDLL('kernel32', use_last_error=True)
        api.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        api.OpenProcess.restype = ctypes.c_void_p
        api.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        api.WaitForSingleObject.restype = ctypes.c_uint32
        api.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = api.OpenProcess(0x100000, False, pid)
        if handle:
            try:
                self.assertEqual(api.WaitForSingleObject(handle, 5000), 0)
            finally:
                api.CloseHandle(handle)


if __name__ == '__main__':
    unittest.main(verbosity=2)
