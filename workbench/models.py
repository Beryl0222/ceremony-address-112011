"""工作台领域模型。

语料中的中文键只在加载层出现；本模块使用稳定的英文字段，
但所有正文、称谓、登记名等原始文字都原样保留，供事后核对。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# ---------------------------------------------------------------- 枚举

class RefKind(str, Enum):
    """指称类型：自称、第三人称名称、称谓及其限定等。"""

    SELF = "self"                  # 我 / 我们
    NAME = "name"                  # 阿槐、林晚、顾代 等专名/昵称
    TITLE = "title"                # 林女士、林老师 等头衔称谓
    KIN = "kin"                    # 外婆、姐姐、舅舅 等亲属称谓
    KIN_QUALIFIED = "kin_qualified"  # 带限定的亲属称谓：对方外婆、东一位的外婆
    PRONOUN = "pronoun"            # 她 / 他
    GENERIC = "generic"            # 亲友、各位 等家庭认可的泛称


class ClauseKind(str, Enum):
    """语段性质：区分主持用语、家庭转述与礼宾行动请求。"""

    HOST_SCRIPT = "host_script"        # 主持人要说的话（称呼/口播）
    FAMILY_REPORT = "family_report"    # 家庭情况转述（座位、身份说明）
    ACTION_REQUEST = "action_request"  # 明确的礼宾行动请求
    SEATING_NOTE = "seating_note"      # 仅描述座位，不构成上台邀请
    GENERIC_STATEMENT = "generic_statement"  # 泛称安排，不指派行动
    RULE = "rule"                      # 岗位约定/回退说法（desk notes）


class ConsentState(str, Enum):
    """一条表述能否被公开使用的同意状态。"""

    GRANTED = "granted"      # 当事人（或其授权代表）同意
    PENDING = "pending"      # 仅亲属建议，未经当事人同意
    DECLINED = "declined"    # 当事人明确拒绝
    WITHDRAWN = "withdrawn"  # 曾同意后被本人更新撤回
    INTERNAL = "internal"    # 仅限后台，永远不可公开朗读


class TaskKind(str, Enum):
    VERIFY_REFERENCE = "verify_reference"  # 指代未解决
    VERIFY_CONSENT = "verify_consent"      # 称呼/做法缺当事人同意
    RESOLVE_CONFLICT = "resolve_conflict"  # 行动安排冲突，交工作人员


class TaskStatus(str, Enum):
    OPEN = "open"
    RESOLVED = "resolved"
    CLOSED = "closed"


class ActionVerb(str, Enum):
    INVITE_RISE = "invite_rise"    # 请起身
    INVITE_STAGE = "invite_stage"  # 请上台/到台口
    ESCORT = "escort"              # 引导移动
    SEAT = "seat"                  # 安排座位（描述性）
    HOLD = "hold"                  # 原地不动/不移动
    NO_RISE = "no_rise"            # 不请起身


class ActionState(str, Enum):
    PROPOSED = "proposed"      # 从留言提取，尚未确认
    CONFIRMED = "confirmed"    # 与当事人参与方式一致，可执行
    BLOCKED = "blocked"        # 缺同意/指代未解/违反约定，不可执行
    CONFLICT = "conflict"      # 与另一段安排冲突，待工作人员确认
    SUPERSEDED = "superseded"  # 被本人后续留言更新替代
    EXECUTED = "executed"      # 现场已执行


class SegmentState(str, Enum):
    SCHEDULED = "scheduled"
    ACTIVE = "active"      # 已开始引导，更正只能由现场负责人处置
    LOCKED = "locked"      # 负责人暂止
    DONE = "done"


class CardStatus(str, Enum):
    DRAFT = "draft"
    DISTRIBUTED = "distributed"  # 已分发/已接收
    CORRECTED = "corrected"      # 已有更正，等待/确认回执中
    SUSPENDED = "suspended"      # 负责人暂止
    EXECUTED = "executed"


class CorrectionState(str, Enum):
    PENDING_ACK = "pending_ack"    # 部分岗位尚未回执
    ACKED = "acked"                # 全部受影响岗位已回执
    HELD = "held"                  # 负责人暂止，待现场负责人处置
    APPLIED = "applied"            # 已生效到当前可执行版本
    REJECTED = "rejected"


# ---------------------------------------------------------------- 人物与指称

@dataclass(frozen=True)
class Person:
    id: str
    family: str
    registered_name: str          # 登记名：仅后台核身份
    stage_name: str | None        # 台上可用名
    pronunciation: str | None     # 读音注记
    pronunciation_visibility: frozenset[str]  # 可见岗位，空集表示无人可见
    delegates: frozenset[str] = frozenset()   # 可代其同意的人 id

    def visible_note(self, post: str) -> dict[str, str]:
        """按岗位返回该人可见的后台注记；无权限时只给身份 id。"""
        if post in self.pronunciation_visibility:
            return {
                "person_id": self.id,
                "登记名": self.registered_name,
                "读音": self.pronunciation or "",
            }
        return {"person_id": self.id}


@dataclass
class Mention:
    """留言或台本中出现的一个指称。"""

    kind: RefKind
    text: str                      # 原文词形
    start: int
    end: int
    speaker_id: str | None = None       # 发言人（留言发件人）
    family_hint: str | None = None      # 留言所属家庭/场次限定
    segment_hint: str | None = None
    qualifier: str | None = None        # 称谓限定：对方、东一位、第一排
    person_id: str | None = None        # 解析结果；None 表示未解决
    candidates: tuple[str, ...] = ()    # 重名时的候选
    resolved: bool = False

    def is_public_safe(self) -> bool:
        return self.resolved and self.person_id is not None


@dataclass
class ParsedAction:
    """从语段中抽出的行动诉求（尚未与参与方式对账）。"""

    verb: ActionVerb
    target: Mention
    location: str | None
    segment_hint: str | None
    negated: bool = False


@dataclass
class Clause:
    """一条留言切出的语段。"""

    kind: ClauseKind
    text: str
    mentions: list[Mention] = field(default_factory=list)
    actions: list[ParsedAction] = field(default_factory=list)
    segment_hint: str | None = None
    source_message_id: str | None = None

    @property
    def action(self) -> ParsedAction | None:
        return self.actions[0] if self.actions else None


@dataclass
class Message:
    id: str
    time: str
    family: str
    sender_id: str
    scope: str                 # 公开 / 内部
    text: str                  # 原文，逐字保留
    clauses: list[Clause] = field(default_factory=list)


# ---------------------------------------------------------------- 流程与卡片

@dataclass
class Segment:
    id: str
    family: str
    time: str
    name: str                  # 环节：出场/致谢/致礼
    location: str
    receiving_post: str
    state: SegmentState = SegmentState.SCHEDULED


@dataclass
class DeskNote:
    id: str
    segment_id: str
    text: str


@dataclass
class GenericApproval:
    """家庭认可的泛称；泛称永远不派生行动指令。"""

    segment_id: str
    term: str
    families: frozenset[str]
    source_id: str
    assignable: bool = False


@dataclass
class UsageGrant:
    """一条称呼/表述的同意记录（当事人同意才是 GRANTED）。"""

    person_id: str | None
    term: str
    scope: str                          # 公开 / 内部
    state: ConsentState
    source_id: str
    granted_by: str | None              # 实际确认者
    segment_id: str | None = None
    note: str = ""


@dataclass
class VerificationTask:
    id: str
    kind: TaskKind
    issue: str
    ask_person_id: str | None           # 应向谁核实（当事人本人）
    ask_post: str | None                # 或应找哪个岗位/负责人
    source_ids: tuple[str, ...]
    status: TaskStatus = TaskStatus.OPEN
    resolution: str = ""


@dataclass
class Action:
    id: str
    segment_id: str
    target_person_id: str | None
    verb: ActionVerb
    location: str | None
    source_id: str
    negated: bool = False
    state: ActionState = ActionState.PROPOSED
    block_reason: str = ""
    conflicts_with: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()


@dataclass
class ScriptLine:
    """司仪口播稿（一个环节可有多个版本）。"""

    id: str
    segment_id: str
    version: int
    text: str
    source_ids: tuple[str, ...]
    consent_ok: bool
    problems: tuple[str, ...] = ()
    superseded: bool = False


@dataclass
class CardVersion:
    no: int
    created: str
    text: str
    raw_status: str                     # 语料中的时态原样保留
    action_ids: tuple[str, ...] = ()
    source_refs: tuple[str, ...] = ()
    acks: dict[str, str] = field(default_factory=dict)  # post -> 回执时态


@dataclass
class Correction:
    """一次跨岗更正：只影响列出的称呼与动作，回执按岗记录。"""

    id: str                              # 通常取 run_of_show 更正记录 id
    segment_id: str
    created: str
    summary: str
    affected_posts: tuple[str, ...]
    affected_action_ids: tuple[str, ...] = ()
    affected_terms: tuple[str, ...] = ()
    acks: dict[str, str] = field(default_factory=dict)  # post -> 回执时态
    state: CorrectionState = CorrectionState.PENDING_ACK
    lead_only: bool = False              # 环节已开始后，只能由现场负责人启用
    fallback_text: str | None = None


@dataclass
class CueCard:
    """岗位行动卡（递词卡/带位卡），按版本留痕、按岗查回执。"""

    id: str
    segment_id: str
    post: str
    versions: list[CardVersion] = field(default_factory=list)
    distributed: bool = False
    lead_locked: bool = False
    fallback_text: str | None = None
    fallback_activated: bool = False
    status: CardStatus = CardStatus.DRAFT

    def latest(self) -> CardVersion | None:
        return self.versions[-1] if self.versions else None

    def active_version(self) -> CardVersion | None:
        """当前岗位可执行的版本。

        未被该岗回执的更正不生效；负责人暂止期间继续展示最近一次
        已确认版本，且不得出现互相矛盾的新指令。
        """
        if not self.versions:
            return None
        if self.fallback_activated and self.fallback_text is not None:
            base = self.versions[0]
            return CardVersion(
                no=base.no,
                created=base.created,
                text=self.fallback_text,
                raw_status="回退说法",
                action_ids=base.action_ids,
                source_refs=base.source_refs,
                acks=dict(base.acks),
            )
        for version in reversed(self.versions):
            if _version_is_active(version, self):
                return version
        return self.versions[0]


def _version_is_active(version: CardVersion, card: CueCard) -> bool:
    if version.no == 1:
        return True
    # 更正版本必须被该卡岗位回执，且未被负责人暂止
    if card.lead_locked and version.no > 1:
        return False
    ack = version.acks.get(card.post, "")
    return ack in {"已接收", "已回执", "已执行"}


@dataclass
class RunOfShowItem:
    id: str
    segment_id: str
    post: str
    created: str
    tense: str
    text: str
    linked_card_id: str | None = None


@dataclass
class ThanksPlan:
    """一方家庭在某环节的致谢安排，两方各自决定。"""

    family: str
    segment_id: str
    approved_terms: tuple[str, ...]
    generic_allowed: bool
    decided_by: tuple[str, ...]
    source_ids: tuple[str, ...]
    open_questions: tuple[str, ...] = ()


@dataclass
class AuditEntry:
    id: str
    time: str
    event_type: str
    summary: str
    evidence_ids: tuple[str, ...]
    public_ok: bool                 # 公开存档是否允许出现该表述
    payload: dict[str, Any] = field(default_factory=dict)


def to_plain(obj: Any) -> Any:
    """把 dataclass/Enum 递归转成可 JSON 序列化的普通结构。"""
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [to_plain(item) for item in obj]
    if isinstance(obj, dict):
        return {key: to_plain(value) for key, value in obj.items()}
    if hasattr(obj, "__dataclass_fields__"):
        return {key: to_plain(getattr(obj, key)) for key in obj.__dataclass_fields__}
    return str(obj)
