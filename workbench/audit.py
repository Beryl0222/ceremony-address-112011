"""审计与公开存档。

- 内部审计：保留原始留言、指称解析、同意依据、行动与更正的完整链路，
  事后可核对某次场上称呼或带位“依据的是哪条留言/哪张卡”。
- 公开存档：只保留经过当事人同意的公开表述；内部留言原文、登记名、
  超范围读音注记、未授权词形一律剔除。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import (
    AuditEntry,
    ConsentState,
    CueCard,
    Message,
    Person,
    ScriptLine,
    UsageGrant,
)


@dataclass
class AuditLog:
    entries: list[AuditEntry] = field(default_factory=list)
    _seq: int = 0

    def record(
        self,
        time: str,
        event_type: str,
        summary: str,
        evidence_ids: tuple[str, ...],
        public_ok: bool,
        payload: dict | None = None,
    ) -> AuditEntry:
        self._seq += 1
        entry = AuditEntry(
            id=f"E{self._seq:03d}",
            time=time,
            event_type=event_type,
            summary=summary,
            evidence_ids=evidence_ids,
            public_ok=public_ok,
            payload=payload or {},
        )
        self.entries.append(entry)
        return entry

    def evidence_chain(self, term_or_action: str) -> list[AuditEntry]:
        return [
            entry for entry in self.entries
            if term_or_action in entry.summary
            or term_or_action in str(entry.payload)
            or term_or_action in entry.evidence_ids
        ]


class PublicArchive:
    """从工作台状态生成只含同意表述的公开存档。"""

    def __init__(self, people: dict[str, Person]) -> None:
        self.people = people

    def build(
        self,
        messages: list[Message],
        grants: list[UsageGrant],
        script_lines: dict[str, list[ScriptLine]],
        cards: dict[str, CueCard],
    ) -> dict:
        public_messages = [
            {
                "id": message.id,
                "时间": message.time,
                "家庭": message.family,
                "内容": message.text,
            }
            for message in messages
            if message.scope == "公开"
        ]

        public_grants = [
            {
                "person_id": grant.person_id,
                "表述": grant.term,
                "来源": grant.source_id,
                "环节": grant.segment_id,
            }
            for grant in grants
            if grant.state == ConsentState.GRANTED and grant.scope == "公开"
        ]

        public_script: list[dict] = []
        for segment_id, rows in script_lines.items():
            for line in rows:
                if line.superseded:
                    continue
                public_script.append({
                    "环节": segment_id,
                    "版本": line.version,
                    "口播": line.text if line.consent_ok else None,
                    "可公开": line.consent_ok,
                    "依据": list(line.source_ids),
                })

        public_cards: list[dict] = []
        for key, card in cards.items():
            version = card.active_version()
            if version is None:
                continue
            executable = not card.lead_locked and card.status.value not in {"suspended", "executed"}
            public_cards.append({
                "卡": key,
                "岗位": card.post,
                "生效版本": version.no,
                "正文": self._strip_internal(version.text, grants) if executable else None,
                "当前可执行": executable,
                "状态": card.status.value,
                "依据": list(version.source_refs),
            })

        return {
            "声明": "仅含当事人同意公开的表述；登记名、内部留言、超范围读音注记已剔除",
            "公开留言": public_messages,
            "同意表述": public_grants,
            "口播存档": public_script,
            "行动卡存档": public_cards,
        }

    def _strip_internal(self, text: str, grants: list[UsageGrant]) -> str:
        allowed_terms = {
            grant.term for grant in grants
            if grant.state == ConsentState.GRANTED and grant.scope == "公开"
        }
        allowed_terms |= {"亲友", "各位"}
        # 登记名绝不进公开存档
        for person in self.people.values():
            if person.registered_name not in allowed_terms and person.registered_name in text:
                text = text.replace(person.registered_name, "（内部姓名已隐去）")
        return text
