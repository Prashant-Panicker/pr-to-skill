import base64
import subprocess
import threading
import unittest
from unittest.mock import patch

import github_collector


class RunGhTests(unittest.TestCase):
    @patch("github_collector.run_gh")
    def test_repository_tree_resolves_commit_and_returns_only_blobs(self, run_gh):
        run_gh.side_effect = [
            {"tree": {"sha": "tree-sha"}},
            {
                "truncated": False,
                "tree": [
                    {"path": "src", "type": "tree", "sha": "directory"},
                    {"path": "src/api.py", "type": "blob", "sha": "blob-sha", "size": 12},
                ],
            },
        ]

        result = github_collector.get_repository_tree("org/repo", "head-sha")

        self.assertEqual(
            result,
            [{"path": "src/api.py", "sha": "blob-sha", "size": 12}],
        )
        self.assertEqual(run_gh.call_args_list[0].args[0], [
            "api", "repos/org/repo/git/commits/head-sha",
        ])
        self.assertEqual(
            run_gh.call_args_list[0].kwargs,
            {"max_retries": 1, "timeout_seconds": 15},
        )
        self.assertEqual(run_gh.call_args_list[1].args[0], [
            "api", "repos/org/repo/git/trees/tree-sha?recursive=1",
        ])
        self.assertEqual(
            run_gh.call_args_list[1].kwargs,
            {"max_retries": 1, "timeout_seconds": 15},
        )

    @patch("github_collector.run_gh")
    def test_repository_blob_decodes_line_wrapped_utf8(self, run_gh):
        encoded = base64.b64encode(b"def validate():\n    return True\n").decode()
        run_gh.return_value = {
            "encoding": "base64",
            "content": f"{encoded[:12]}\n{encoded[12:]}",
        }

        result = github_collector.get_repository_blob(
            "org/repo", "blob-sha", max_bytes=100
        )

        self.assertEqual(result, "def validate():\n    return True\n")
        self.assertEqual(
            run_gh.call_args.kwargs,
            {"max_retries": 1, "timeout_seconds": 15},
        )

    @patch("github_collector.run_gh")
    def test_repository_tree_rejects_truncated_result(self, run_gh):
        run_gh.side_effect = [
            {"tree": {"sha": "tree-sha"}},
            {"truncated": True, "tree": []},
        ]

        with self.assertRaisesRegex(ValueError, "truncated"):
            github_collector.get_repository_tree("org/repo", "head-sha")

    @patch("github_collector.run_gh")
    @patch("github_collector.get_pr_reviews")
    def test_review_publisher_skips_head_already_posted(self, get_reviews, run_gh):
        get_reviews.return_value = [{
            "body": "Review\n\n<!-- pr-to-skill:abc123 -->",
            "html_url": "https://example/review/1",
        }]

        result = github_collector.GitHubReviewPublisher().publish(
            "org/repo", 12, "Review", "abc123"
        )

        self.assertEqual(result, "https://example/review/1")
        run_gh.assert_not_called()

    @patch("github_collector.run_gh")
    @patch("github_collector.get_pull_request")
    @patch("github_collector.get_pr_reviews", return_value=[])
    def test_review_publisher_posts_comment_for_new_head(
        self, get_reviews, get_pull_request, run_gh
    ):
        get_pull_request.return_value = {"head": {"sha": "abc123"}}
        run_gh.side_effect = [
            {"id": 42, "html_url": "https://example/review/2"},
            {"html_url": "https://example/review/2"},
        ]

        github_collector.GitHubReviewPublisher().publish(
            "org/repo", 12, "Review", "abc123"
        )

        create_request = run_gh.call_args_list[0].args[0]
        submit_request = run_gh.call_args_list[1].args[0]
        self.assertNotIn("event=COMMENT", create_request)
        self.assertIn("commit_id=abc123", create_request)
        self.assertTrue(any(
            "pr-to-skill:abc123" in value for value in create_request
        ))
        self.assertIn("repos/org/repo/pulls/12/reviews/42/events", submit_request)
        self.assertIn("event=COMMENT", submit_request)

    @patch("github_collector.run_gh")
    @patch("github_collector.get_pull_request")
    @patch("github_collector.get_pr_reviews", return_value=[])
    def test_review_publisher_deletes_pending_review_when_head_changes(
        self, get_reviews, get_pull_request, run_gh
    ):
        get_pull_request.return_value = {"head": {"sha": "new-head"}}
        run_gh.side_effect = [{"id": 42}, {}]

        with self.assertRaisesRegex(RuntimeError, "head changed"):
            github_collector.GitHubReviewPublisher().publish(
                "org/repo", 12, "Review", "old-head"
            )

        delete_request = run_gh.call_args_list[1].args[0]
        self.assertIn("--method", delete_request)
        self.assertIn("DELETE", delete_request)
        self.assertIn("repos/org/repo/pulls/12/reviews/42", delete_request)

    @patch("github_collector.run_gh")
    @patch("github_collector.get_pull_request")
    @patch("github_collector.get_pr_reviews")
    def test_review_publisher_replaces_matching_pending_review(
        self, get_reviews, get_pull_request, run_gh
    ):
        get_reviews.return_value = [{
            "id": 41,
            "state": "PENDING",
            "body": "Review\n\n<!-- pr-to-skill:abc123 -->",
        }]
        get_pull_request.return_value = {"head": {"sha": "abc123"}}
        run_gh.side_effect = [
            {},
            {"id": 42, "html_url": "https://example/review/2"},
            {"html_url": "https://example/review/2"},
        ]

        result = github_collector.GitHubReviewPublisher().publish(
            "org/repo", 12, "Review", "abc123"
        )

        self.assertEqual(result, "https://example/review/2")
        self.assertIn(
            "repos/org/repo/pulls/12/reviews/41",
            run_gh.call_args_list[0].args[0],
        )

    @patch("github_collector.run_gh")
    @patch("github_collector.get_pull_request")
    @patch("github_collector.get_pr_reviews", return_value=[])
    def test_review_publisher_deletes_pending_review_when_submit_fails(
        self, get_reviews, get_pull_request, run_gh
    ):
        get_pull_request.return_value = {"head": {"sha": "abc123"}}
        run_gh.side_effect = [
            {"id": 42},
            RuntimeError("submit failed"),
            {},
        ]

        with self.assertRaisesRegex(RuntimeError, "submit failed"):
            github_collector.GitHubReviewPublisher().publish(
                "org/repo", 12, "Review", "abc123"
            )

        self.assertIn(
            "repos/org/repo/pulls/12/reviews/42",
            run_gh.call_args_list[2].args[0],
        )

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

    @patch("github_collector.get_pull_request_diff")
    @patch("github_collector.get_pr_issue_comments", return_value=[])
    @patch("github_collector.get_pr_reviews", return_value=[])
    @patch("github_collector.get_pr_review_comments")
    def test_collects_any_author_replies_only_for_trusted_roots(
        self, review_comments, reviews, issue_comments, get_diff
    ):
        review_comments.return_value = [
            {
                "id": 10, "user": {"login": "trusted"}, "body": "Use a port.",
                "path": "service.py", "diff_hunk": "@@ old @@",
                "created_at": "2026-01-01", "html_url": "https://example/10",
            },
            {
                "id": 11, "in_reply_to_id": 10,
                "user": {"login": "author"}, "body": "Implemented.",
                "created_at": "2026-01-02", "html_url": "https://example/11",
            },
            {
                "id": 20, "user": {"login": "untrusted"}, "body": "Unrelated.",
                "created_at": "2026-01-01", "html_url": "https://example/20",
            },
        ]
        get_diff.return_value = "diff --git a/service.py b/service.py\n+class Port:"
        pull_request = {
            "number": 12, "title": "PR", "html_url": "https://example/12",
            "state": "closed", "merged_at": "2026-01-03",
        }

        result = github_collector._collect_for_pr(
            "org/repo", ["trusted", "another-trusted"], pull_request
        )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].reviewer, "trusted")
        self.assertEqual(result[0].replies[0]["author"], "author")
        self.assertIn("class Port", result[0].final_diff)

    @patch.dict("github_collector.os.environ", {"GITHUB_TOKEN": "token"}, clear=True)
    @patch("github_collector.requests.get")
    def test_rejects_oversized_complete_diff(self, get):
        response = get.return_value
        response.text = "x" * (github_collector.MAX_FINAL_DIFF_CHARS + 1)

        with self.assertRaisesRegex(ValueError, "maximum supported"):
            github_collector.get_pull_request_diff("org/repo", 12)

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
