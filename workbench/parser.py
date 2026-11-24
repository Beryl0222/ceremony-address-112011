"""家庭留言的确定性解析。

只做规则可解释的浅层解析：切句后按词表和固定句式抽取指称与行动，
解析结果都带原文偏移和来源留言 id，任何归并都可回到原件复核。
重名（如两位“阿槐”、两位“林晚”）保留候选，交给 resolver 结合
发言人、家庭与场次消歧，解析层不武断指认。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .models import (
    ActionVerb,
    Clause,
    ClauseKind,
    Message,
    Mention,
    ParsedAction,
    Person,
    RefKind,
)

_SENT_SPLIT = re.compile(r"(?<=[。！？；;])")
_SEGMENT_KEYWORDS = (
    ("致谢", "致谢"),
    ("致礼", "致礼"),
    ("出场", "出场"),
    ("进场", "出场"),
)

# 行动动词模式：否定形式优先匹配，避免把“不请起身/不去台口”误读成邀请
_ACTION_PATTERNS = (
    # 否定/原地类（先匹配）
    (ActionVerb.NO_RISE, re.compile(r"(?P<target>[^，。；,]*?)不起身|不(?:请)?(?P<target2>[^，。；,]*?)起身")),
    (ActionVerb.HOLD, re.compile(r"(?P<target3>[^，。；,]*?)别走(?P<loc>[^，。；,]+)")),
    (ActionVerb.HOLD, re.compile(r"不去(?P<loc2>台口|[东西南北]侧?台口)")),
    # 肯定类
    (ActionVerb.INVITE_STAGE, re.compile(r"(?P<target4>[^，。；,]*?)到?(?P<loc3>[东西南北]侧?台口|台口)(?:接受致礼|候场)?")),
    (ActionVerb.ESCORT, re.compile(r"引导(?P<target5>[^，。；,]+?)从(?P<loc4>[^，。；,]+?)(?:进场|入场)")),
    (ActionVerb.INVITE_RISE, re.compile(r"请(?P<target6>[^，。；,]*?)在座位起身")),
    (ActionVerb.INVITE_RISE, re.compile(r"请(?P<target7>[^，。；,]*?)起身")),
    (ActionVerb.INVITE_RISE, re.compile(r"同意(?P<target8>[^，。；,]*?)在[^，。；,]*?起身")),
    (ActionVerb.INVITE_RISE, re.compile(r"(?P<target9>[^，。；,]*?)在?座位?起身即可|台下起身即可")),
)

_SUBCLAUSE_SPLIT = re.compile(r"[，,；;]")
_SEATING_PATTERN = re.compile(r"坐(?P<loc>[^，。；,]+)|还在(?P<loc2>[^，。；,]+)")
_GENERIC_TERMS = ("亲友", "各位", "大家")
_KIN_TERMS = ("外婆", "姐姐", "舅舅", "妹妹", "哥哥", "弟弟", "叔叔", "阿姨")
_QUALIFIERS = ("对方", "第一排", "东一位", "东一排", "西一位", "西一排", "那位")
_LOCATION_WORDS = ("东", "西", "南", "北", "排", "位", "台口", "东口", "西区", "东区")
_DIRECTIVE_VERBS = ("叫", "称", "念", "称呼")


@dataclass
class Lexicon:
    """从人物表建立的词表：专名/昵称/称谓 -> 候选人物 id。"""

    by_term: dict[str, list[str]]
    people: dict[str, Person]
    # 解析器从留言中学到的别名（晚妹等），初始为空
    learned_aliases: dict[tuple[str, str | None], str] = field(default_factory=dict)

    @classmethod
    def build(cls, people: dict[str, Person]) -> "Lexicon":
        by_term: dict[str, list[str]] = {}
        for person in people.values():
            terms = [person.registered_name]
            if person.stage_name:
                terms.append(person.stage_name)
            for term in terms:
                by_term.setdefault(term, []).append(person.id)
        return cls(by_term=by_term, people=people)


def split_sentences(text: str) -> list[str]:
    parts = [part.strip() for part in _SENT_SPLIT.split(text) if part.strip()]
    return parts or [text.strip()]


def segment_hint_of(text: str) -> str | None:
    for keyword, hint in _SEGMENT_KEYWORDS:
        if keyword in text:
            return hint
    return None


def _find_qualified_kin(sentence: str, base: int) -> tuple[Mention, int] | None:
    """识别带限定的亲属称谓，如“对方外婆”“东一位的外婆”“第一排那位”。"""
    for qualifier in _QUALIFIERS:
        pattern = re.compile(re.escape(qualifier) + r"(?:的)?(?P<kin>" + "|".join(_KIN_TERMS) + r")?")
        match = pattern.search(sentence)
        if not match:
            continue
        kin = match.group("kin")
        text = match.group(0)
        kind = RefKind.KIN_QUALIFIED if kin else RefKind.KIN
        mention = Mention(
            kind=kind,
            text=text,
            start=base + match.start(),
            end=base + match.end(),
            qualifier=qualifier,
        )
        return mention, match.end()
    return None


def extract_mentions(sentence: str, base: int, lexicon: Lexicon, speaker_id: str | None,
                     family_hint: str | None, segment_hint: str | None) -> list[Mention]:
    mentions: list[Mention] = []
    occupied: list[tuple[int, int]] = []

    def take(mention: Mention) -> None:
        if any(not (mention.end <= start or mention.start >= end) for start, end in occupied):
            return
        mentions.append(mention)
        occupied.append((mention.start, mention.end))

    # 1. 带限定的亲属称谓
    qualified = _find_qualified_kin(sentence, base)
    if qualified is not None:
        mention, _ = qualified
        mention.speaker_id = speaker_id
        mention.family_hint = family_hint
        mention.segment_hint = segment_hint
        take(mention)

    # 2. 自称
    for match in re.finditer(r"我们|我", sentence):
        take(Mention(
            kind=RefKind.SELF,
            text=match.group(0),
            start=base + match.start(),
            end=base + match.end(),
            speaker_id=speaker_id,
            family_hint=family_hint,
            segment_hint=segment_hint,
        ))

    # 3. 头衔称谓：林女士 / 林老师 / 顾先生…（剥离“叫/称/念/请”等动词）
    for match in re.finditer(r"[一-鿿]{0,2}(?:女士|先生|老师)", sentence):
        term = match.group(0)
        for verb in _DIRECTIVE_VERBS + ("请",):
            if term.startswith(verb):
                term = term[len(verb):]
        if not term:
            continue
        candidates = tuple(lexicon.by_term.get(term, []))
        start = match.end() - len(term)
        take(Mention(
            kind=RefKind.TITLE,
            text=term,
            start=base + start,
            end=base + match.end(),
            speaker_id=speaker_id,
            family_hint=family_hint,
            segment_hint=segment_hint,
            candidates=candidates,
        ))

    # 4. 词表中的专名/台上可用名，以及解析器已学到的别名
    extra_terms = set()
    for (term, _family), pid in getattr(lexicon, "learned_aliases", {}).items():
        if term and term not in lexicon.by_term:
            extra_terms.add(term)
    ordered_terms = sorted(
        list(lexicon.by_term.keys()) + list(extra_terms), key=lambda item: -len(item)
    )
    for term in ordered_terms:
        candidate_ids = tuple(lexicon.by_term.get(term, []))
        for match in re.finditer(re.escape(term), sentence):
            if not candidate_ids:
                # 已学别名：解析阶段再归并，先记为待消歧名称
                candidate_ids = tuple()
            person = lexicon.people[candidate_ids[0]] if candidate_ids else None
            take(Mention(
                kind=RefKind.NAME,
                text=term,
                start=base + match.start(),
                end=base + match.end(),
                speaker_id=speaker_id,
                family_hint=family_hint,
                segment_hint=segment_hint,
                candidates=candidate_ids,
                qualifier=(
                    "登记名"
                    if person is not None and term == person.registered_name
                    and term != person.stage_name
                    else None
                ),
            ))

    # 5. 裸亲属称谓（座位词“东一排/第一排”不算亲属）
    seat_spans = [
        (base + match.start(), base + match.end())
        for match in _SEATING_PATTERN.finditer(sentence)
    ]
    for kin in _KIN_TERMS:
        for match in re.finditer(re.escape(kin), sentence):
            m_start, m_end = base + match.start(), base + match.end()
            # 已被“带限定称谓”整体覆盖或落在座位描述中的跳过
            if any(start <= m_start < end for start, end in occupied):
                continue
            if any(start <= m_start < end for start, end in seat_spans):
                continue
            take(Mention(
                kind=RefKind.KIN,
                text=kin,
                start=m_start,
                end=m_end,
                speaker_id=speaker_id,
                family_hint=family_hint,
                segment_hint=segment_hint,
            ))

    # 6. 第三人称代词
    for match in re.finditer(r"她们|他们|她|他", sentence):
        take(Mention(
            kind=RefKind.PRONOUN,
            text=match.group(0),
            start=base + match.start(),
            end=base + match.end(),
            speaker_id=speaker_id,
            family_hint=family_hint,
            segment_hint=segment_hint,
        ))

    # 7. 泛称
    for term in _GENERIC_TERMS:
        for match in re.finditer(re.escape(term), sentence):
            take(Mention(
                kind=RefKind.GENERIC,
                text=term,
                start=base + match.start(),
                end=base + match.end(),
                speaker_id=speaker_id,
                family_hint=family_hint,
                segment_hint=segment_hint,
            ))

    mentions.sort(key=lambda item: item.start)
    return mentions


def _is_location_mention(mention: Mention) -> bool:
    if mention.qualifier in {"第一排", "东一位", "东一排", "西一位", "西一排"}:
        return True
    return any(marker in mention.text for marker in _LOCATION_WORDS)


def _match_action(sub: str, sub_offset: int, mentions: list[Mention],
                  hint: str | None) -> ParsedAction | None:
    for verb, pattern in _ACTION_PATTERNS:
        match = pattern.search(sub)
        if not match:
            continue
        negated = verb in {ActionVerb.NO_RISE, ActionVerb.HOLD}
        loc = None
        for group_name in ("loc", "loc2", "loc3", "loc4"):
            try:
                loc = match.group(group_name)
            except IndexError:
                loc = None
            if loc:
                break
        candidates = [m for m in mentions if not _is_location_mention(m)]
        target = _nearest_target(
            sub_offset + match.start(), sub_offset + match.end(), candidates
        )
        if target is None:
            # 主语省略（“不起身/不去台口/起身即可”）：回退到句中显著人物
            target = _fallback_target(candidates)
        if target is None:
            continue
        if loc and not any(marker in loc for marker in _LOCATION_WORDS):
            loc = None
        return ParsedAction(
            verb=verb,
            target=target,
            location=loc,
            segment_hint=hint,
            negated=negated,
        )
    return None


def _fallback_target(mentions: list[Mention]) -> Mention | None:
    people_mentions = [
        mention for mention in mentions
        if mention.kind not in (RefKind.GENERIC, RefKind.SELF)
        and not _is_location_mention(mention)
    ]
    return people_mentions[0] if people_mentions else None


def _extract_actions(sentence: str, base: int, mentions: list[Mention],
                     hint: str | None, message_mentions: list[Mention] | None = None) -> list[ParsedAction]:
    actions: list[ParsedAction] = []
    subclauses = [part for part in _SUBCLAUSE_SPLIT.split(sentence) if part.strip()]
    if len(subclauses) <= 1:
        found = _match_action(sentence, base, mentions, hint)
        if found is None and message_mentions:
            found = _match_action(sentence, base, message_mentions, hint)
        return [found] if found else []

    cursor = 0
    people_mentions = [m for m in mentions if not _is_location_mention(m)]
    fallback_pool = people_mentions or [
        m for m in (message_mentions or []) if not _is_location_mention(m)
    ]
    for sub in subclauses:
        offset = sentence.find(sub, cursor)
        cursor = offset + len(sub)
        local_mentions = [
            mention for mention in mentions
            if base + offset <= mention.start < base + offset + len(sub)
            and not _is_location_mention(mention)
        ]
        # 主语省略时允许回退到整句、乃至整条留言的显著人物
        found = _match_action(sub, base + offset, local_mentions or fallback_pool, hint)
        if found is not None:
            actions.append(found)
    return actions


def _nearest_target(start: int, end: int, mentions: list[Mention]) -> Mention | None:
    """取与动词片段距离最近、且落在句中的指称作为行动目标。"""
    if not mentions:
        return None

    def distance(mention: Mention) -> int:
        if mention.end <= start:
            return start - mention.end
        if mention.start >= end:
            return mention.start - end
        return 0

    ordered = sorted(mentions, key=lambda item: (distance(item), item.start))
    target = ordered[0]
    if target.kind is RefKind.GENERIC:  # 泛称永远不能作为行动目标
        return None
    return target


def classify_clause(sentence: str, actions: list[ParsedAction]) -> ClauseKind:
    if actions:
        return ClauseKind.ACTION_REQUEST
    if _SEATING_PATTERN.search(sentence):
        return ClauseKind.SEATING_NOTE
    if any(term in sentence for term in _GENERIC_TERMS):
        return ClauseKind.GENERIC_STATEMENT
    if any(marker in sentence for marker in _DIRECTIVE_VERBS):
        return ClauseKind.HOST_SCRIPT
    return ClauseKind.FAMILY_REPORT


def parse_message(message: Message, lexicon: Lexicon) -> list[Clause]:
    sentences = split_sentences(message.text)
    # 第一遍：抽取整条留言的指称，供跨句主语省略回退
    all_mentions: list[Mention] = []
    offset = 0
    sentence_frames: list[tuple[str, int, str | None]] = []
    message_hint: str | None = None
    for sentence in sentences:
        start = message.text.find(sentence, offset)
        if start < 0:
            start = offset
        offset = start + len(sentence)
        hint = segment_hint_of(sentence) or message_hint
        if hint:
            message_hint = hint
        sentence_frames.append((sentence, start, hint))
        all_mentions.extend(
            extract_mentions(sentence, start, lexicon, message.sender_id, message.family, hint)
        )

    # 第二遍：逐句分类与动作抽取
    clauses: list[Clause] = []
    for sentence, start, hint in sentence_frames:
        mentions = [m for m in all_mentions if start <= m.start < start + len(sentence)]
        actions = _extract_actions(sentence, start, mentions, hint, all_mentions)
        for parsed in actions:
            if parsed.segment_hint is None:
                parsed.segment_hint = hint
            parsed.target.segment_hint = hint
        kind = classify_clause(sentence, actions)
        clauses.append(Clause(
            kind=kind,
            text=sentence,
            mentions=mentions,
            actions=actions,
            segment_hint=hint,
            source_message_id=message.id,
        ))
    return clauses
