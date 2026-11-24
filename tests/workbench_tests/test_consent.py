"""同意管理：本人确认 vs 亲属建议、撤回、读音可见性、登记名保护。"""
import unittest

from tests.workbench_tests.support import build


class ConsentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.wb = build()
        cls.consent = cls.wb.consent

    def test_self_directed_alias_is_granted(self):
        self.assertTrue(self.consent.can_speak_publicly("P1", "阿槐"))
        self.assertTrue(self.consent.can_speak_publicly("P6", "林老师"))

    def test_relative_suggestion_is_pending_never_granted(self):
        # M4：P2（亲属）觉得“外婆在台口接受致礼”方便，不能当作 P4 同意
        pending_sources = {issue[0] for issue in self.consent.pending_issues}
        self.assertIn("M4", pending_sources)
        m4_issues = [issue for issue in self.consent.pending_issues if issue[0] == "M4"]
        self.assertTrue(any(issue[1] == "P4" for issue in m4_issues))
        # P4 从未授予“到台口/上台”
        state = self.consent.term_state("P4", "到台口/上台")
        self.assertTrue(state is None or state.value != "granted")

    def test_later_self_message_withdraws_prior_participation(self):
        # M3 同意致谢起身，M6 本人“不起身”撤回
        rise = [g for g in self.consent.grants
                if g.person_id == "P4" and g.term == "致谢起身"]
        self.assertTrue(rise)
        self.assertEqual(rise[-1].state.value, "withdrawn")
        no_rise = [g for g in self.consent.grants
                   if g.person_id == "P4" and g.term == "不请起身"]
        self.assertTrue(no_rise and no_rise[-1].state.value == "granted")

    def test_pronunciation_visibility_by_post(self):
        p1 = self.wb.people["P1"]   # 读音范围 司仪
        p4 = self.wb.people["P4"]   # 读音范围 后台
        self.assertIsNotNone(self.consent.visible_pronunciation(p1, "司仪"))
        self.assertIsNone(self.consent.visible_pronunciation(p1, "礼宾"))
        self.assertIsNone(self.consent.visible_pronunciation(p4, "司仪"))
        self.assertIsNotNone(self.consent.visible_pronunciation(p4, "后台"))

    def test_registered_name_not_publicly_speakable(self):
        # 顾代、叶冬等登记名不能作为公开称呼（除非本人明确授权该词形）
        self.assertFalse(self.consent.can_speak_publicly("P4", "叶冬"))
        # P3 在致礼环节明确授权“顾代”，该词形在该环节可用
        gu_dai = [g for g in self.consent.grants if g.person_id == "P3" and g.term == "顾代"]
        self.assertTrue(gu_dai and gu_dai[-1].state.value == "granted")

    def test_verification_tasks_point_to_principal_not_relative(self):
        tasks = self.wb.open_tasks()
        consent_tasks = [t for t in tasks if t.kind.value == "verify_consent"]
        # 缺同意的问题必须指向当事人 P4，而不是提建议的 P2/P3
        ask = {t.ask_person_id for t in consent_tasks}
        self.assertIn("P4", ask)
        self.assertNotIn("P2", ask)

    def test_scoped_term_grant(self):
        # 晚妹仅用于致谢、阿槐用于出场
        wanmei = [g for g in self.consent.grants if g.term == "晚妹"]
        self.assertTrue(any(g.segment_id == "致谢" and g.state.value == "granted"
                            for g in wanmei))


if __name__ == "__main__":
    unittest.main()
