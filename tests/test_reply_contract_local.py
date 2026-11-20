"""Explicit opt-in local Docker integration; never uses SSH or production data."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import uuid

BASE = Path(__file__).resolve().parents[1]


class LocalReplyContractTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("MAIBOT_RUN_LOCAL_REPLY_CONTRACT") == "1", "Local Docker opt-in required")
    def test_real_host_reply_contract(self):
        name = "deai-local-contract-" + uuid.uuid4().hex
        image = os.environ.get("MAIBOT_LOCAL_TEST_IMAGE", "sengokucola/maibot:v1.4.5")
        # No implicit network pull; the operator prepares the image beforehand.
        inspected = subprocess.run(["docker", "image", "inspect", image], capture_output=True, timeout=30)
        self.assertEqual(inspected.returncode, 0, "Prepare the local test image first")
        with tempfile.TemporaryDirectory(prefix="deai-local-contract-") as tmp:
            root = Path(tmp)
            # TemporaryDirectory is 0700 by default. Keep its parent private,
            # but let the image's unprivileged UID traverse the mounted child.
            root = root / "inputs"
            root.mkdir()
            root.chmod(0o755)
            for name in ("plugin", "deai", "capture_message", "memory_capture", "memory_store", "candidate_viewer"):
                target = root / (name + ".py")
                target.write_bytes((BASE / target.name).read_bytes())
                target.chmod(0o644)
            fixture = BASE / "tests/fixtures/reply_contract_runtime.py"
            (root / "contract_probe.py").write_bytes(fixture.read_bytes())
            (root / "contract_probe.py").chmod(0o644)
            command = ["docker", "run", "--pull", "never", "--name", name,
                       "--network", "none", "--read-only", "--cap-drop", "ALL",
                       "--security-opt", "no-new-privileges", "--memory", "2g", "--cpus", "2",
                       "--pids-limit", "128", "--no-healthcheck",
                       "--tmpfs", "/sandbox:rw,size=256m,mode=755", "--tmpfs", "/tmp:rw,size=128m",
                       "--mount", f"type=bind,src={root},dst=/test,readonly",
                       "--env", "PYTHONDONTWRITEBYTECODE=1", "--env", "PYTHONPATH=/test:/MaiMBot",
                       "--env", "MAIBOT_INSTANCE_DIR=/sandbox/instance",
                       "--workdir", "/sandbox", "--entrypoint", "/MaiMBot/.venv/bin/python",
                       image, "/test/contract_probe.py"]
            try:
                result = subprocess.run(command, capture_output=True, text=True, timeout=180)
                snapshot = subprocess.run(["docker", "inspect", name], capture_output=True, text=True, timeout=30)
                self.assertEqual(snapshot.returncode, 0, "Local container inspection failed")
                details = json.loads(snapshot.stdout)[0]
                config = details["HostConfig"]
                self.assertEqual(config["NetworkMode"], "none")
                self.assertTrue(config["ReadonlyRootfs"])
                self.assertEqual(config["CapDrop"], ["ALL"])
                self.assertTrue(any(item.startswith("no-new-privileges") for item in config["SecurityOpt"]))
                binds = [m for m in details["Mounts"] if m["Type"] == "bind"]
                self.assertEqual(len(binds), 1)
                self.assertTrue(binds[0]["Source"] == str(root) and binds[0]["Destination"] == "/test"
                                and not binds[0]["RW"], "Unexpected local bind mount")
                # All mounted inputs are synthetic/public, but do not echo image identifiers.
                if result.returncode:
                    self.fail("Local contract fixture failed; inspect locally under controlled diagnostics")
                marker = "REPLY_CONTRACT_PASS "
                record = next((line[len(marker):] for line in result.stdout.splitlines() if line.startswith(marker)), None)
                self.assertIsNotNone(record, "Fixture success marker absent")
                evidence = json.loads(record)
                self.assertFalse(evidence["production_access"])
                self.assertGreaterEqual(len(evidence["checks"]), 11)
                print("LOCAL_REPLY_CONTRACT_PASS " + json.dumps(evidence))
            finally:
                # name is a fixed-prefix UUID generated here, never a user container name.
                removed = subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30)
                self.assertEqual(removed.returncode, 0, "Local test container cleanup failed")


if __name__ == "__main__":
    unittest.main()
