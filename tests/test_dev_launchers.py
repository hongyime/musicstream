"""Offline dev launcher contracts; Docker is always stubbed."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
REPO_KIND = 'musicstream'

class ComposeLauncherContracts(unittest.TestCase):
    def test_explicit_dev_dispatch_and_exit_status(self):
        if os.name == "nt" and not shutil.which("pwsh"):
            self.skipTest("PowerShell 7 unavailable")
        for repo in (REPO_KIND,):
            for action in ("up", "build", "logs"):
                for exit_code in (0, 37):
                    with self.subTest(repo=repo, action=action, exit_code=exit_code), tempfile.TemporaryDirectory(prefix="compose launcher ") as temp:
                        root = Path(temp) / "repo with spaces"
                        root.mkdir()
                        suffix = ".ps1" if os.name == "nt" else ".sh"
                        script = root / ("dev" + suffix)
                        shutil.copyfile(ROOT / script.name, script)
                        log = Path(temp) / "call.json"
                        env = dict(os.environ, PORT_SCRIPT=str(script), PORT_LOG=str(log), PORT_EXIT=str(exit_code), PORT_ACTION=action)
                        if os.name == "nt":
                            code = "function docker { @{args=@($args);cwd=(Get-Location).Path} | ConvertTo-Json -Compress | Set-Content -LiteralPath $env:PORT_LOG; $global:LASTEXITCODE=[int]$env:PORT_EXIT }; & $env:PORT_SCRIPT -Action $env:PORT_ACTION -ComposeArgs @('service with spaces'); exit $LASTEXITCODE"
                            command = [shutil.which("pwsh"), "-NoProfile", "-NonInteractive", "-Command", code]
                        else:
                            stub = Path(temp) / "docker"
                            stub.write_text("#!" + sys.executable + "\nimport json,os,sys\nwith open(os.environ['PORT_LOG'],'w') as f: json.dump({'args':sys.argv[1:],'cwd':os.getcwd()},f)\nsys.exit(int(os.environ['PORT_EXIT']))\n", encoding="utf-8")
                            stub.chmod(0o755)
                            env["PATH"] = temp + os.pathsep + env["PATH"]
                            command = ["sh", str(script), action, "service with spaces"]
                        result = subprocess.run(command, cwd=temp, env=env, capture_output=True, text=True, timeout=15)
                        self.assertEqual(result.returncode, exit_code, result.stderr)
                        call = json.loads(log.read_text(encoding="utf-8-sig"))
                        expected = ["compose", "--env-file", ".env.dev"]
                        if repo == "ticketremaster-b":
                            expected += ["-f", "docker-compose.yml"]
                        expected += ["-f", "compose.dev.yaml", action]
                        if action == "up":
                            expected += ["--no-build"]
                        expected += ["service with spaces"]
                        self.assertEqual(call["args"], expected)
                        self.assertTrue(Path(call["cwd"]).samefile(root), "launcher changed to a different directory")

    def test_up_rejects_build_and_pull_overrides(self):
        for repo in (REPO_KIND,):
            for option in ("--build", "--pull=always", "--no-build=false"):
                with self.subTest(repo=repo, option=option):
                    script = ROOT / ("dev.ps1" if os.name == "nt" else "dev.sh")
                    env = dict(os.environ, PORT_SCRIPT=str(script), PORT_OPTION=option)
                    if os.name == "nt":
                        code = "function docker { throw 'unexpected Docker invocation' }; & $env:PORT_SCRIPT -Action up -ComposeArgs @($env:PORT_OPTION)"
                        command = [shutil.which("pwsh"), "-NoProfile", "-NonInteractive", "-Command", code]
                    else:
                        temporary = tempfile.TemporaryDirectory(prefix="blocked compose ")
                        self.addCleanup(temporary.cleanup)
                        stub = Path(temporary.name) / "docker"
                        stub.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
                        stub.chmod(0o755)
                        env["PATH"] = temporary.name + os.pathsep + env["PATH"]
                        command = ["sh", str(script), "up", option]
                    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=15)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("explicit build action", result.stderr)


if __name__ == "__main__":
    unittest.main()
