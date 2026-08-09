import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "bootstrap-localstack.sh"


class BootstrapLocalStackTests(unittest.TestCase):
    def test_rejects_invalid_opensearch_setting_before_external_commands(self):
        result = subprocess.run(
            ["bash", str(SCRIPT)],
            env={**os.environ, "PR_TO_SKILL_LOCAL_OPENSEARCH": "sometimes"},
            capture_output=True,
            text=True,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(
            result.stderr.strip(),
            "PR_TO_SKILL_LOCAL_OPENSEARCH must be true or false",
        )

    def test_default_bootstrap_does_not_call_opensearch_and_escapes_environment(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            calls = directory / "aws-calls"
            self._write_executable(directory / "curl", "exit 0")
            self._write_executable(
                directory / "aws",
                textwrap.dedent(
                    f"""\
                    printf '%s\\n' "$*" >>{calls!s}
                    case "$*" in
                      *"s3api head-bucket"*) exit 0 ;;
                      *"sqs create-queue"*) printf 'http://queue.local/value with spaces\\n' ;;
                      *"sqs get-queue-attributes"*) printf 'arn:aws:sqs:us-east-1:000000000000:dlq\\n' ;;
                    esac
                    """
                ),
            )
            endpoint = "http://localhost:4566/value with spaces;still-data"
            environment = {
                **os.environ,
                "PATH": f"{directory}:{os.environ['PATH']}",
                "PR_TO_SKILL_AWS_ENDPOINT_URL": endpoint,
            }

            subprocess.run(
                ["bash", str(SCRIPT)],
                cwd=directory,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            )

            self.assertNotIn("opensearch", calls.read_text())
            loaded = subprocess.run(
                [
                    "bash",
                    "-c",
                    'set -a; source .env.localstack; printf "%s\\n%s" '
                    '"$AWS_OPENSEARCH_ENABLED" "$PR_TO_SKILL_AWS_ENDPOINT_URL"',
                ],
                cwd=directory,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(loaded.stdout, f"false\n{endpoint}")

    def test_opensearch_requires_explicit_opt_in(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            calls = directory / "aws-calls"
            self._write_executable(directory / "curl", "exit 0")
            self._write_executable(
                directory / "sleep",
                "exit 0",
            )
            self._write_executable(
                directory / "aws",
                textwrap.dedent(
                    f"""\
                    printf '%s\\n' "$*" >>{calls!s}
                    case "$*" in
                      *"s3api head-bucket"*) exit 0 ;;
                      *"sqs create-queue"*) printf 'http://queue.local/work\\n' ;;
                      *"sqs get-queue-attributes"*) printf 'arn:aws:sqs:us-east-1:000000000000:dlq\\n' ;;
                      *"DomainStatus.Processing"*) printf 'False\\n' ;;
                      *"DomainStatus.Endpoint"*) printf 'search.localhost.localstack.cloud:4566\\n' ;;
                    esac
                    """
                ),
            )
            environment = {
                **os.environ,
                "PATH": f"{directory}:{os.environ['PATH']}",
                "PR_TO_SKILL_LOCAL_OPENSEARCH": "true",
            }

            subprocess.run(
                ["bash", str(SCRIPT)],
                cwd=directory,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            )

            self.assertIn("opensearch describe-domain", calls.read_text())
            settings = (directory / ".env.localstack").read_text()
            self.assertIn("AWS_OPENSEARCH_ENABLED=true", settings)
            self.assertIn(
                "AWS_OPENSEARCH_ENDPOINT=http://search.localhost.localstack.cloud:4566",
                settings,
            )

    def test_secret_value_is_sent_only_on_stdin(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            calls = directory / "aws-calls"
            secret_input = directory / "secret-input"
            self._write_executable(directory / "curl", "exit 0")
            self._write_executable(
                directory / "aws",
                textwrap.dedent(
                    f"""\
                    printf '%s\\n' "$*" >>{calls!s}
                    case "$*" in
                      *"s3api head-bucket"*) exit 0 ;;
                      *"sqs create-queue"*) printf 'http://queue.local/work\\n' ;;
                      *"sqs get-queue-attributes"*) printf 'arn:aws:sqs:us-east-1:000000000000:dlq\\n' ;;
                      *"secretsmanager create-secret"*|*"secretsmanager put-secret-value"*) cat >{secret_input!s} ;;
                    esac
                    """
                ),
            )
            secret = "line one\n$(must-not-execute); line two"
            environment = {
                **os.environ,
                "PATH": f"{directory}:{os.environ['PATH']}",
                "GITHUB_WEBHOOK_SECRET": secret,
            }

            subprocess.run(
                ["bash", str(SCRIPT)],
                cwd=directory,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            )

            arguments = calls.read_text()
            settings = (directory / ".env.localstack").read_text()
            self.assertNotIn(secret, arguments)
            self.assertNotIn(secret, settings)
            self.assertIn("--secret-string file:///dev/stdin", arguments)
            self.assertEqual(secret_input.read_text(), secret)

    @staticmethod
    def _write_executable(path: Path, body: str) -> None:
        path.write_text(f"#!/usr/bin/env bash\nset -euo pipefail\n{body}\n")
        path.chmod(0o755)


if __name__ == "__main__":
    unittest.main()