import unittest

from configuration import trusted_reviewers


class ConfigurationTests(unittest.TestCase):
    def test_returns_multiple_unique_reviewers_case_insensitively(self):
        result = trusted_reviewers({
            "person": {"github_usernames": ["Alice", "bob", "alice"]}
        })

        self.assertEqual(result, ["Alice", "bob"])

    def test_accepts_legacy_singular_reviewer(self):
        self.assertEqual(
            trusted_reviewers({"person": {"github_username": "reviewer"}}),
            ["reviewer"],
        )

    def test_rejects_empty_reviewer_list(self):
        with self.assertRaisesRegex(ValueError, "At least one"):
            trusted_reviewers({"person": {"github_usernames": []}})


if __name__ == "__main__":
    unittest.main()