import io
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import unittest
import tempfile
import zipfile
from unittest.mock import Mock, patch

from rich.console import Console
from consolecraft import __version__, ui
from consolecraft.cli import Application, main, make_parser
from consolecraft.config import ConfigStore


class TerminalMenuTests(unittest.TestCase):
    def test_invalid_setting_returns_to_menu(self):
        with tempfile.TemporaryDirectory() as home:
            app = Application(ConfigStore(home))
            app.settings.auto_check_updates = False
            app.store.save(app.settings)
            output = io.StringIO()
            with patch.object(ui, 'console', Console(file=output)), patch.object(ui, 'select', side_effect=['10', '0']) as select, \
                    patch.object(ui, 'prompt_choice', return_value='memory'), patch.object(ui, 'input_text', return_value='abc'), \
                    patch('sys.stderr', io.StringIO()):
                self.assertEqual(app.menu(), 0)
            self.assertEqual(select.call_count, 2)
            self.assertIn('Некорректное значение', output.getvalue())

    def test_update_check_uses_the_real_repository(self):
        from consolecraft import commands
        app = Mock()
        app.settings.download_timeout = 30
        response = Mock()
        response.json.return_value = {'tag_name': 'v99.0.0'}
        with patch('requests.get', return_value=response) as get, patch.object(ui, 'info') as info:
            commands.check_updates(app)
        self.assertEqual(get.call_args.args[0], 'https://api.github.com/repos/nullxdead/ConsoleLauncher/releases/latest')
        self.assertIn('99.0.0', info.call_args.args[0])

    def test_interactive_menu_returns_value_selected_with_arrows(self):
        question = Mock()
        question.ask.return_value = '5'
        with patch('sys.stdin.isatty', return_value=True), patch('sys.stdout.isatty', return_value=True), patch('questionary.select', return_value=question) as select:
            self.assertEqual(ui.select('Меню', [('Играть', '1'), ('Моды', '5')], '1'), '5')
        self.assertEqual([item.value for item in select.call_args.kwargs['choices']], ['1', '5'])

    def test_piped_menu_accepts_existing_numeric_input(self):
        with patch('sys.stdin', io.StringIO('0\n')), patch.object(ui, 'console', Console(file=io.StringIO())):
            self.assertEqual(ui.select('Меню', [('Играть', '1'), ('Выход', '0')], '1'), '0')

    def test_default_opens_console_and_gui_command_is_removed(self):
        with patch('consolecraft.cli.Application') as app:
            app.return_value.run_command.return_value = 0
            self.assertEqual(main([]), 0)
            self.assertEqual(app.return_value.run_command.call_args.args[0].command, 'cli')
        with patch('sys.stderr', io.StringIO()), self.assertRaises(SystemExit):
            main(['gui'])

    def test_saved_server_can_be_launched_from_cli(self):
        with tempfile.TemporaryDirectory() as home:
            app = Application(ConfigStore(home))
            app.play = Mock(return_value=0)
            parser = make_parser()
            app.run_command(parser.parse_args(['servers', 'add', 'Home', 'example.org', '--port', '25566']))
            app.run_command(parser.parse_args(['servers', 'play', 'Home']))
            app.play.assert_called_once_with(server='example.org', port=25566)

    @unittest.skipUnless(sys.platform == 'win32', 'Windows PowerShell 5.1 is available on Windows')
    def test_windows_powershell_bootstrap_pipeline(self):
        installer = Path(__file__).resolve().parents[1] / 'scripts' / 'install.ps1'
        self.assertFalse(installer.read_bytes().startswith(b'\xef\xbb\xbf'))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / f'ConsoleLauncher-{__version__}-Windows-AMD64.zip'
            with zipfile.ZipFile(archive, 'w') as package:
                package.writestr('consolelauncher.exe', b'fixture')
                package.writestr('install-cli.ps1', "param([string]$PackageDirectory)\nif (-not (Test-Path (Join-Path $PackageDirectory 'consolelauncher.exe'))) { throw 'missing' }\n[IO.File]::WriteAllText($env:CL_TEST_MARKER, 'ok')\n")
            (root / 'SHA256SUMS.txt').write_text(hashlib.sha256(archive.read_bytes()).hexdigest() + '  ' + archive.name + '\n', encoding='ascii')
            check = root / 'check.ps1'
            check.write_text("""$ErrorActionPreference='Stop'
function Invoke-RestMethod { param([string]$Uri) [Text.Encoding]::UTF8.GetString([IO.File]::ReadAllBytes($env:CL_TEST_INSTALLER)) }
function Invoke-WebRequest { param([switch]$UseBasicParsing,[string]$Uri,[string]$OutFile) Copy-Item -LiteralPath (Join-Path $env:CL_TEST_ROOT (($Uri -split '/')[-1])) -Destination $OutFile }
irm 'https://example.invalid/install.ps1' | iex
if ((Get-Content -LiteralPath $env:CL_TEST_MARKER -Raw) -ne 'ok') { throw 'Installer did not run' }
""", encoding='ascii')
            env = dict(os.environ, CL_TEST_INSTALLER=str(installer), CL_TEST_ROOT=str(root), CL_TEST_MARKER=str(root / 'marker'))
            powershell = Path(os.environ['WINDIR']) / 'System32' / 'WindowsPowerShell' / 'v1.0' / 'powershell.exe'
            result = subprocess.run([str(powershell), '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(check)], env=env, capture_output=True, timeout=25)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
