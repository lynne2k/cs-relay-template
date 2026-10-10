import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROMPTS = {
    "age_shirt_removal": ROOT / "templates" / "prompts" / "age_transition_shirt_removal_v2.txt",
    "age_palm_cover": ROOT / "templates" / "prompts" / "age_transition_palm_cover_v2.txt",
    "handoff": ROOT / "templates" / "prompts" / "player_handoff_v2.txt",
}


class PromptTemplateTests(unittest.TestCase):
    def test_templates_have_no_face_accessory_cue_words(self):
        for kind, path in PROMPTS.items():
            text = path.read_text(encoding="utf-8").lower()
            for token in ("eyewear", "glasses", "spectacles"):
                self.assertNotIn(token, text, f"{kind} prompt contains {token}")

    def test_templates_are_player_and_team_agnostic(self):
        forbidden = (
            "s1mple", "guardian", "device", "fallen", "coldzera", "olofmeister",
            "get_right", "f0rest", "shox", "kennys", "dank1ng", "zywoo",
            "bc.game", "natus vincere", "astralis", "furia", "fnatic", "tyloo",
            "verygames", "vitality", "dignitas", "ninjas in pyjamas",
        )
        for kind, path in PROMPTS.items():
            text = path.read_text(encoding="utf-8").lower()
            for token in forbidden:
                self.assertIsNone(
                    re.search(rf"(?<![a-z0-9_]){re.escape(token)}(?![a-z0-9_])", text),
                    f"{kind} prompt contains case-specific token {token}",
                )

    def test_palm_cover_requires_complete_edge_to_edge_occlusion(self):
        text = PROMPTS["age_palm_cover"].read_text(encoding="utf-8").lower()
        required_phrases = (
            "all four corners",
            "every edge",
            "every image pixel",
            "palm skin extends beyond the top, bottom, left, and right image boundaries",
            "no clothing, face, hair, body, room, sliver, border, or gap is visible",
        )
        for phrase in required_phrases:
            self.assertIn(phrase, text)


if __name__ == "__main__":
    unittest.main()
