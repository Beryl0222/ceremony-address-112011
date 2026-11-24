"""司仪/礼宾协同工作台（门面）。

把报名名单、亲友留言、司仪稿、舞台时段和已分发卡片接成一条可审计
链路；所有子模块只负责自己的判断，Workbench 负责按顺序组织并提供
岗位视图。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .actions import ActionBuilder
from .audit import AuditLog, PublicArchive
from .cards import CardManager
from .consent import ConsentManager
from .loader import (
    load_corpus,
    load_desk_notes,
    load_messages,
    load_people,
    load_run_of_show,
    load_segments,
)
from .models import (
    Action,
    ActionState,
    CardStatus,
    CardVersion,
    Clause,
    ClauseKind,
    Correction,
    CorrectionState,
    DeskNote,
    GenericApproval,
    Message,
    Person,
    RefKind,
    ScriptLine,
    Segment,
    SegmentState,
    TaskKind,
    ThanksPlan,
    VerificationTask,
    to_plain,
)
from .parser import Lexicon, parse_message, segment_hint_of
from .resolver import Resolver
from .script import ScriptBuilder
from .thanks import ThanksPlanner

_CN_SPLIT = re.compile(r"[，,；;。]")


def re_split_cn(text: str) -> list[str]:
    return [part.strip() for part in _CN_SPLIT.split(text) if part.strip()]


def segment_hint_for(text: str) -> str | None:
    return segment_hint_of(text)


class Workbench:
    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir)
        self.raw = load_corpus(self.data_dir)

        # 基础资料
        self.people: dict[str, Person] = load_people(self.raw["people"])
        self.messages: list[Message] = load_messages(self.raw["requests"])
        self.segments: dict[str, Segment] = load_segments(self.raw["programme"])
        self.desk_notes: list[DeskNote] = load_desk_notes(self.raw["desk_notes"])
        self.run_items, self.cards = load_run_of_show(self.raw["run_of_show"])

        # 组件
        self.lexicon = Lexicon.build(self.people)
        self.resolver = Resolver(self.people, self.lexicon, self.segments)
        self.consent = ConsentManager(self.people)
        self.audit = AuditLog()
        self.generics: dict[str, list[GenericApproval]] = {}

        self.clauses: dict[str, list[Clause]] = {}
        # 留言按收到时间处理（同日 HH:MM[:SS] 可字典序比较），但展示保留原序
        self.chronological = sorted(self.messages, key=lambda m: m.time)
        self._build_pipeline()

    # ============================================================ 构建

    def _build_pipeline(self) -> None:
        # 1. 第一遍解析：切句/抽词表指称
        for message in self.chronological:
            self.clauses[message.id] = parse_message(message, self.lexicon)

        # 2. 学习别名/亲属/座位（会把晚妹等新称谓写回 lexicon）
        self.resolver.learn(self.chronological, self.clauses)

        # 3. 第二遍解析：让新学别名进入指称，再做指代消歧
        for message in self.chronological:
            self.clauses[message.id] = parse_message(message, self.lexicon)
        for message in self.chronological:
            self.resolver.resolve_clauses(message, self.clauses[message.id])

        # 4. 泛称认可（desk notes 与留言中的泛称安排）
        self._build_generics()

        # 5. 同意与参与方式
        for message in self.chronological:
            self.consent.ingest_message(message, self.clauses[message.id])
        self._consent_pending_to_tasks()

        # 5. 行动对账
        seating = dict(self.resolver.seating.location_to_person)
        self.action_builder = ActionBuilder(
            segments=self.segments,
            consent=self.consent,
            desk_notes=self.desk_notes,
            seating=seating,
        )
        self.action_builder.build_from_clauses(self.messages, self.clauses)
        self.actions: dict[str, Action] = {a.id: a for a in self.action_builder.actions}

        # 6. 卡片管理 + 从 run_of_show 还原更正链
        self.card_manager = CardManager(self.cards, self.segments, self.run_items)
        self._replay_run_of_show()

        # 7. 司仪稿
        self.script_builder = ScriptBuilder(self.resolver, self.consent, self.generics)
        self._seed_scripts_from_messages()

        # 8. 致谢方案
        self.thanks_planner = ThanksPlanner(self.consent, self.generics)
        self.thanks_plans: dict[tuple[str, str], ThanksPlan] = {}

        # 9. 审计初始事件
        self._record_initial_audit()

    def _build_generics(self) -> None:
        for note in self.desk_notes:
            if "统称" in note.text or "亲友" in note.text:
                families = frozenset(ch for ch in ("甲", "乙", "丙") if ch in note.text
                                     or ("甲乙" in note.text and ch in ("甲", "乙")))
                if not families:
                    families = frozenset(("甲", "乙"))
                approval = GenericApproval(
                    segment_id=note.segment_id,
                    term="亲友",
                    families=families,
                    source_id=note.id,
                )
                self.generics.setdefault(note.segment_id, []).append(approval)

    def _consent_pending_to_tasks(self) -> None:
        for source_id, person_id, term, issue in self.consent.pending_issues:
            self.resolver._add_task(  # noqa: SLF001
                TaskKind.VERIFY_CONSENT,
                issue,
                (source_id,),
                ask_person_id=person_id,
            )

    def _replay_run_of_show(self) -> None:
        """把语料里的更正/暂止/回执重放为 Correction 生命周期。"""
        pending: dict[str, Correction] = {}
        for item in self.run_items:
            if item.tense == "已接收更正":
                # 司仪岗已接收的口播更正（如 C6），先只影响司仪岗
                correction = self.card_manager.issue_correction(
                    correction_id=item.id,
                    segment_id=item.segment_id,
                    created=item.created,
                    summary=item.text,
                    affected_posts=(item.post,),
                    affected_terms=self._terms_in_text(item.text),
                    source_items=(item.id,),
                )
                correction.acks[item.post] = "已接收更正"
                self.card_manager._refresh_state(correction)  # noqa: SLF001
                pending[item.segment_id] = correction
            elif item.tense == "尚未回执":
                # C7：负责人把同一更正转发给礼宾岗，等待回执
                correction = pending.get(item.segment_id)
                if correction is None:
                    correction = self.card_manager.issue_correction(
                        correction_id=item.id,
                        segment_id=item.segment_id,
                        created=item.created,
                        summary=item.text,
                        affected_posts=("礼宾",),
                        source_items=(item.id,),
                    )
                    pending[item.segment_id] = correction
                elif "礼宾" not in correction.affected_posts:
                    correction.affected_posts = correction.affected_posts + ("礼宾",)
                    card = self.card_manager.card(item.segment_id, "礼宾")
                    if card is not None:
                        card.versions.append(CardVersion(
                            no=len(card.versions) + 1,
                            created=item.created,
                            text=correction.summary,
                            raw_status="尚未回执",
                            source_refs=(item.id,),
                        ))
                        card.status = CardStatus.CORRECTED
                correction.state = CorrectionState.PENDING_ACK
            elif item.tense == "暂止":
                correction = pending.get(item.segment_id)
                if correction is not None:
                    self.card_manager.hold(correction.id, item.text)
                self._set_segment_state(item.segment_id, SegmentState.LOCKED)
            elif item.tense == "已回执":
                correction = pending.get(item.segment_id)
                if correction is not None:
                    self.card_manager.acknowledge(correction.id, item.post, "已回执", item.id)
            elif item.tense == "开始引导":
                self._set_segment_state(item.segment_id, SegmentState.ACTIVE)
            elif item.tense == "已执行":
                self._set_segment_state(item.segment_id, SegmentState.DONE)
                card = self.card_manager.card(item.segment_id, item.post)
                if card is not None:
                    card.status = CardStatus.EXECUTED

    @staticmethod
    def _terms_in_text(text: str) -> tuple[str, ...]:
        terms = []
        for term in ("外婆", "阿槐", "林老师", "林女士", "晚妹", "亲友"):
            if term in text:
                terms.append(term)
        return tuple(terms)

    def _set_segment_state(self, segment_id: str, state: SegmentState) -> None:
        if segment_id in self.segments:
            self.segments[segment_id].state = state

    def _seed_scripts_from_messages(self) -> None:
        """从含明确口播称呼指令的留言生成司仪稿初版（按称呼拆分到环节）。"""
        self.script_builder.generics = self.generics
        for message in self.chronological:
            for clause in self.clauses.get(message.id, []):
                if clause.kind != ClauseKind.HOST_SCRIPT:
                    continue
                spoken = [
                    mention for mention in clause.mentions
                    if mention.kind in {RefKind.NAME, RefKind.TITLE, RefKind.KIN,
                                        RefKind.KIN_QUALIFIED, RefKind.GENERIC}
                ]
                if not spoken:
                    continue
                # 按子句切分称呼指令，每个子句独立转写并归到对应环节
                for sub in re_split_cn(clause.text):
                    # 否定/说明性语句（“与我无关”“不是”）不是口播指令
                    if "无关" in sub or "不是" in sub:
                        continue
                    sub_mentions = [
                        mention for mention in clause.mentions
                        if mention.text and mention.text in sub
                    ]
                    if not any(m.kind in {RefKind.NAME, RefKind.TITLE, RefKind.KIN,
                                         RefKind.KIN_QUALIFIED} for m in sub_mentions):
                        continue
                    hint = segment_hint_for(sub) or clause.segment_hint
                    segment_id = self._hint_to_segment(hint, message.family)
                    if segment_id is None:
                        # 丙场等独立场次：按家庭直接匹配
                        segment_id = self._family_segment(message.family)
                    if segment_id is None:
                        continue
                    host_text = self.script_builder.compose_host_sentence(
                        sub, sub_mentions, message.sender_id
                    )
                    line = self.script_builder.build_line(
                        segment_id=segment_id,
                        host_text=host_text,
                        source_ids=(message.id,),
                        family_hint=message.family,
                    )
                    self.audit.record(
                        message.time, "script_seed",
                        f"环节 {segment_id} 生成口播稿 {line.id}",
                        (message.id,), public_ok=line.consent_ok,
                        payload={"line": to_plain(line)},
                    )

    def _family_segment(self, family: str) -> str | None:
        for segment in self.segments.values():
            if segment.family == family:
                return segment.id
        return None

    def _hint_to_segment(self, hint: str | None, family: str) -> str | None:
        if hint is None:
            return None
        for segment in self.segments.values():
            if segment.name == hint and (
                family in segment.family.replace("和", "") or family == segment.family
            ):
                return segment.id
        for segment in self.segments.values():
            if segment.name == hint:
                return segment.id
        return None

    def _record_initial_audit(self) -> None:
        for message in self.messages:
            self.audit.record(
                message.time, "message_received",
                f"收到 {message.family} 家留言 {message.id}（范围：{message.scope}）",
                (message.id,), public_ok=message.scope == "公开",
                payload={"原文": message.text},
            )
        for action in self.actions.values():
            self.audit.record(
                "", "action_derived",
                f"动作 {action.id} 状态 {action.state.value}",
                action.evidence,
                public_ok=action.state == ActionState.CONFIRMED,
                payload={"action": to_plain(action)},
            )

    # ============================================================ 查询

    def mentions_of(self, message_id: str) -> list:
        return [mention for clause in self.clauses.get(message_id, [])
                for mention in clause.mentions]

    def open_tasks(self) -> list[VerificationTask]:
        return self.resolver.open_tasks() + self.action_builder.tasks

    def resolve_task(self, task_id: str, resolution: str) -> VerificationTask:
        return self.resolver.resolve_task(task_id, resolution)

    def person_view(self, person_id: str, post: str) -> dict:
        """按岗位返回人物视图：无权限的登记名/读音不出现。"""
        person = self.people[person_id]
        view = {
            "person_id": person.id,
            "家庭": person.family,
            "台上可用名": person.stage_name,
            "后台注记": person.visible_note(post),
        }
        return view

    def host_view(self, segment_id: str) -> dict:
        """司仪视图：当前可用讲稿 + 可见读音 + 未决问题。"""
        line = self.script_builder.current_line(segment_id)
        safe = self.script_builder.render_safe(segment_id)
        problems = list(line.problems) if line else ["该环节尚无口播稿"]
        visible_notes = [
            self.person_view(pid, "司仪")["后台注记"]
            for pid in self.people
            if "司仪" in self.people[pid].pronunciation_visibility
        ]
        return {
            "环节": segment_id,
            "讲稿": line,
            "可朗读稿": safe,
            "待核实": [task.issue for task in self._tasks_for_segment(segment_id)],
            "司仪可见读音": visible_notes,
            "泛称兜底": self.script_builder.generic_fallback(segment_id),
        }

    def concierge_view(self, segment_id: str) -> dict:
        """礼宾视图：当前可执行行动卡（未回执更正不出现为可执行指令）。"""
        cards = []
        for card in self.card_manager.cards_for_segment(segment_id):
            active = card.active_version()
            cards.append({
                "卡": card.id,
                "岗位": card.post,
                "状态": card.status.value,
                "生效版本": active.no if active else None,
                "生效正文": active.text if active else None,
                "暂止": card.lead_locked,
                "待回执更正": [c.id for c in self.card_manager.pending_corrections(card.post)
                             if c.segment_id == segment_id],
            })
        executable = [
            {
                "动作": action.id,
                "对象": action.target_person_id,
                "动词": action.verb.value,
                "位置": action.location,
                "依据": list(action.evidence),
            }
            for action in self.action_builder.actions_for_segment(segment_id)
            if action.state == ActionState.CONFIRMED and not action.block_reason
        ]
        return {
            "环节": segment_id,
            "行动卡": cards,
            "可执行动作": executable,
            "冲突待确认": [
                action.id for action in self.action_builder.actions_for_segment(segment_id)
                if action.state == ActionState.CONFLICT
            ],
        }

    def _tasks_for_segment(self, segment_id: str) -> list[VerificationTask]:
        related = set()
        for message in self.messages:
            for clause in self.clauses.get(message.id, []):
                if self._hint_to_segment(clause.segment_hint, message.family) == segment_id:
                    related.add(message.id)
        return [
            task for task in self.open_tasks()
            if any(source in related for source in task.source_ids)
        ]

    # ============================================================ 操作

    def revise_script(
        self, segment_id: str, new_text: str, source_ids: tuple[str, ...], family_hint: str | None
    ) -> ScriptLine:
        segment = self.segments[segment_id]
        if segment.state in {SegmentState.ACTIVE, SegmentState.LOCKED}:
            raise PermissionError(
                f"环节 {segment_id} 已开始/暂止，口播更正只能由现场负责人处置"
            )
        line = self.script_builder.build_line(segment_id, new_text, source_ids, family_hint)
        self.audit.record(
            "", "script_revised",
            f"环节 {segment_id} 口播更正为 {line.id}",
            source_ids, public_ok=line.consent_ok, payload={"line": to_plain(line)},
        )
        return line

    def issue_correction(self, *args, **kwargs) -> Correction:
        correction = self.card_manager.issue_correction(*args, **kwargs)
        self.audit.record(
            correction.created, "correction_issued",
            f"更正 {correction.id}：{correction.summary}",
            (correction.id,), public_ok=False, payload=to_plain(correction),
        )
        return correction

    def lead_decision(self, correction_id: str, note: str, *, apply: bool) -> Correction:
        if apply:
            correction = self.card_manager.lead_apply(correction_id, note)
        else:
            correction = self.card_manager.hold(correction_id, note)
        self.audit.record(
            "", "lead_decision",
            f"负责人对 {correction_id} 的处置：{note}",
            (correction_id,), public_ok=False, payload={"state": correction.state.value},
        )
        return correction

    def build_thanks_plan(self, segment_id: str, family: str) -> ThanksPlan:
        questions = tuple(
            task.issue for task in self.open_tasks()
            if any(self.messages_by_id(src).family == family for src in task.source_ids
                   if src in {m.id for m in self.messages})
        )
        plan = self.thanks_planner.build(
            segment_id, family, self.messages, self.clauses, questions
        )
        self.thanks_plans[(family, segment_id)] = plan
        return plan

    def messages_by_id(self, message_id: str) -> Message:
        return next(message for message in self.messages if message.id == message_id)

    def public_archive(self) -> dict:
        archive = PublicArchive(self.people)
        return archive.build(
            messages=self.messages,
            grants=self.consent.grants,
            script_lines=self.script_builder.lines,
            cards=self.cards,
        )

    def evidence_for(self, term_or_action: str) -> list[dict]:
        """事后核对：某次称呼/带位依据了哪些原始留言和卡片。"""
        chain = self.audit.evidence_chain(term_or_action)
        result = []
        for entry in chain:
            result.append({"审计": to_plain(entry)})
        # 回溯原始留言
        for message in self.messages:
            if term_or_action in message.text:
                result.append({"原始留言": {"id": message.id, "范围": message.scope,
                                            "发件": message.sender_id, "原文": message.text}})
        for item in self.run_items:
            if term_or_action in item.text:
                result.append({"岗位交接": {"id": item.id, "岗位": item.post,
                                            "时态": item.tense, "原文": item.text}})
        return result
