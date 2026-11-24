"""礼宾行动对账：座位描述不触发邀请、冲突、撤回与岗位约定。"""
import unittest

from tests.workbench_tests.support import build


class ActionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.wb = build()
        cls.actions = {a.id: a for a in cls.wb.actions.values()}

    def by_source(self, source_id):
        return [a for a in self.wb.actions.values() if a.source_id == source_id]

    def test_seating_statement_does_not_create_invitation(self):
        # “坐东一排/还在东一排”是座位描述，不产生上台或起身动作
        seating_sources = {
            c.source_message_id
            for clauses in self.wb.clauses.values()
            for c in clauses
            if c.kind.value == "seating_note"
        }
        self.assertIn("M3", seating_sources)
        # 座位描述没有生成任何 invite_stage 动作
        for action in self.wb.actions.values():
            if action.source_id in seating_sources and action.verb.value == "invite_stage":
                self.fail("座位描述被误当成上台邀请")

    def test_relative_stage_request_blocked_pending_principal(self):
        # M4 的台口请求来自亲属 P2，不能成为已确认动作
        m4 = self.by_source("M4")
        self.assertTrue(m4)
        for action in m4:
            self.assertNotEqual(action.state.value, "confirmed")

    def test_self_withdrawal_supersedes_older_positive_action(self):
        # M6 不起身 -> 早先 M1/M3 的起身动作被取代
        superseded = {a.id for a in self.wb.actions.values()
                      if a.state.value == "superseded"}
        m1_actions = {a.id for a in self.by_source("M1")}
        self.assertTrue(m1_actions.issubset(superseded) or
                        all(a.state.value == "superseded" for a in self.by_source("M1")))

    def test_negative_actions_are_compatible(self):
        # 不去台口(hold) 与 不起身(no_rise) 都表示留在座位，可并存为 confirmed
        s2_negatives = [
            a for a in self.wb.actions.values()
            if a.segment_id == "S2" and a.verb in {"hold", "no_rise"}
        ]
        self.assertEqual(len(s2_negatives), 2)
        self.assertTrue(all(a.state.value == "confirmed" for a in s2_negatives))

    def test_conflicting_arrangements_go_to_staff(self):
        # S3：本人“别走台阶”与亲属“台口接受致礼”冲突，交现场负责人
        conflict_actions = [a for a in self.wb.actions.values() if a.state.value == "conflict"]
        conflict_ids = {a.id for a in conflict_actions}
        self.assertTrue(conflict_ids)
        conflict_tasks = [t for t in self.wb.open_tasks() if t.kind.value == "resolve_conflict"]
        self.assertTrue(conflict_tasks)
        self.assertTrue(all(t.ask_post == "现场负责人" for t in conflict_tasks))

    def test_unresolved_reference_never_creates_action(self):
        # 所有动作都必须有确定的目标人物
        for action in self.wb.actions.values():
            self.assertIsNotNone(action.target_person_id)

    def test_no_contradictory_executable_instructions(self):
        # 可执行动作中不应同时存在对同一人的肯定与否定指令
        executable = [a for a in self.wb.actions.values() if a.state.value == "confirmed"]
        keyed = {}
        for action in executable:
            keyed.setdefault((action.target_person_id, action.segment_id), []).append(action)
        for rows in keyed.values():
            verbs = {a.verb for a in rows}
            positives = verbs & {"invite_rise", "invite_stage", "escort"}
            negatives = verbs & {"hold", "no_rise"}
            self.assertFalse(positives and negatives)


if __name__ == "__main__":
    unittest.main()
