"""岗位行动卡与跨岗更正管理。

关键约束：
- 一张卡的更正必须被该岗位回执后才成为可执行版本；未回执期间
  active_version 仍返回上一版已确认内容，且把更正标记为待回执，
  现场不会出现互相矛盾的可执行指令。
- 一次更正可同时影响多个岗位；每个岗位的确认状态独立可查。
- 环节已开始（ACTIVE）后收到的更正 lead_only=True，只能由现场
  负责人处置；负责人可启用家庭已同意的回退说法（fallback）。
- 更正只修改 affected_posts/affected_terms/affected_action_ids
  列出的范围，其它卡与动作保持原版本。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import (
    CardStatus,
    CardVersion,
    Correction,
    CorrectionState,
    CueCard,
    RunOfShowItem,
    Segment,
    SegmentState,
)


@dataclass
class CardManager:
    cards: dict[str, CueCard]
    segments: dict[str, Segment]
    run_items: list[RunOfShowItem]
    corrections: list[Correction] = field(default_factory=list)
    # card key -> 已执行动作快照，环节开始后只减不增
    _lead_decisions: list[str] = field(default_factory=list)

    def card(self, segment_id: str, post: str) -> CueCard | None:
        return self.cards.get(f"{segment_id}:{post}")

    def cards_for_segment(self, segment_id: str) -> list[CueCard]:
        return [card for card in self.cards.values() if card.segment_id == segment_id]

    # ------------------------------------------------------------ 更正

    def issue_correction(
        self,
        correction_id: str,
        segment_id: str,
        created: str,
        summary: str,
        affected_posts: tuple[str, ...],
        *,
        affected_action_ids: tuple[str, ...] = (),
        affected_terms: tuple[str, ...] = (),
        fallback_text: str | None = None,
        source_items: tuple[str, ...] = (),
    ) -> Correction:
        segment = self.segments.get(segment_id)
        lead_only = bool(segment and segment.state in {SegmentState.ACTIVE, SegmentState.LOCKED})
        correction = Correction(
            id=correction_id,
            segment_id=segment_id,
            created=created,
            summary=summary,
            affected_posts=affected_posts,
            affected_action_ids=affected_action_ids,
            affected_terms=affected_terms,
            state=CorrectionState.PENDING_ACK,
            lead_only=lead_only,
            fallback_text=fallback_text,
        )
        self.corrections.append(correction)

        for post in affected_posts:
            card = self.card(segment_id, post)
            if card is None:
                continue
            new_version = CardVersion(
                no=len(card.versions) + 1,
                created=created,
                text=summary,
                raw_status="已接收更正",
                action_ids=affected_action_ids if post in affected_posts else (),
                source_refs=source_items,
            )
            card.versions.append(new_version)
            if card.status != CardStatus.EXECUTED:
                card.status = CardStatus.CORRECTED
        return correction

    def acknowledge(
        self, correction_id: str, post: str, tense: str = "已回执", item_id: str | None = None
    ) -> Correction:
        correction = self._get(correction_id)
        if post not in correction.affected_posts:
            raise ValueError(f"岗位 {post} 不在更正 {correction_id} 的影响范围内")
        correction.acks[post] = tense
        card = self.card(correction.segment_id, post)
        if card is not None and card.versions:
            card.versions[-1].acks[post] = tense
            if tense == "已执行":
                card.status = CardStatus.EXECUTED
        self._refresh_state(correction)
        return correction

    def _refresh_state(self, correction: Correction) -> None:
        if correction.state == CorrectionState.HELD:
            return
        if all(post in correction.acks for post in correction.affected_posts):
            correction.state = (
                CorrectionState.HELD if correction.lead_only else CorrectionState.ACKED
            )
            if not correction.lead_only:
                self._apply(correction)
        else:
            correction.state = CorrectionState.PENDING_ACK

    def hold(self, correction_id: str, decision_note: str) -> Correction:
        """负责人暂止：未开始的动作冻结，已开始的交现场负责人。"""
        correction = self._get(correction_id)
        correction.state = CorrectionState.HELD
        for post in correction.affected_posts:
            card = self.card(correction.segment_id, post)
            if card is not None:
                card.lead_locked = True
                if card.status != CardStatus.EXECUTED:
                    card.status = CardStatus.SUSPENDED
        self._lead_decisions.append(f"{correction_id}: {decision_note}")
        return correction

    def lead_apply(self, correction_id: str, decision_note: str) -> Correction:
        """现场负责人在环节开始后启用更正（或回退说法）。"""
        correction = self._get(correction_id)
        correction.state = CorrectionState.APPLIED
        for post in correction.affected_posts:
            correction.acks.setdefault(post, "负责人启用")
            card = self.card(correction.segment_id, post)
            if card is not None:
                card.lead_locked = False
                if card.status == CardStatus.SUSPENDED:
                    card.status = CardStatus.CORRECTED
        self._apply(correction)
        self._lead_decisions.append(f"{correction_id}: {decision_note}")
        return correction

    def activate_fallback(self, segment_id: str, post: str) -> CueCard:
        card = self.card(segment_id, post)
        if card is None or card.fallback_text is None:
            raise ValueError("该卡没有已同意的回退说法")
        card.fallback_activated = True
        card.lead_locked = False
        card.status = CardStatus.CORRECTED
        return card

    def _apply(self, correction: Correction) -> None:
        correction.state = CorrectionState.APPLIED
        for post in correction.affected_posts:
            card = self.card(correction.segment_id, post)
            if card is None:
                continue
            if card.status == CardStatus.SUSPENDED:
                card.lead_locked = False
            card.status = CardStatus.CORRECTED

    # ------------------------------------------------------------ 影响面

    def correction_impact(
        self, correction: Correction, actions: dict[str, Action]
    ) -> dict[str, dict[str, object]]:
        """工作人员更正前查询：受影响岗位、旧指令能否执行、是否只能现场处置。"""
        impact: dict[str, dict[str, object]] = {}
        segment = self.segments.get(correction.segment_id)
        for post in correction.affected_posts:
            card = self.card(correction.segment_id, post)
            old = card.active_version() if card else None
            affected_actions = [
                actions[aid] for aid in correction.affected_action_ids if aid in actions
            ]
            started = bool(segment and segment.state in {SegmentState.ACTIVE, SegmentState.LOCKED})
            impact[post] = {
                "card_id": card.id if card else None,
                "old_version": old.no if old else None,
                "old_text": old.text if old else None,
                "old_still_executable": (
                    not started and old is not None and post not in correction.acks
                ),
                "ack_received": correction.acks.get(post),
                "lead_only": correction.lead_only,
                "affected_actions": [
                    {
                        "action_id": action.id,
                        "state": action.state.value,
                        "block_reason": action.block_reason,
                    }
                    for action in affected_actions
                ],
            }
        return impact

    def ack_status_by_post(self, segment_id: str) -> dict[str, dict[str, str]]:
        result: dict[str, dict[str, str]] = {}
        for correction in self.corrections:
            if correction.segment_id != segment_id:
                continue
            for post in correction.affected_posts:
                result.setdefault(post, {})[correction.id] = correction.acks.get(post, "尚未回执")
        return result

    def pending_corrections(self, post: str) -> list[Correction]:
        return [
            correction for correction in self.corrections
            if post in correction.affected_posts and post not in correction.acks
        ]

    def _get(self, correction_id: str) -> Correction:
        for correction in self.corrections:
            if correction.id == correction_id:
                return correction
        raise KeyError(f"更正不存在：{correction_id}")
