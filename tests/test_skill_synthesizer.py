import unittest

import skill_synthesizer


class FakeClient:
    def __init__(self):
        self.calls = []

    def call_text(
        self, deployment, system_prompt, user_prompt, *, temperature, response_format=None
    ):
        self.calls.append((deployment, system_prompt, user_prompt, temperature))
        return "---\nname: review-skill\ndescription: test\n---\n"


class SynthesisTests(unittest.TestCase):
    def test_uses_shared_adapter_for_single_pass(self):
        client = FakeClient()
        result = skill_synthesizer.synthesize_skill(
            client, "deployment", [], "reviewer"
        )

        self.assertIn("name: review-skill", result)
        self.assertEqual(len(client.calls), 1)

    def test_uses_shared_adapter_for_map_reduce(self):
        client = FakeClient()
        note = {
            "category": "testing",
            "severity": "suggestion",
            "original_issue": "No test.",
            "requested_change": "Add a test.",
            "rationale": "Prevent regressions.",
        }

        skill_synthesizer.synthesize_skill(
            client, "deployment", [note, note, note], "reviewer", max_notes_per_call=2
        )

        self.assertEqual(len(client.calls), 3)


if __name__ == "__main__":
    unittest.main()
