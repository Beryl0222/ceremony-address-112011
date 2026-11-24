"""同意管理：决定一条称呼或做法能否公开使用、能否落成行动。

关键约定：
- 当事人本人在留言中确认（“叫我X”“同意念X”“可以仍称X”）才是 GRANTED；
  其他亲属的建议（“我觉得方便”“请卡上给她写…”）只能是 PENDING，
  并生成核实任务，向当事人本人核实。
- 本人后续留言可以撤回或收窄此前同意（WITHDRAWN），以时间在后者为准。
- 读音注记按人物表的可见岗位控制；登记名仅后台核身份，默认不公开。
- 留言的“范围”约束的是原文是否能进公开存档，不改变本人对台上称呼
  的明确授权。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .models import (
    ActionVerb,
    Clause,
    ConsentState,
    Message,
    ParsedAction,
    Person,
    RefKind,
    UsageGrant,
)

_TERM_DIRECTIVES = re.compile(
    r"(?:叫我|称我|请称|仍叫|叫|称|念|称呼|仍称)([^，。；]+?)(?:就好|$)"
)
# 带环节/场次限定的称呼指令：致礼时称顾代 / 致谢请称晚妹 / 丙场叫林老师
_SCOPED_DIRECTIVE = re.compile(
    r"(?P<segment>致礼|致谢|出场|进场|[甲乙丙]场)(?:时|环节)?(?:请)?(?:仍)?(?:叫|称|念|称呼)(?P<term>[^，。；]+?)(?:$|[，。；])"
)
_CONSENT_MARKERS = ("同意", "可以", "仍可", "请称", "叫我")
_DECLINE_MARKERS = ("不叫", "不称", "不念", "不要称", "别叫")


@dataclass
class ConsentManager:
    people: dict[str, Person]
    grants: list[UsageGrant] = field(default_factory=list)
    grant_index: dict[tuple[str | None, str], UsageGrant] = field(default_factory=dict)
    # 待核实的同意问题：(source_id, target_id, term) -> 描述
    pending_issues: list[tuple[str, str, str, str]] = field(default_factory=list)

    def is_delegate(self, principal_id: str, actor_id: str) -> bool:
        if principal_id == actor_id:
            return True
        person = self.people.get(principal_id)
        return bool(person and actor_id in person.delegates)

    # ------------------------------------------------------------- 录入

    def record(
        self,
        person_id: str | None,
        term: str,
        scope: str,
        state: ConsentState,
        source_id: str,
        granted_by: str | None,
        segment_id: str | None = None,
        note: str = "",
    ) -> UsageGrant:
        # 同人物、同词形、同环节、同来源的重复记录直接复用，避免一个句式产生多条
        if person_id is not None:
            existing = self.grant_index.get((person_id, term))
            if (
                existing is not None
                and existing.segment_id == segment_id
                and existing.source_id == source_id
                and existing.state == state
            ):
                return existing
        grant = UsageGrant(
            person_id=person_id,
            term=term,
            scope=scope,
            state=state,
            source_id=source_id,
            granted_by=granted_by,
            segment_id=segment_id,
            note=note,
        )
        self.grants.append(grant)
        if person_id is not None:
            # 同人同词以时间靠后的留言为准（调用方按时间顺序录入）
            self.grant_index[(person_id, term)] = grant
        return grant

    def ingest_message(self, message: Message, clauses: list[Clause]) -> None:
        for clause in clauses:
            self._ingest_clause(message, clause)

    def _ingest_clause(self, message: Message, clause: Clause) -> None:
        text = clause.text
        segment_id = clause.segment_hint

        # 带环节限定的本人称呼授权（致礼时称顾代 / 出场仍叫阿槐）
        for match in _SCOPED_DIRECTIVE.finditer(text):
            term = match.group("term").strip("‘’“”'\" ")
            term = term.replace("我们家", "").replace("我家", "")
            scoped_segment = match.group("segment")
            target = self._speaker_or_unique(clause, message)
            if term and target and self.is_delegate(target, message.sender_id):
                self.record(
                    target, term, "公开", ConsentState.GRANTED, message.id,
                    message.sender_id, scoped_segment,
                    note=f"本人确认{scoped_segment}环节称呼",
                )

        # 本人自称别名：“叫我阿槐就好”“我也叫阿槐”
        for mention in clause.mentions:
            if mention.kind is RefKind.SELF and mention.resolved:
                for match in re.finditer(r"叫我(?P<term>[^，。；]+?)(?:就好|$)|我也叫(?P<term2>[^，。；,]+)", text):
                    term = match.group("term") or match.group("term2")
                    if term:
                        self.record(
                            mention.person_id, term, "公开", ConsentState.GRANTED,
                            message.id, message.sender_id, segment_id,
                            note="本人确认台上称呼",
                        )

        # 以动词开头的称呼指令，按指称对象判断授权人
        for match in _TERM_DIRECTIVES.finditer(text):
            term = match.group(1).strip("‘’“”'\" ")
            if not term:
                continue
            target = self._directive_target(clause, term)
            if target is None:
                continue
            if self.is_delegate(target, message.sender_id):
                state = ConsentState.GRANTED
                note = "本人确认"
            else:
                state = ConsentState.PENDING
                note = f"由 {message.sender_id} 建议，未经当事人同意"
                self.pending_issues.append(
                    (message.id, target, term, f"称呼“{term}”仅获亲属建议，需当事人本人确认")
                )
            if any(marker in text for marker in _DECLINE_MARKERS):
                state = ConsentState.DECLINED
            self.record(
                target, term, message.scope, state, message.id,
                message.sender_id, segment_id, note=note,
            )

        # 行动诉求 -> 参与方式同意
        for parsed in clause.actions:
            self._ingest_action(message, parsed, text, segment_id)

    def _speaker_or_unique(self, clause: Clause, message: Message) -> str | None:
        """带环节限定的称呼指令默认指向发言人本人（本人安排自己的台上称呼）。"""
        for mention in clause.mentions:
            if mention.kind is RefKind.SELF and mention.resolved:
                return mention.person_id
        if message.sender_id:
            return message.sender_id
        return self._directive_target(clause, "")

    def _directive_target(self, clause: Clause, term: str) -> str | None:
        # 自称语句
        for mention in clause.mentions:
            if mention.kind is RefKind.SELF and mention.resolved:
                return mention.person_id
        # 词形与某指称原文一致
        for mention in clause.mentions:
            if mention.resolved and (term in mention.text or mention.text in term):
                if mention.kind != RefKind.GENERIC:
                    return mention.person_id
        # 退而求其次：句中唯一第三人称
        third = [m for m in clause.mentions if m.resolved and m.kind
                 not in (RefKind.SELF, RefKind.GENERIC, RefKind.PRONOUN)]
        if len(third) == 1:
            return third[0].person_id
        return None

    def _ingest_action(
        self,
        message: Message,
        action: ParsedAction,
        text: str,
        segment_id: str | None,
    ) -> None:
        target = action.target
        if not target.resolved or target.person_id is None:
            return
        person_id = target.person_id
        own = self.is_delegate(person_id, message.sender_id)
        label = _action_label(action.verb, action.negated)

        if not own:
            self.pending_issues.append(
                (message.id, person_id, label,
                 f"{message.sender_id} 为当事人安排“{label}”，需本人确认")
            )
            return

        if action.negated or action.verb in (ActionVerb.HOLD, ActionVerb.NO_RISE):
            # 本人拒绝/收窄此前安排：撤回同一环节同主题的旧授权
            self._withdraw_prior(person_id, segment_id, label)
            self.record(
                person_id, label, message.scope, ConsentState.GRANTED,
                message.id, message.sender_id, segment_id,
                note="本人确认的参与方式（否定/原地）",
            )
            return

        self.record(
            person_id, label, message.scope, ConsentState.GRANTED,
            message.id, message.sender_id, segment_id,
            note="本人确认的参与方式",
        )

    def _withdraw_prior(self, person_id: str, segment_id: str | None, label: str) -> None:
        """否定动作撤回同环节内与之对立的肯定参与方式。"""
        opposite = _OPPOSITE_LABELS.get(label)
        for grant in self.grants:
            if grant.person_id != person_id or grant.segment_id != segment_id:
                continue
            if grant.state != ConsentState.GRANTED:
                continue
            if opposite is not None and grant.term in opposite or grant.term == label:
                grant.state = ConsentState.WITHDRAWN
                grant.note += f"；被后续否定安排（{label}）收窄"

    def _find_participation(self, person_id: str, segment_id: str | None) -> UsageGrant | None:
        for grant in reversed(self.grants):
            if grant.person_id == person_id and grant.segment_id == segment_id:
                return grant
        return None

    # ------------------------------------------------------------- 查询

    def term_state(self, person_id: str, term: str) -> ConsentState | None:
        grant = self.grant_index.get((person_id, term))
        return grant.state if grant else None

    def can_speak_publicly(self, person_id: str | None, term: str) -> bool:
        if person_id is None:
            return False
        grant = self.grant_index.get((person_id, term))
        return bool(grant and grant.state == ConsentState.GRANTED and grant.scope == "公开")

    def visible_pronunciation(self, person: Person, post: str) -> str | None:
        if post in person.pronunciation_visibility:
            return person.pronunciation
        return None

    def participation_for(self, person_id: str, segment_id: str | None) -> list[UsageGrant]:
        return [
            grant for grant in self.grants
            if grant.person_id == person_id
            and grant.segment_id == segment_id
            and "参与方式" in grant.note
        ]

    def latest_participation(self, person_id: str, segment_id: str | None) -> UsageGrant | None:
        rows = self.participation_for(person_id, segment_id)
        return rows[-1] if rows else None


def _action_label(verb: ActionVerb, negated: bool) -> str:
    labels = {
        ActionVerb.INVITE_RISE: "致谢起身",
        ActionVerb.INVITE_STAGE: "到台口/上台",
        ActionVerb.ESCORT: "引导移动",
        ActionVerb.SEAT: "座位安排",
        ActionVerb.HOLD: "原地不移动",
        ActionVerb.NO_RISE: "不请起身",
    }
    return labels.get(verb, verb.value)


# 否定/原地标签 -> 它所撤回的肯定标签
_OPPOSITE_LABELS: dict[str, set[str]] = {
    "不请起身": {"致谢起身"},
    "原地不移动": {"致谢起身", "到台口/上台", "引导移动"},
}
