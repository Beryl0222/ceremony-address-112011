"""指代解析：结合发言人、家庭/场次与语段上下文消歧。

解析原则（与业务约定一致）：
- 自称只归发言人本人；第三人称才走别名/亲属/座位推理。
- 重名昵称（阿槐、林晚）必须有家庭或场次限定才能归并，否则保留候选
  并生成核实任务，绝不替工作人员猜。
- 亲属称谓先在留言家庭内解析；“对方”限定指向另一家庭；座位限定
  （第一排/东一排）对照从留言学到的座位图。
- 代词按句内与跨留言显著性回指，回指失败即生成核实任务。
- 泛称（亲友）不指向任何个人，也不能成为行动目标。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .models import (
    Clause,
    ClauseKind,
    Mention,
    Message,
    Person,
    RefKind,
    Segment,
    TaskKind,
    TaskStatus,
    VerificationTask,
)
from .parser import Lexicon, extract_mentions, segment_hint_of, split_sentences

# 亲属称谓 -> 台上名/登记名中可能出现的字
_KIN_MARKERS = {
    "姐姐": ("姐",),
    "外婆": ("婆", "外婆"),
    "舅舅": ("舅",),
    "妹妹": ("妹",),
    "哥哥": ("哥",),
    "弟弟": ("弟",),
    "叔叔": ("叔",),
    "阿姨": ("姨",),
}
_FAMILIES_JOINT = ("甲", "乙")

_APPOSITIVE_KIN = re.compile(r"(?P<term>[^，。；]+?)是我(?P<kin>姐姐|外婆|舅舅|妹妹|哥哥|弟弟)")
_SELF_ALIAS = re.compile(r"叫我(?P<term>[^，。；]+?)(?:就好|$)|我也叫(?P<term2>[^，。；]+)")
# 本人在某环节启用的新称谓：致谢请称我们家晚妹 / 请称我家晚妹
_OWN_FAMILY_TERM = re.compile(r"称(?:我们|我)家(?P<term>[^，。；]+)")
_QUALIFIED_DEF = re.compile(r"(?P<qual>第一排|东一位|东一排|西一位|西一排)(?:那位)?是(?P<desc>.+)$")
_SELF_IS = re.compile(r"^(?P<kin>外婆|姐姐|舅舅)是我")
# “另一位林晚与我无关”：该同名者在本条语境指发言人本人，声明与另一人区分
_OTHER_SAME_NAME = re.compile(r"另一位(?P<term>[^与，。；]+)与我无关")


@dataclass
class Seating:
    """座位图：位置词 -> 人物 id，来源留言留痕。"""

    location_to_person: dict[str, str] = field(default_factory=dict)
    person_to_location: dict[str, str] = field(default_factory=dict)
    evidence: dict[str, str] = field(default_factory=dict)


class Resolver:
    def __init__(
        self,
        people: dict[str, Person],
        lexicon: Lexicon,
        segments: dict[str, Segment],
    ) -> None:
        self.people = people
        self.lexicon = lexicon
        self.segments = segments
        # (词形, 家庭或 None) -> person_id；None 表示跨家庭通用的自称别名
        self.aliases: dict[tuple[str, str | None], str] = {}
        # (家庭, 亲属词, 限定或 None) -> person_id
        self.kin_map: dict[tuple[str, str, str | None], str] = {}
        self.seating = Seating()
        self.tasks: list[VerificationTask] = []
        self._task_seq = 0
        self._salience: list[str] = []  # 最近解析到的非本人人物 id
        # (词形, 留言id) -> 经“与我无关”等句式锁定的人物
        self._disambiguated: dict[tuple[str, str], str] = {}

    # ------------------------------------------------------------ 任务

    def _add_task(
        self,
        kind: TaskKind,
        issue: str,
        source_ids: tuple[str, ...],
        ask_person_id: str | None = None,
        ask_post: str | None = None,
    ) -> VerificationTask:
        self._task_seq += 1
        task = VerificationTask(
            id=f"T{self._task_seq:02d}",
            kind=kind,
            issue=issue,
            ask_person_id=ask_person_id,
            ask_post=ask_post,
            source_ids=source_ids,
        )
        self.tasks.append(task)
        return task

    def open_tasks(self) -> list[VerificationTask]:
        return [task for task in self.tasks if task.status == TaskStatus.OPEN]

    def resolve_task(self, task_id: str, resolution: str) -> VerificationTask:
        for task in self.tasks:
            if task.id == task_id:
                task.status = TaskStatus.RESOLVED
                task.resolution = resolution
                return task
        raise KeyError(f"核实任务不存在：{task_id}")

    # ------------------------------------------------------------ 学习

    def _learn_alias(self, term: str, family: str | None, person_id: str) -> None:
        if family is not None:
            self.aliases[(term, family)] = person_id
        self.aliases.setdefault((term, None), person_id)
        self.lexicon.learned_aliases[(term, family)] = person_id

    def learn(self, messages: list[Message], clauses_by_message: dict[str, list[Clause]]) -> None:
        """第一遍扫描：从同位语/自称句式学习别名、亲属与座位。"""
        for message in messages:
            family = message.family
            speaker = self.people.get(message.sender_id)
            text = message.text

            for match in _SELF_ALIAS.finditer(text):
                term = match.group("term") or match.group("term2")
                if term and speaker is not None:
                    self._learn_alias(term, family, speaker.id)

            for match in _OWN_FAMILY_TERM.finditer(text):
                term = match.group("term")
                if term and speaker is not None:
                    self._learn_alias(term, family, speaker.id)

            for match in _APPOSITIVE_KIN.finditer(text):
                term, kin = match.group("term"), match.group("kin")
                target = self._kin_candidate(family, kin)
                if target is not None:
                    self.kin_map[(family, kin, None)] = target
                    self.aliases[(term, family)] = target
                else:
                    self._add_task(
                        TaskKind.VERIFY_REFERENCE,
                        f"“{term}”被称为发言人的{kin}，但{family}家庭中找不到对应人物",
                        (message.id,),
                        ask_post="现场负责人",
                    )

            match_self = _SELF_IS.search(text)
            if match_self and speaker is not None:
                kin = match_self.group("kin")
                self.kin_map[(family, kin, None)] = speaker.id
                self.aliases[(kin, family)] = speaker.id

            match_other = _OTHER_SAME_NAME.search(text)
            if match_other and speaker is not None:
                # 发言人声明该名称指自己（与另一位同名者区分）
                term = match_other.group("term")
                self._disambiguated[(term, message.id)] = speaker.id

            match_q = _QUALIFIED_DEF.search(text)
            if match_q:
                qualifier, desc = match_q.group("qual"), match_q.group("desc")
                person_id = self._describe_to_person(desc, family, message.id)
                if person_id is not None:
                    self.seating.location_to_person[qualifier] = person_id
                    self.seating.person_to_location[person_id] = qualifier
                    self.seating.evidence[qualifier] = message.id

            # 直接的座位陈述：坐东一排 / 还在东一排
            seat = re.search(r"坐(?P<loc>[^，。；]+)|还在(?P<loc2>[^，。；,]+?)[，,]", text)
            if seat and speaker is not None:
                loc = seat.group("loc") or seat.group("loc2")
                if loc and loc not in self.seating.location_to_person:
                    self.seating.location_to_person[loc] = speaker.id
                    self.seating.person_to_location[speaker.id] = loc
                    self.seating.evidence[loc] = message.id

        self._seed_kin_from_stage_names()

    def _seed_kin_from_stage_names(self) -> None:
        """台上可用名本身就是亲属词（外婆/舅舅）时，直接建立家庭内称谓。"""
        for person in self.people.values():
            stage = person.stage_name or ""
            for kin, markers in _KIN_MARKERS.items():
                if stage == kin or any(marker in stage for marker in markers):
                    self.kin_map.setdefault((person.family, kin, None), person.id)
                    self.aliases.setdefault((stage, person.family), person.id)

    def _kin_candidate(self, family: str, kin: str) -> str | None:
        markers = _KIN_MARKERS.get(kin, ())
        matches = [
            person.id
            for person in self.people.values()
            if person.family == family
            and (
                person.stage_name == kin
                or any(marker in (person.stage_name or "") for marker in markers)
            )
        ]
        return matches[0] if len(matches) == 1 else None

    def _describe_to_person(self, desc: str, family: str, source_id: str) -> str | None:
        other_family = self._other_family(family)
        if "对方外婆" in desc and other_family:
            person_id = self.kin_map.get((other_family, "外婆", None))
            if person_id:
                self.kin_map[(family, "外婆", "对方")] = person_id
                return person_id
        for kin in _KIN_MARKERS:
            if kin in desc:
                target_family = other_family if "对方" in desc else family
                person_id = self.kin_map.get((target_family or family, kin, None))
                if person_id:
                    return person_id
        return None

    @staticmethod
    def _other_family(family: str) -> str | None:
        if family in _FAMILIES_JOINT:
            return "乙" if family == "甲" else "甲"
        return None

    # ------------------------------------------------------------ 解析

    def resolve_clauses(self, message: Message, clauses: list[Clause]) -> None:
        for clause in clauses:
            self._resolve_clause(message, clause)

    def _resolve_clause(self, message: Message, clause: Clause) -> None:
        for mention in clause.mentions:
            self._resolve_mention(mention, message, clause)
        # 代词句内回指/预指：同句中存在已解析的第三人称时对齐
        third_person = [
            mention for mention in clause.mentions
            if mention.kind not in (RefKind.SELF, RefKind.PRONOUN, RefKind.GENERIC)
            and mention.resolved
        ]
        for mention in clause.mentions:
            if mention.kind is RefKind.PRONOUN and not mention.resolved and third_person:
                mention.person_id = third_person[0].person_id
                mention.resolved = True

    def _resolve_mention(self, mention: Mention, message: Message, clause: Clause) -> None:
        mention.speaker_id = message.sender_id
        mention.family_hint = mention.family_hint or message.family

        if mention.kind is RefKind.SELF:
            mention.person_id = message.sender_id
            mention.resolved = True
            return

        if mention.kind is RefKind.GENERIC:
            # 泛称不指派个人；resolved 保持 False 但不产生任务
            return

        if mention.kind is RefKind.PRONOUN:
            target = self._salience[-1] if self._salience else None
            if target is not None:
                mention.person_id = target
                mention.resolved = True
            else:
                self._add_task(
                    TaskKind.VERIFY_REFERENCE,
                    f"代词“{mention.text}”缺少可回指的上文人物",
                    (message.id,),
                    ask_post="现场负责人",
                )
            return

        if mention.kind in (RefKind.NAME, RefKind.TITLE):
            self._resolve_name(mention, message)
        elif mention.kind in (RefKind.KIN, RefKind.KIN_QUALIFIED):
            self._resolve_kin(mention, message, clause)

        if mention.resolved and mention.person_id:
            self._salience.append(mention.person_id)

    def _resolve_name(self, mention: Mention, message: Message) -> None:
        # “另一位X与我无关”句式在本留言内锁定为发言人
        locked = self._disambiguated.get((mention.text, message.id))
        if locked is not None:
            # 保留全部重名候选留痕，但归并结果锁定为发言人
            mention.person_id, mention.resolved = locked, True
            return

        candidates = list(mention.candidates)
        if not candidates:
            learned = self.aliases.get((mention.text, message.family))
            if learned is None:
                learned = self.aliases.get((mention.text, None))
            if learned is not None:
                mention.person_id, mention.resolved = learned, True
                return
            self._add_task(
                TaskKind.VERIFY_REFERENCE,
                f"称谓“{mention.text}”在登记名单与已学别名中均无对应人物",
                (message.id,),
                ask_person_id=message.sender_id,
            )
            return

        if len(candidates) == 1:
            mention.person_id, mention.resolved = candidates[0], True
            return

        # 重名：先用留言家庭过滤
        family = message.family
        narrowed = [pid for pid in candidates if self.people[pid].family == family]
        if len(narrowed) == 1:
            mention.person_id, mention.resolved = narrowed[0], True
            mention.candidates = tuple(candidates)
            return

        # 再用场次家庭过滤
        if mention.segment_hint:
            segment_families = self._segment_families(mention.segment_hint)
            narrowed2 = [pid for pid in candidates if self.people[pid].family in segment_families]
            if len(narrowed2) == 1:
                mention.person_id, mention.resolved = narrowed2[0], True
                return

        mention.candidates = tuple(candidates)
        self._add_task(
            TaskKind.VERIFY_REFERENCE,
            (
                f"“{mention.text}”重名，候选 {', '.join(self._label(pid) for pid in candidates)}；"
                f"留言家庭“{family}”与场次均不足以唯一指认"
            ),
            (message.id,),
            ask_post="现场负责人",
        )

    def _resolve_kin(self, mention: Mention, message: Message, clause: Clause) -> None:
        family = message.family
        kin = mention.text if mention.text in _KIN_MARKERS else next(
            (k for k in _KIN_MARKERS if k in mention.text), mention.text
        )

        # 座位限定优先
        if mention.qualifier in self.seating.location_to_person:
            mention.person_id = self.seating.location_to_person[mention.qualifier]
            mention.resolved = True
            return

        # “对方”限定 -> 另一家庭
        if mention.qualifier == "对方" or "对方" in mention.text:
            other = self._other_family(family)
            person_id = self.kin_map.get((other or "", kin, None)) if other else None
            if person_id:
                mention.person_id, mention.resolved = person_id, True
                return

        # 本家庭
        person_id = self.kin_map.get((family, kin, mention.qualifier))
        if person_id is None:
            person_id = self.kin_map.get((family, kin, None))
        if person_id is not None:
            mention.person_id, mention.resolved = person_id, True
            return

        # 跨家庭语境：本家庭没有该亲属，尝试对方家庭
        other = self._other_family(family)
        if other:
            person_id = self.kin_map.get((other, kin, None))
            if person_id is not None:
                mention.person_id, mention.resolved = person_id, True
                return

        self._add_task(
            TaskKind.VERIFY_REFERENCE,
            f"亲属称谓“{mention.text}”无法在家庭“{family}”及其对方家庭中定位",
            (message.id,),
            ask_person_id=message.sender_id,
        )

    def _segment_families(self, hint: str) -> set[str]:
        families: set[str] = set()
        for segment in self.segments.values():
            if segment.name == hint:
                for char in segment.family:
                    if char in {"甲", "乙", "丙"}:
                        families.add(char)
        return families

    def _label(self, person_id: str) -> str:
        person = self.people[person_id]
        stage = f"/{person.stage_name}" if person.stage_name else ""
        return f"{person_id}({person.family}家 {person.registered_name}{stage})"

    # ------------------------------------------------------ 台本/卡片复用

    def resolve_free_text(
        self,
        text: str,
        family_hint: str | None,
        segment_hint: str | None,
        source_id: str,
        speaker_id: str | None = None,
    ) -> list[Mention]:
        """解析台本或卡片正文中的指称（合成留言上下文）。"""
        synthetic = Message(
            id=source_id,
            time="",
            family=family_hint or "",
            sender_id=speaker_id or "",
            scope="内部",
            text=text,
        )
        mentions: list[Mention] = []
        for sentence in split_sentences(text):
            base = text.find(sentence)
            mentions.extend(
                extract_mentions(
                    sentence, base, self.lexicon, speaker_id, family_hint, segment_hint
                )
            )
        for mention in mentions:
            if mention.kind is RefKind.SELF:
                if speaker_id:
                    mention.person_id = speaker_id
                    mention.resolved = True
                else:
                    # 无发言人上下文的自称不阻塞台本，跳过
                    mention.resolved = False
                continue
            fake_clause = Clause(kind=ClauseKind.FAMILY_REPORT, text=text, mentions=[mention])
            self._resolve_mention(mention, synthetic, fake_clause)
        return mentions
