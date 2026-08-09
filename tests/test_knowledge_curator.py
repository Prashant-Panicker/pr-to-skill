import unittest

from knowledge_curator import curate_notes


def note(comment_id, category, merged_at, *, selected=True, supersedes=False):
    return {
        "repo": "org/repo", "github_comment_id": comment_id,
        "comment_type": "review_comment", "category": category,
        "merged_at": merged_at, "implemented": selected,
        "include_in_vector_store": selected,
        "supersedes_prior_architecture": supersedes,
    }


class KnowledgeCuratorTests(unittest.TestCase):
    def test_supersession_removes_only_older_architecture(self):
        old = note(1, "architecture", "2026-01-01T00:00:00Z")
        security = note(2, "security", "2026-01-01T00:00:00Z")
        replacement = note(
            3, "architecture", "2026-02-01T00:00:00Z", supersedes=True
        )
        newer = note(4, "architecture", "2026-03-01T00:00:00Z")

        result = curate_notes([newer, replacement, security, old])

        self.assertNotIn(old, result)
        self.assertIn(security, result)
        self.assertIn(replacement, result)
        self.assertIn(newer, result)

    def test_unknown_merge_order_is_not_deleted(self):
        legacy = note(1, "architecture", "")
        replacement = note(
            2, "architecture", "2026-02-01T00:00:00Z", supersedes=True
        )

        self.assertIn(legacy, curate_notes([legacy, replacement]))

    def test_excludes_unselected_or_unimplemented_notes(self):
        self.assertEqual(curate_notes([
            note(1, "security", "2026-01-01", selected=False)
        ]), [])


if __name__ == "__main__":
    unittest.main()