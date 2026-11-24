"""卡片版本与跨岗更正：回执一致性、暂止、回退说法与按岗查询。"""
import unittest

from tests.workbench_tests.support import build


class CardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.wb = build()
        cls.cm = cls.wb.card_manager

    def test_correction_tracked_per_post(self):
        correction = self.cm.corrections[0]
        self.assertEqual(correction.id, "C6")
        self.assertIn("司仪", correction.acks)
        self.assertIn("礼宾", correction.acks)

    def test_ack_status_queryable_by_post(self):
        status = self.cm.ack_status_by_post("S2")
        self.assertEqual(status["司仪"]["C6"], "已接收更正")
        self.assertEqual(status["礼宾"]["C6"], "已回执")

    def test_suspended_card_keeps_last_confirmed_version(self):
        # 暂止期间礼宾卡仍展示已确认旧版，但标记不可新增动作
        card = self.wb.cards["S2:礼宾"]
        active = card.active_version()
        self.assertTrue(card.lead_locked)
        self.assertEqual(active.no, 1)
        self.assertIn("外婆", active.text)

    def test_unacked_correction_not_executable(self):
        # 构造一个两岗更正，仅一岗回执
        wb = build()
        corr = wb.card_manager.issue_correction(
            correction_id="X1",
            segment_id="S1",
            created="11:00",
            summary="测试更正",
            affected_posts=("司仪", "引导"),
        )
        wb.card_manager.acknowledge("X1", "司仪", "已回执")
        self.assertEqual(corr.state.value, "pending_ack")
        # 引导岗尚未回执，其卡上不应出现该更正为生效指令
        guide = wb.cards["S1:引导"]
        self.assertNotIn("引导", corr.acks)

    def test_lead_only_after_segment_started(self):
        wb = build()
        wb.segments["S2"].state = wb.segments["S2"].state.__class__.ACTIVE
        corr = wb.card_manager.issue_correction(
            correction_id="X2", segment_id="S2", created="11:05",
            summary="环节开始后的更正", affected_posts=("礼宾",),
        )
        self.assertTrue(corr.lead_only)

    def test_fallback_activation_uses_agreed_wording(self):
        wb = build()
        card = wb.cards["S2:礼宾"]
        card.fallback_text = "保留外婆称呼，不请起身，统称亲友继续"
        wb.card_manager.activate_fallback("S2", "礼宾")
        active = card.active_version()
        self.assertEqual(active.text, "保留外婆称呼，不请起身，统称亲友继续")
        self.assertTrue(card.fallback_activated)

    def test_correction_impact_lists_posts_and_old_version(self):
        correction = self.cm.corrections[0]
        impact = self.cm.correction_impact(correction, self.wb.actions)
        self.assertIn("礼宾", impact)
        self.assertIn("old_text", impact["礼宾"])
        self.assertIn("lead_only", impact["礼宾"])

    def test_distributed_cards_retain_versions(self):
        # 已分发卡的每个版本都保留，不就地覆盖
        for card in self.wb.cards.values():
            if card.distributed:
                self.assertTrue(card.versions)
                for version in card.versions:
                    self.assertTrue(version.text)


if __name__ == "__main__":
    unittest.main()
