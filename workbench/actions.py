"""礼宾行动对账。

行动卡上的每条动作必须同时满足：
1. 目标指代已解决（未解决的指代不产生动作，转核实任务）；
2. 与当事人确认的参与方式一致（本人最新留言可撤回旧安排）；
3. 不违反岗位约定（跨区时间、同时段单点动作、暂止期间不发新动作）；
4. 同一人在同一时段的两处安排若互不兼容，保留双方并交工作人员确认。

描述性座位语句（“让她坐前排/坐东一排”）只更新座位图与行动参考，
绝不生成上台邀请。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .consent import ConsentManager
from .models import (
    Action,
    ActionState,
    ActionVerb,
    Clause,
    ClauseKind,
    DeskNote,
    Message,
    ParsedAction,
    Segment,
    TaskKind,
    VerificationTask,
)

@dataclass
class ActionBuilder:
    segments: dict[str, Segment]
    consent: ConsentManager
    desk_notes: list[DeskNote]
    seating: dict[str, str]  # location -> person_id
    actions: list[Action] = field(default_factory=list)
    tasks: list[VerificationTask] = field(default_factory=list)
    b2_hold_required: bool = False
    _seq: int = 0

    def _new_id(self) -> str:
        self._seq += 1
        return f"A{self._seq:02d}"

    def _task(
        self, kind: TaskKind, issue: str, source_ids: tuple[str, ...],
        ask_person_id: str | None = None, ask_post: str | None = None,
    ) -> VerificationTask:
        task = VerificationTask(
            id=f"AT{len(self.tasks) + 1:02d}",
            kind=kind,
            issue=issue,
            ask_person_id=ask_person_id,
            ask_post=ask_post,
            source_ids=source_ids,
        )
        self.tasks.append(task)
        return task

    # ------------------------------------------------------------ 构建

    def build_from_clauses(self, messages: list[Message], clauses_by_id: dict[str, list[Clause]]) -> None:
        # 调用方按时间排序传入；本人最新留言可撤回旧安排
        self._message_order = {message.id: index for index, message in enumerate(messages)}
        self._clauses_by_id = clauses_by_id
        self._message_segment_hint = {
            message.id: self._single_hint(clauses_by_id.get(message.id, []))
            for message in messages
        }
        # 人物 -> 当天在留言中出现过的环节提示，用于无环节词留言的兜底
        self._person_hints: dict[str, set[str]] = {}
        self._person_verb_hints: dict[tuple[str, ActionVerb], set[str]] = {}
        for message in messages:
            for clause in clauses_by_id.get(message.id, []):
                for parsed in clause.actions:
                    if parsed.target.resolved and parsed.target.person_id and clause.segment_hint:
                        self._person_hints.setdefault(parsed.target.person_id, set()).add(
                            clause.segment_hint
                        )
                        self._person_verb_hints.setdefault(
                            (parsed.target.person_id, parsed.verb), set()
                        ).add(clause.segment_hint)
        # 起身/不起身同属致谢语境，互相补提示
        for person_id in self._person_hints:
            rise_hints = self._person_verb_hints.get((person_id, ActionVerb.INVITE_RISE), set())
            no_rise_hints = self._person_verb_hints.get((person_id, ActionVerb.NO_RISE), set())
            merged = rise_hints | no_rise_hints
            if merged:
                self._person_verb_hints[(person_id, ActionVerb.INVITE_RISE)] = set(merged)
                self._person_verb_hints[(person_id, ActionVerb.NO_RISE)] = set(merged)

        for message in messages:
            for clause in clauses_by_id.get(message.id, []):
                for parsed in clause.actions:
                    self._create_action(message, clause, parsed)
        self._apply_seating_notes(messages, clauses_by_id)
        self._cancel_intra_message()
        self._supersede_withdrawn()
        self._detect_conflicts()
        self._apply_desk_rules()

    @staticmethod
    def _single_hint(clauses: list[Clause]) -> str | None:
        hints = {clause.segment_hint for clause in clauses if clause.segment_hint}
        return next(iter(hints)) if len(hints) == 1 else None

    def _cancel_intra_message(self) -> None:
        """同一留言内既肯定又否定同一动作时，以本人明确的否定/原地为准。"""
        grouped: dict[tuple[str, str, str], list[Action]] = {}
        for action in self.actions:
            key = (action.source_id, action.target_person_id or "", action.segment_id)
            grouped.setdefault(key, []).append(action)
        for rows in grouped.values():
            negatives = [a for a in rows if a.negated or a.verb in {ActionVerb.HOLD, ActionVerb.NO_RISE}]
            positives = [a for a in rows if not negatives or a not in negatives]
            if negatives and positives:
                for positive in positives:
                    positive.state = ActionState.SUPERSEDED
                    positive.block_reason = (
                        f"同一留言 {positive.source_id} 内本人另有否定安排，以否定为准"
                    )
                    for negative in negatives:
                        negative.evidence = tuple(dict.fromkeys(negative.evidence + positive.evidence))

    def _create_action(self, message: Message, clause: Clause, parsed: ParsedAction) -> None:
        segment_id = self._segment_id(parsed.segment_hint, message.family)
        # 留言没有明确环节词时：先看整条留言的唯一环节，再按动词语义兜底
        if segment_id is None:
            hint = self._message_segment_hint.get(message.id)
            if hint is None and parsed.target.person_id:
                hint = self._verb_semantic_hint(parsed)
                if hint is None:
                    verb_hints = self._person_verb_hints.get(
                        (parsed.target.person_id, parsed.verb), set()
                    )
                    if len(verb_hints) == 1:
                        hint = next(iter(verb_hints))
                    else:
                        person_hints = self._person_hints.get(parsed.target.person_id, set())
                        hint = next(iter(person_hints)) if len(person_hints) == 1 else None
            if hint:
                segment_id = self._segment_id(hint, message.family)
        target = parsed.target
        if not target.resolved or target.person_id is None:
            self._task(
                TaskKind.VERIFY_REFERENCE,
                f"行动“{clause.text}”的目标指代未解决，不得发出带位/邀请指令",
                (message.id,),
                ask_post="现场负责人",
            )
            return

        action = Action(
            id=self._new_id(),
            segment_id=segment_id or "",
            target_person_id=target.person_id,
            verb=parsed.verb,
            location=parsed.location or self._location_from_text(clause.text),
            source_id=message.id,
            negated=parsed.negated,
            evidence=(message.id,),
        )

        # 与本人参与方式对账
        participation = self.consent.latest_participation(target.person_id, parsed.segment_hint)
        if participation is not None and participation.state.value == "withdrawn":
            action.state = ActionState.SUPERSEDED
            action.block_reason = f"当事人已在 {participation.source_id} 撤回原安排"
        elif participation is not None and participation.state.value == "pending":
            action.state = ActionState.BLOCKED
            action.block_reason = participation.note
            self._task(
                TaskKind.VERIFY_CONSENT,
                action.block_reason,
                (message.id,),
                ask_person_id=target.person_id,
            )
        elif self._contradicts_participation(parsed, participation):
            action.state = ActionState.BLOCKED
            action.block_reason = (
                f"与当事人确认的参与方式冲突（{participation.source_id}：{participation.term}）"
            )
            self._task(
                TaskKind.VERIFY_CONSENT,
                action.block_reason,
                (message.id,),
                ask_person_id=target.person_id,
            )
        else:
            action.state = ActionState.CONFIRMED
        self.actions.append(action)

    def _contradicts_participation(
        self, parsed: ParsedAction, participation
    ) -> bool:
        if participation is None or participation.state.value != "granted":
            return False
        term = participation.term
        if parsed.negated:
            return False
        if parsed.verb == ActionVerb.INVITE_RISE and term == "不请起身":
            return True
        if parsed.verb == ActionVerb.INVITE_STAGE and term in {"原地不移动", "致谢起身"}:
            # 本人只同意座位起身，到台口属于升级动作
            return True
        return False

    def _apply_seating_notes(
        self, messages: list[Message], clauses_by_id: dict[str, list[Clause]]
    ) -> None:
        for message in messages:
            for clause in clauses_by_id.get(message.id, []):
                if clause.kind != ClauseKind.SEATING_NOTE:
                    continue
                for mention in clause.mentions:
                    if mention.resolved and mention.person_id:
                        # 仅作座位参考，不产生任何动作
                        self.seating.setdefault(
                            self._location_from_text(clause.text) or mention.text,
                            mention.person_id,
                        )

    # ------------------------------------------------------------ 冲突

    @staticmethod
    def _verb_semantic_hint(parsed: ParsedAction) -> str | None:
        # 起身/不起身属于“致谢”；上台/台口属于“致礼”；引导移动属于“出场”
        if parsed.verb in {ActionVerb.INVITE_RISE, ActionVerb.NO_RISE}:
            return "致谢"
        if parsed.verb in {ActionVerb.INVITE_STAGE}:
            return "致礼"
        if parsed.verb == ActionVerb.ESCORT:
            return "出场"
        return None

    def _supersede_withdrawn(self) -> None:
        """本人后续的否定/原地安排（最新留言）取代此前的肯定动作。

        否定类动作之间（不起身、不去台口、原地不动）彼此兼容，可以并存，
        它们共同约束当事人“留在座位”。
        """
        by_target: dict[tuple[str, str], list[Action]] = {}
        for action in self.actions:
            if action.target_person_id is None or action.state == ActionState.BLOCKED:
                continue
            by_target.setdefault((action.target_person_id, action.segment_id), []).append(action)

        for rows in by_target.values():
            rows.sort(key=lambda item: self._message_order.get(item.source_id, 0))
            has_negative = any(
                row.negated or row.verb in {ActionVerb.HOLD, ActionVerb.NO_RISE} for row in rows
            )
            if not has_negative:
                continue
            latest_time = max(
                self._message_order.get(row.source_id, 0)
                for row in rows
                if row.negated or row.verb in {ActionVerb.HOLD, ActionVerb.NO_RISE}
            )
            for older in rows:
                if older.negated or older.verb in {ActionVerb.HOLD, ActionVerb.NO_RISE}:
                    continue  # 否定/原地动作保留，互相兼容
                if self._message_order.get(older.source_id, 0) < latest_time:
                    older.state = ActionState.SUPERSEDED
                    newer = next(
                        row for row in rows
                        if (row.negated or row.verb in {ActionVerb.HOLD, ActionVerb.NO_RISE})
                        and self._message_order.get(row.source_id, 0) == latest_time
                    )
                    older.block_reason = (
                        f"被当事人后续留言 {newer.source_id} 的“{newer.verb.value}”取代"
                    )
                    newer.evidence = tuple(dict.fromkeys(newer.evidence + older.evidence))

    def _detect_conflicts(self) -> None:
        by_target: dict[tuple[str, str], list[Action]] = {}
        for action in self.actions:
            if action.state in {ActionState.BLOCKED, ActionState.SUPERSEDED}:
                continue
            key = (action.target_person_id or "", action.segment_id)
            by_target.setdefault(key, []).append(action)

        for (person_id, segment_id), rows in by_target.items():
            if len(rows) < 2:
                continue

            def is_negative(row: Action) -> bool:
                return row.negated or row.verb in {ActionVerb.HOLD, ActionVerb.NO_RISE}

            negatives = [row for row in rows if is_negative(row)]
            positives = [row for row in rows if not is_negative(row)]
            # 否定类彼此兼容；只有否定与肯定并存，或两个肯定动作位置互斥才算冲突
            if not positives or not negatives:
                if len(positives) >= 2 and len({row.location for row in positives if row.location}) > 1:
                    pass  # 落到下面的位置冲突判断
                else:
                    continue

            locations = {row.location for row in rows if row.location}
            incompatible_locations = len(locations) > 1
            if (negatives and positives) or incompatible_locations:
                ids = tuple(row.id for row in rows)
                for row in rows:
                    row.state = ActionState.CONFLICT
                    row.conflicts_with = tuple(x for x in ids if x != row.id)
                self._task(
                    TaskKind.RESOLVE_CONFLICT,
                    (
                        f"人物 {person_id} 在环节 {segment_id or '未注明'} 被安排到互斥位置/动作："
                        + "；".join(f"{row.id}({row.verb.value}@{row.location or '未注明'})" for row in rows)
                    ),
                    tuple(sorted({row.source_id for row in rows})),
                    ask_post="现场负责人",
                )

    def _apply_desk_rules(self) -> None:
        rules = "\n".join(note.text for note in self.desk_notes)
        for action in self.actions:
            if action.state == ActionState.BLOCKED:
                continue
            segment = self.segments.get(action.segment_id)
            if segment is None:
                continue
            # 台口动作必须与环节地点一致，不能越过舞台通行约定
            if action.verb == ActionVerb.INVITE_STAGE and "台口" not in (segment.location or ""):
                action.state = ActionState.BLOCKED
                action.block_reason = (
                    f"环节 {segment.id} 地点为“{segment.location}”，台口动作越过舞台通行约定"
                )
                self._task(
                    TaskKind.RESOLVE_CONFLICT,
                    action.block_reason,
                    action.evidence,
                    ask_post=segment.receiving_post,
                )
        # B2 是流程规则：暂止期间不允许“新增”动作；已确认动作的可执行性
        # 由卡片 lead_locked / 回执状态控制，这里只暴露标志，不改写动作状态。
        self.b2_hold_required = "在现场负责人确认前不继续发动作" in rules

    # ------------------------------------------------------------ 工具

    def _segment_id(self, hint: str | None, family: str) -> str | None:
        if hint is None:
            return None
        for segment in self.segments.values():
            if segment.name == hint and (
                family in segment.family.replace("和", "") or family == segment.family
            ):
                return segment.id
        # 仅按环节名匹配首个
        for segment in self.segments.values():
            if segment.name == hint:
                return segment.id
        return None

    @staticmethod
    def _location_from_text(text: str) -> str | None:
        for loc in ("西侧台口", "东侧台口", "台口", "东一排", "东一位", "西一排", "西一位", "第一排", "台下东区", "台下西区", "东口"):
            if loc in text:
                return loc
        return None

    def actions_for_segment(self, segment_id: str) -> list[Action]:
        return [a for a in self.actions if a.segment_id == segment_id]

    def executable_for_post(self, post: str) -> list[Action]:
        rows = []
        for action in self.actions:
            segment = self.segments.get(action.segment_id)
            if segment is None or segment.receiving_post != post:
                continue
            if action.state == ActionState.CONFIRMED and not action.block_reason:
                rows.append(action)
        return rows
