import subprocess
import threading
import unittest
from unittest.mock import patch

import github_collector


class RunGhTests(unittest.TestCase):
    @patch.dict("github_collector.os.environ", {"GITHUB_TOKEN": "token"}, clear=True)
    @patch("github_collector._run_github_api", return_value={"number": 12})
    def test_uses_http_transport_when_github_credentials_are_configured(self, run_api):
        result = github_collector.run_gh(["api", "repos/org/repo/pulls/12"])

        self.assertEqual(result, {"number": 12})
        run_api.assert_called_once_with(["api", "repos/org/repo/pulls/12"], 3)

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

    @patch("github_collector.get_pr_issue_comments", return_value=[])
    @patch("github_collector.get_pr_reviews")
    @patch("github_collector.get_pr_review_comments", return_value=[])
    def test_excludes_dismissed_review_from_history(
        self, review_comments, reviews, issue_comments
    ):
        reviews.return_value = [{
            "id": 99,
            "user": {"login": "reviewer"},
            "body": "This guidance was withdrawn.",
            "state": "DISMISSED",
        }]
        pull_request = {
            "number": 12, "title": "PR", "html_url": "https://example/12",
            "merged_at": None,
        }

        result = github_collector._collect_for_pr(
            "org/repo", "reviewer", pull_request
        )

        self.assertEqual(result, [])

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
