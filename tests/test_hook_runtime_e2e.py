"""Opt-in isolated real-runtime E2E. Disabled unless explicitly enabled."""
from __future__ import annotations

import json
import os
import shlex
import unittest
import uuid
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
FIXTURE = BASE / "tests" / "fixtures" / "hook_runtime_e2e.py"
LIFECYCLE_FIXTURE = BASE / "tests" / "fixtures" / "lifecycle_runtime_e2e.py"


class HookRuntimeE2ETests(unittest.TestCase):
    def _run_isolated(self, fixture, marker):
        try:
            import paramiko
        except ImportError as exc:
            self.fail(f"Install Paramiko to run this opt-in test: {exc}")

        env = os.environ
        host = env["MAIBOT_E2E_SSH_HOST"]
        user = env["MAIBOT_E2E_SSH_USER"]
        image = env["MAIBOT_E2E_IMAGE"]
        port = int(env.get("MAIBOT_E2E_SSH_PORT", "22"))
        password = env.get("MAIBOT_E2E_SSH_PASSWORD")
        key_file = env.get("MAIBOT_E2E_SSH_KEY_FILE")
        if not password and not key_file:
            self.fail("Set MAIBOT_E2E_SSH_PASSWORD or MAIBOT_E2E_SSH_KEY_FILE")

        client = paramiko.SSHClient()
        client.load_system_host_keys()
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        client.connect(
            hostname=host, port=port, username=user,
            password=password or None, key_filename=key_file or None,
            allow_agent=False, look_for_keys=False, timeout=20,
        )
        remote = "/tmp/mai-hook-e2e-" + uuid.uuid4().hex[:12]
        container = "mai-hook-e2e-" + uuid.uuid4().hex[:12]
        q = shlex.quote
        before = None
        evidence = {}

        def run(command, timeout=90):
            _stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
            out = stdout.read().decode("utf-8", "replace")
            err = stderr.read().decode("utf-8", "replace")
            status = stdout.channel.recv_exit_status()
            if status:
                raise AssertionError(f"remote exit {status}: {out[-3000:]} {err[-2000:]}")
            return out

        def state():
            return run(
                "docker inspect --format '{{.Id}} {{.Image}} {{.RestartCount}} "
                "{{.State.Status}} {{.State.Health.Status}} {{.State.StartedAt}}' maibot-core"
            ).strip()

        try:
            before = state()
            evidence["production_before"] = before
            run("mkdir -m 755 " + q(remote))
            sftp = client.open_sftp()
            files = [p for p in BASE.glob("*.py") if not p.name.startswith("nas_")]
            files.extend([BASE / "_manifest.json", BASE / "config.toml", fixture])
            for path in files:
                target = remote + "/" + path.name
                sftp.put(str(path), target)
                sftp.chmod(target, 0o644)
            sftp.close()

            command = (
                "docker run --name " + q(container)
                + " --network none --read-only --cap-drop ALL"
                + " --security-opt no-new-privileges --memory 768m --cpus 1 --pids-limit 96"
                + " --tmpfs /sandbox/instance:rw,size=128m,mode=755"
                + " --tmpfs /sandbox:rw,size=128m,mode=755 --tmpfs /tmp:rw,size=64m"
                + " --no-healthcheck --env PYTHONDONTWRITEBYTECODE=1"
                + " --env PYTHONPATH=/MaiMBot --env MAIBOT_INSTANCE_DIR=/sandbox/instance"
                + " --mount type=bind,src=" + q(remote) + ",dst=/test,readonly"
                + " --workdir /MaiMBot --entrypoint /MaiMBot/.venv/bin/python "
                + q(image) + " /test/" + q(fixture.name)
            )
            _stdin, stdout, stderr = client.exec_command(command, timeout=180)
            evidence["stdout"] = stdout.read().decode("utf-8", "replace")
            evidence["stderr"] = stderr.read().decode("utf-8", "replace")
            code = stdout.channel.recv_exit_status()
            inspection = json.loads(run("docker inspect --format '{{json .}}' " + q(container)))
            hc = inspection["HostConfig"]
            mounts = inspection["Mounts"]
            self.assertEqual(hc["NetworkMode"], "none")
            self.assertTrue(hc["ReadonlyRootfs"])
            self.assertEqual(hc["CapDrop"], ["ALL"])
            self.assertIn("no-new-privileges", hc["SecurityOpt"])
            self.assertTrue(mounts)
            self.assertTrue(all(m["Source"] == remote and m["Destination"] == "/test" and not m["RW"] for m in mounts))
            self.assertEqual(code, 0, evidence["stdout"] + "\n" + evidence["stderr"])
            self.assertIn(marker, evidence["stdout"])
        finally:
            try:
                run("docker rm -f " + q(container) + " >/dev/null 2>&1 || true")
                run("rm -rf -- " + q(remote))
                evidence["production_after"] = state()
                evidence["production_unchanged"] = evidence["production_after"] == before
            finally:
                client.close()
        self.assertTrue(evidence["production_unchanged"], evidence)
        self.last_evidence = evidence
        output = env.get("MAIBOT_E2E_EVIDENCE_FILE")
        if output:
            Path(output).write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    @unittest.skipUnless(os.environ.get("MAIBOT_RUN_HOOK_E2E") == "1", "Hook E2E opt-in required")
    def test_runner_registers_and_dispatches_in_isolated_container(self):
        self._run_isolated(FIXTURE, "E2E_PASS")

    @unittest.skipUnless(os.environ.get("MAIBOT_RUN_LIFECYCLE_E2E") == "1", "Lifecycle E2E opt-in required")
    def test_lifecycle_in_isolated_container(self):
        self._run_isolated(LIFECYCLE_FIXTURE, "LIFECYCLE_PASS")


if __name__ == "__main__":
    unittest.main()
