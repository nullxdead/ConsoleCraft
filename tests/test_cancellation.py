"""Cancellation and navigation without real downloads or user files."""
import io
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from rich.console import Console
from minecraft_launcher_lib import install
from consolecraft import ui
from consolecraft.cancellation import DownloadCancelled, cancellable_download
from consolecraft.minecraft import library_limits


class CancellationTests(unittest.TestCase):
    def test_ctrl_c_cancels_and_restores_signal_handler(self):
        previous = signal.getsignal(signal.SIGINT)
        with patch.object(ui, 'console', Console(file=io.StringIO())):
            with self.assertRaises(DownloadCancelled):
                with ui.download_progress():
                    signal.raise_signal(signal.SIGINT)
        self.assertEqual(signal.getsignal(signal.SIGINT), previous)

    def test_cancelled_download_keeps_existing_file_and_removes_partial(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / 'client.jar'
            destination.write_bytes(b'original')
            response = Mock(status_code=200)
            try:
                with cancellable_download() as event:
                    class Stream:
                        decode_content = False
                        calls = 0
                        def read(self, *args, **kwargs):
                            self.calls += 1
                            if self.calls == 1: return b'partial'
                            event.set()
                            return b'more'
                    response.raw = Stream()
                    with patch('requests.sessions.Session.request', return_value=response):
                        with library_limits(timeout=1, max_workers=2):
                            install.download_file('https://example.org/client.jar', str(destination), overwrite=True)
            except DownloadCancelled:
                pass
            else:
                self.fail('Download was not cancelled')
            self.assertEqual(destination.read_bytes(), b'original')
            self.assertEqual([p.name for p in Path(directory).iterdir()], ['client.jar'])

    def test_completed_download_is_installed_and_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / 'client.jar'
            response = Mock(status_code=200)
            response.raw = io.BytesIO(b'complete')
            with cancellable_download(), patch('requests.sessions.Session.request', return_value=response) as request:
                with library_limits(timeout=1, max_workers=2):
                    self.assertTrue(install.download_file('https://example.org/client.jar', str(destination)))
                    self.assertFalse(install.download_file('https://example.org/client.jar', str(destination)))
            self.assertEqual(destination.read_bytes(), b'complete')
            self.assertEqual(request.call_count, 1)
            self.assertEqual([p.name for p in Path(directory).iterdir()], ['client.jar'])

    def test_installer_subprocess_still_returns_output(self):
        with cancellable_download(), library_limits(timeout=1, max_workers=1):
            result = subprocess.run([sys.executable, '-c', "print('ready')"], capture_output=True, text=True, check=True, timeout=5)
        self.assertEqual(result.stdout.strip(), 'ready')

    def test_other_submenus_can_return_to_main_menu(self):
        with patch.object(ui, 'select', return_value='back') as choose:
            with self.assertRaises(ui.MenuBack):
                ui.prompt_choice('Модлоадер', ['fabric', 'forge'])
        self.assertIn(('На главный экран', 'back'), choose.call_args.args[1])

    def test_cancel_stops_queued_download_workers(self):
        started = []
        try:
            with cancellable_download() as event:
                with library_limits(timeout=1, max_workers=1):
                    with install.ThreadPoolExecutor(max_workers=1) as executor:
                        executor.submit(lambda: event.wait(1))
                        for _ in range(20): executor.submit(lambda: started.append(True))
                        event.set()
                        raise DownloadCancelled()
        except DownloadCancelled:
            pass
        self.assertEqual(started, [])

    def test_cancelled_installer_process_is_stopped(self):
        process = Mock()
        process.__enter__ = Mock(return_value=process)
        process.__exit__ = Mock(return_value=False)
        process.args = ['java', '-jar', 'installer.jar']
        process.pid = 12345
        process.poll.side_effect = [None, 1]
        process.communicate.side_effect = [DownloadCancelled(), (None, None)]
        killer = Mock()
        with cancellable_download(), patch('subprocess.Popen', side_effect=[process, killer]), patch('os.killpg', create=True) as kill_group:
            with library_limits(timeout=1, max_workers=1):
                with self.assertRaises(DownloadCancelled):
                    subprocess.run(['java', '-jar', 'installer.jar'])
        if sys.platform == 'win32':
            killer.wait.assert_called_once()
        else:
            kill_group.assert_called_once_with(12345, signal.SIGKILL)

    def test_submenus_with_existing_back_option_keep_it(self):
        with patch.object(ui, 'select', return_value='back') as choose:
            self.assertEqual(ui.prompt_choice('Профили', ['create', 'back']), 'back')
        self.assertIn(('На главный экран', 'back'), choose.call_args.args[1])


if __name__ == '__main__':
    unittest.main()
