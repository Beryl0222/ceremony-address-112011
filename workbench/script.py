"""司仪口播稿构建。

规则：
- 稿中每个专名/称谓都必须有当事人 GRANTED 的公开授权，登记名与
  超范围读音注记一律不得出现在公开稿中。
- 留言是家庭语言（含“我/我们/登记名”），先转写成口播句：自称替换为
  发言人的台上名、登记名替换为台上可用名，再做授权校验。
- 未解决或未授权的称呼不允许“猜一个名字”：改用该环节家庭已认可的
  泛称（如“亲友”），并在 problems 中留痕；泛称不产生任何行动。
- 每个环节保留版本，更正生成新版本，旧版标记 superseded 但不删除。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .consent import ConsentManager
from .models import (
    GenericApproval,
    Mention,
    RefKind,
    ScriptLine,
)
from .resolver import Resolver


@dataclass
class ScriptBuilder:
    resolver: Resolver
    consent: ConsentManager
    generics: dict[str, list[GenericApproval]] = field(default_factory=dict)
    lines: dict[str, list[ScriptLine]] = field(default_factory=dict)
    _seq: int = 0

    def add_generic(self, approval: GenericApproval) -> None:
        self.generics.setdefault(approval.segment_id, []).append(approval)

    def _new_id(self) -> str:
        self._seq += 1
        return f"L{self._seq:02d}"

    # ---------------------------------------------------------- 转写

    def _rewrite_for_host(self, text: str, mentions: list[Mention]) -> str:
        """把家庭留言转写为口播句。

        只做两类安全替换（按指称偏移，不做全局字符删除）：
        - 自称（我/我们）-> 发言人台上可用名；
        - 登记名词形 -> 该人台上可用名。
        其余元话语（叫/称/同意念）由句模板在调用处处理。
        """
        rewritten = text
        for mention in sorted(mentions, key=lambda item: item.start, reverse=True):
            replacement: str | None = None
            if mention.kind == RefKind.SELF and mention.person_id:
                person = self.resolver.people.get(mention.person_id)
                replacement = person.stage_name if person and person.stage_name else None
            elif mention.qualifier == "登记名" and mention.person_id:
                person = self.resolver.people.get(mention.person_id)
                replacement = person.stage_name if person and person.stage_name else None
            if replacement:
                rewritten = rewritten[:mention.start] + replacement + rewritten[mention.end:]
        return rewritten.strip("，。、 ")

    # 留言指令 -> 口播句模板（key 为去标点后的特征子串）
    _TEMPLATES = (
        ("致礼时称", "致礼环节请{term}"),
        ("致谢请称", "致谢环节请{term}"),
        ("出场仍叫", "出场仍请{term}"),
        ("仍称", "仍称{term}"),
        ("同意念", "请{term}"),
        ("叫", "请{term}"),
        ("称", "请{term}"),
    )

    def _to_host_sentence(self, clause_text: str, spoken: list[Mention],
                          speaker_id: str | None) -> str:
        # 先替换自称/登记名
        rewritten = self._rewrite_for_host(
            clause_text,
            [m for m in spoken if m.kind in (RefKind.SELF,) or m.qualifier == "登记名"],
        )
        # 取句中真正要朗读的称呼（专名/头衔/亲属/泛称），按模板组装
        terms = [
            mention.text for mention in spoken
            if mention.kind in (RefKind.NAME, RefKind.TITLE, RefKind.KIN,
                                RefKind.KIN_QUALIFIED, RefKind.GENERIC)
        ]
        term = terms[-1] if terms else rewritten
        for marker, template in self._TEMPLATES:
            if marker in clause_text:
                return template.format(term=term)
        return rewritten

    def build_line(
        self,
        segment_id: str,
        host_text: str,
        source_ids: tuple[str, ...],
        family_hint: str | None,
    ) -> ScriptLine:
        # 转写后的口播句直接做授权校验
        host_mentions = self.resolver.resolve_free_text(
            host_text, family_hint, None, source_ids[0] + "#host"
        )
        problems: list[str] = []
        consent_ok = True

        for mention in host_mentions:
            if mention.kind == RefKind.SELF:
                continue  # 转写后理论上不应残留，残留也不阻塞
            if mention.kind == RefKind.GENERIC:
                approved = any(
                    approval.term == mention.text
                    for approval in self.generics.get(segment_id, [])
                )
                if not approved:
                    problems.append(f"泛称“{mention.text}”未经家庭认可，不能写入口播")
                    consent_ok = False
                continue
            if not mention.resolved or mention.person_id is None:
                problems.append(f"指称“{mention.text}”未解决，须用泛称替代并向工作人员核实")
                consent_ok = False
                continue
            term_state = self.consent.term_state(mention.person_id, mention.text)
            if term_state is None:
                problems.append(f"“{mention.text}”没有公开使用授权（person={mention.person_id}）")
                consent_ok = False
            elif term_state.value != "granted":
                problems.append(
                    f"“{mention.text}”同意状态为 {term_state.value}（person={mention.person_id}）"
                )
                consent_ok = False
            elif not self.consent.can_speak_publicly(mention.person_id, mention.text):
                problems.append(f"“{mention.text}”仅内部授权，不能公开朗读")
                consent_ok = False

        line = ScriptLine(
            id=self._new_id(),
            segment_id=segment_id,
            version=len(self.lines.get(segment_id, [])) + 1,
            text=host_text,
            source_ids=source_ids,
            consent_ok=consent_ok,
            problems=tuple(problems),
        )
        previous = self.lines.get(segment_id, [])
        for old in previous:
            old.superseded = True
        self.lines.setdefault(segment_id, []).append(line)
        return line

    def compose_host_sentence(self, clause_text: str, spoken: list[Mention],
                              speaker_id: str | None) -> str:
        """对外暴露的留言->口播句转写。"""
        return self._to_host_sentence(clause_text, spoken, speaker_id)

    def generic_fallback(self, segment_id: str) -> str | None:
        approvals = self.generics.get(segment_id, [])
        if not approvals:
            return None
        return approvals[0].term

    def render_safe(self, segment_id: str, family_hint: str | None = None) -> str | None:
        """生成可直接朗读的安全稿：未授权称呼替换为认可泛称。"""
        rows = self.lines.get(segment_id, [])
        if not rows:
            return None
        line = next((row for row in reversed(rows) if not row.superseded), rows[-1])
        if line.consent_ok:
            return line.text
        fallback = self.generic_fallback(segment_id)
        if fallback is None:
            return None
        mentions = self.resolver.resolve_free_text(
            line.text, family_hint, None, line.source_ids[0] + "#render"
        )
        rendered = line.text
        for mention in sorted(mentions, key=lambda item: item.start, reverse=True):
            if mention.kind == RefKind.GENERIC:
                continue
            if not mention.resolved or mention.person_id is None or not self.consent.can_speak_publicly(
                mention.person_id, mention.text
            ):
                rendered = rendered[:mention.start] + fallback + rendered[mention.end:]
        return rendered

    def current_line(self, segment_id: str) -> ScriptLine | None:
        rows = self.lines.get(segment_id, [])
        if not rows:
            return None
        return next((row for row in reversed(rows) if not row.superseded), rows[-1])
