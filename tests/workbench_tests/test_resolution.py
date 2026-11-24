"""指代解析：自称、第三人称、称谓限定、重名与跨家庭消歧。"""
import unittest

from tests.workbench_tests.support import build


class ResolutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.wb = build()

    def mentions(self, message_id):
        return {m.text: m for clause in self.wb.clauses[message_id] for m in clause.mentions}

    def test_self_reference_resolves_to_sender(self):
        m1 = self.mentions("M1")
        self.assertEqual(m1["我"].person_id, "P1")
        self.assertEqual(m1["我"].kind.value, "self")
        m3 = self.mentions("M3")
        self.assertEqual(m3["我"].person_id, "P4")

    def test_appositive_title_resolves_to_sister(self):
        # “林女士是我姐姐”——林女士是 P2，而不是发言人 P1
        m1 = self.mentions("M1")
        self.assertEqual(m1["林女士"].person_id, "P2")
        self.assertEqual(m1["姐姐"].person_id, "P2")

    def test_qualified_kin_crosses_family(self):
        # “对方外婆”限定指向乙家 P4，而非甲家任何亲属
        m1 = self.mentions("M1")
        qualified = next(m for m in self.wb.mentions_of("M1") if m.kind.value == "kin_qualified")
        self.assertEqual(qualified.person_id, "P4")
        self.assertEqual(qualified.qualifier, "对方")

    def test_duplicate_nickname_disambiguated_by_speaker_family(self):
        # 两位“阿槐”：P1（甲）与 P3（乙），按留言家庭分别归并
        m1 = self.mentions("M1")
        m2 = self.mentions("M2")
        self.assertEqual(m1["阿槐"].person_id, "P1")
        self.assertEqual(m2["阿槐"].person_id, "P3")

    def test_duplicate_registered_name_kept_distinct(self):
        # 两位“林晚”（P1、P6）不能合并；M7 经“与我无关”声明锁定到发言人 P6
        m7 = self.mentions("M7")
        mention = m7["林晚"]
        self.assertEqual(mention.person_id, "P6")
        self.assertTrue(mention.resolved)
        self.assertEqual(set(mention.candidates), {"P1", "P6"})  # 重名候选留痕
        # P1 自身语境（M1）里的“阿槐”仍归 P1，没有被 P6 污染
        self.assertEqual(self.mentions("M1")["阿槐"].person_id, "P1")

    def test_pronoun_resolves_via_salience(self):
        # “到致谢再请她在座位起身”的“她”回指对方外婆 P4
        m1 = self.mentions("M1")
        self.assertEqual(m1["她"].person_id, "P4")

    def test_seating_qualifier_maps_to_person(self):
        self.assertEqual(self.wb.resolver.seating.location_to_person.get("东一排"), "P4")
        self.assertEqual(self.wb.resolver.seating.evidence.get("东一排"), "M3")

    def test_generic_term_has_no_person_and_cannot_be_action_target(self):
        m1_actions = [a for c in self.wb.clauses["M1"] for a in c.actions]
        self.assertTrue(m1_actions)
        for action in m1_actions:
            self.assertNotEqual(action.target.kind.value, "generic")
            self.assertTrue(action.target.resolved)

    def test_stage_name_kin_term_seeded(self):
        # P4 台上名“外婆”、P5 台上名“舅舅”建立家庭内称谓
        self.assertEqual(self.wb.resolver.kin_map.get(("乙", "外婆", None)), "P4")
        self.assertEqual(self.wb.resolver.kin_map.get(("甲", "舅舅", None)), "P5")


if __name__ == "__main__":
    unittest.main()
