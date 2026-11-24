"""岗位视图、泛称兜底、致谢分治与公开存档。"""
import unittest

from tests.workbench_tests.support import build


class ViewArchiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.wb = build()

    def test_host_view_has_speakable_script_per_segment(self):
        expected = {
            "S1": "阿槐",
            "S2": "晚妹",
            "S3": "顾代",
            "S4": "林老师",
        }
        for segment_id, term in expected.items():
            view = self.wb.host_view(segment_id)
            self.assertIsNotNone(view["可朗读稿"], segment_id)
            self.assertIn(term, view["可朗读稿"])

    def test_host_view_excludes_backend_only_pronunciation(self):
        # P4 读音仅后台可见，司仪视图的读音清单不含 P4
        host_s2 = self.wb.host_view("S2")
        visible_ids = {note.get("person_id") for note in host_s2["司仪可见读音"]}
        self.assertIn("P1", visible_ids)
        self.assertNotIn("P4", visible_ids)

    def test_concierge_view_separate_from_host(self):
        view = self.wb.concierge_view("S2")
        self.assertIn("行动卡", view)
        self.assertIn("可执行动作", view)
        # 礼宾视图不携带司仪口播稿文本结构
        self.assertNotIn("讲稿", view)

    def test_generic_fallback_does_not_become_action(self):
        fallback = self.wb.script_builder.generic_fallback("S2")
        self.assertEqual(fallback, "亲友")
        # 没有任何动作的目标词形是泛称（动作只指向具体人物 id）
        for action in self.wb.actions.values():
            self.assertIsNotNone(action.target_person_id)
            person = self.wb.people[action.target_person_id]
            self.assertNotEqual(person.stage_name, "亲友")

    def test_thanks_plans_are_independent_per_family(self):
        plan_a = self.wb.build_thanks_plan("S2", "甲")
        plan_b = self.wb.build_thanks_plan("S2", "乙")
        self.assertIn("晚妹", plan_a.approved_terms)
        # 参与方式动作不混入称呼
        self.assertNotIn("原地不移动", plan_b.approved_terms)
        self.assertTrue(plan_a.generic_allowed and plan_b.generic_allowed)
        # 两方决定者不同，互不代签
        self.assertNotEqual(set(plan_a.decided_by), set(plan_b.decided_by))

    def test_public_archive_excludes_internal_and_registered_names(self):
        archive = self.wb.public_archive()
        blob = repr(archive)
        # 内部留言 M1/M3/M6 不进公开留言
        public_ids = {row["id"] for row in archive["公开留言"]}
        self.assertEqual(public_ids, {"M5", "M7"})
        # 后台登记名（叶冬、顾代若未授权公开词形）不出现在公开正文
        self.assertNotIn("叶冬", blob)
        # 同意表述里保留的是词形而非登记名
        terms = {row["表述"] for row in archive["同意表述"]}
        self.assertIn("林老师", terms)

    def test_evidence_chain_references_raw_messages(self):
        chain = self.wb.evidence_for("外婆")
        raw_ids = {item["原始留言"]["id"] for item in chain if "原始留言" in item}
        self.assertIn("M3", raw_ids)
        self.assertIn("M6", raw_ids)

    def test_every_audit_original_text_retained(self):
        # 原始留言逐字保留，可与解析结果对照
        messages = {m.id: m.text for m in self.wb.messages}
        raw_requests = {row["id"]: row["内容"] for row in self.wb.raw["requests"]}
        self.assertEqual(messages, raw_requests)


if __name__ == "__main__":
    unittest.main()
