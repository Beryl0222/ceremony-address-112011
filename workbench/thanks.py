"""两方家庭各自的致谢安排。

甲、乙（以及丙场）在致谢/致礼环节的称呼和动作分别决策，
工作台只汇总各自已确认的部分，并列出仍需向谁核实的问题，
绝不把任一方亲属的建议当作当事人同意。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .consent import ConsentManager
from .models import (
    Clause,
    GenericApproval,
    Message,
    RefKind,
    ThanksPlan,
)


@dataclass
class ThanksPlanner:
    consent: ConsentManager
    generics: dict[str, list[GenericApproval]]
    plans: dict[tuple[str, str], ThanksPlan] = field(default_factory=dict)

    def build(
        self,
        segment_id: str,
        family: str,
        messages: list[Message],
        clauses_by_id: dict[str, list[Clause]],
        open_questions: tuple[str, ...] = (),
    ) -> ThanksPlan:
        approved: list[str] = []
        sources: list[str] = []
        deciders: list[str] = []

        for message in messages:
            if message.family != family:
                continue
            for clause in clauses_by_id.get(message.id, []):
                if clause.segment_hint not in {"致谢", "致礼"}:
                    continue
                for mention in clause.mentions:
                    if mention.kind in {RefKind.NAME, RefKind.TITLE, RefKind.KIN,
                                        RefKind.KIN_QUALIFIED} and mention.resolved:
                        if self.consent.can_speak_publicly(mention.person_id, mention.text):
                            approved.append(mention.text)
                            sources.append(message.id)
                            deciders.append(message.sender_id)
                # 本人确认的称呼类授权（参与方式动作不进致谢称呼表）
                for grant in self.consent.grants:
                    if (
                        grant.person_id == message.sender_id
                        and grant.segment_id in {"致谢", "致礼", "丙场"}
                        and grant.source_id == message.id
                        and grant.state.value == "granted"
                        and "参与方式" not in grant.note
                        and grant.term not in approved
                    ):
                        approved.append(grant.term)
                        sources.append(message.id)
                        deciders.append(message.sender_id)

        generic_allowed = any(
            approval.term in {"亲友", "各位"}
            for approval in self.generics.get(segment_id, [])
            if family in approval.families
        )
        plan = ThanksPlan(
            family=family,
            segment_id=segment_id,
            approved_terms=tuple(dict.fromkeys(approved)),
            generic_allowed=generic_allowed,
            decided_by=tuple(dict.fromkeys(deciders)),
            source_ids=tuple(dict.fromkeys(sources)),
            open_questions=open_questions,
        )
        self.plans[(family, segment_id)] = plan
        return plan

    def compare_families(self, segment_id: str) -> dict[str, ThanksPlan]:
        return {
            family: plan
            for (family, seg_id), plan in self.plans.items()
            if seg_id == segment_id
        }
