import subprocess
import threading
import unittest
from unittest.mock import patch

import github_collector


class RunGhTests(unittest.TestCase):
    @patch("github_collector._collect_for_pr")
    @patch("github_collector.list_closed_prs")
    def test_collects_prs_concurrently_but_preserves_order(self, list_prs, collect_pr):
        list_prs.return_value = [{"number": 1}, {"number": 2}]
        rendezvous = threading.Barrier(2)

        def collect(repo, username, pr, progress_cb):
            rendezvous.wait(timeout=1)
            return [pr["number"]]

        collect_pr.side_effect = collect

        result = github_collector.collect_for_repo(
            "org/repo", "reviewer", max_workers=2
        )

        self.assertEqual(result, [1, 2])

    @patch("github_collector.subprocess.run")
    def test_flattens_every_paginated_json_document(self, run):
        run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout='[{"id": 1}]\n[{"id": 2}, {"id": 3}]\n',
            stderr="",
        )

        result = github_collector.run_gh(["api", "--paginate", "an-endpoint"])

        self.assertEqual(result, [{"id": 1}, {"id": 2}, {"id": 3}])

    @patch("github_collector.subprocess.run")
    def test_rejects_non_array_paginated_pages(self, run):
        run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout='[{"id": 1}]\n{"id": 2}', stderr=""
        )

        with self.assertRaisesRegex(ValueError, "page 2"):
            github_collector.run_gh(["api", "--paginate", "an-endpoint"])

    @patch("github_collector.time.sleep")
    @patch("github_collector.subprocess.run")
    def test_retries_plain_eof_transport_failure(self, run, sleep):
        run.side_effect = [
            subprocess.CompletedProcess(
                args=[], returncode=1, stdout="", stderr="Get 'https://api.github.com': EOF"
            ),
            subprocess.CompletedProcess(
                args=[], returncode=0, stdout='[{"id": 1}]', stderr=""
            ),
        ]

        result = github_collector.run_gh(["api", "--paginate", "an-endpoint"])

        self.assertEqual(result, [{"id": 1}])
        self.assertEqual(run.call_count, 2)
        sleep.assert_called_once_with(1)


if __name__ == "__main__":
    unittest.main()
