import os
import subprocess
import tempfile
import unittest
from pathlib import Path


class PagerComposeScriptTests(unittest.TestCase):
    def test_helper_derives_container_identity_from_state_directory(self):
        service_dir = Path(__file__).resolve().parent
        script = service_dir / "pdl" / "pager-compose.sh"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            state.mkdir()
            runtime = root / "runtime"
            compose_dir = runtime / "compose" / "pager-gateway"
            compose_dir.mkdir(parents=True)
            (compose_dir / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")

            fake_bin = root / "bin"
            fake_bin.mkdir()
            output = root / "identity.txt"
            fake_docker = fake_bin / "docker"
            fake_docker.write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = compose ] && [ \"$2\" = version ]; then exit 0; fi\n"
                "printf '%s:%s\\n' \"$PAGER_RUNTIME_UID\" \"$PAGER_RUNTIME_GID\" > \"$PAGER_TEST_IDENTITY\"\n"
                "exit 0\n",
                encoding="utf-8",
            )
            fake_docker.chmod(0o755)

            env = os.environ.copy()
            env.update({
                "PATH": f"{fake_bin}:{env.get('PATH', '')}",
                "PAGER_GATEWAY_ENV": str(root / "missing.env"),
                "PAGER_RUNTIME_REPO": str(runtime),
                "PAGER_DATA_HOST_PATH": str(state),
                "PAGER_TEST_IDENTITY": str(output),
            })
            env.pop("PAGER_RUNTIME_UID", None)
            env.pop("PAGER_RUNTIME_GID", None)

            result = subprocess.run(
                ["bash", str(script), "config"],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            expected = f"{state.stat().st_uid}:{state.stat().st_gid}"
            self.assertEqual(output.read_text(encoding="utf-8").strip(), expected)


if __name__ == "__main__":
    unittest.main()
