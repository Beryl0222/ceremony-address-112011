"""语料加载层：读取 data/*.json，中文键映射为领域模型。

原始 JSON 记录同时保留在 Workbench.raw 中，任何场上决定事后都能
回到原件逐字核对。
"""
from __future__ import annotations

import json
from pathlib import Path

from .models import (
    CardStatus,
    CardVersion,
    CueCard,
    DeskNote,
    Message,
    Person,
    RunOfShowItem,
    Segment,
)

# run_of_show 时态到卡片状态的归类（原文时态另存于 raw_status）
_CARD_TENSES = {"草稿", "已分发", "已接收", "开始引导", "暂止", "已执行"}
_EXECUTED_TENSES = {"已执行"}
_ACK_TENSES = {"已接收", "已执行", "开始引导"}
_LOCK_TENSES = {"暂止"}


def read_json(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as handle:
        rows = json.load(handle)
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError(f"{path.name} 应为记录对象的数组")
    return rows


def load_people(rows: list[dict]) -> dict[str, Person]:
    people: dict[str, Person] = {}
    for row in rows:
        visibility = frozenset(
            part.strip() for part in str(row.get("读音范围", "")).split("/") if part.strip()
        )
        people[row["id"]] = Person(
            id=row["id"],
            family=row["家庭"],
            registered_name=row["登记名"],
            stage_name=row.get("台上可用名") or None,
            pronunciation=row.get("读音") or None,
            pronunciation_visibility=visibility,
        )
    return people


def load_messages(rows: list[dict]) -> list[Message]:
    return [
        Message(
            id=row["id"],
            time=row["时间"],
            family=row["家庭"],
            sender_id=row["发件"],
            scope=row["范围"],
            text=row["内容"],
        )
        for row in rows
    ]


def load_segments(rows: list[dict]) -> dict[str, Segment]:
    return {
        row["id"]: Segment(
            id=row["id"],
            family=row["家庭"],
            time=row["时间"],
            name=row["环节"],
            location=row["地点"],
            receiving_post=row["接收岗"],
        )
        for row in rows
    }


def load_desk_notes(rows: list[dict]) -> list[DeskNote]:
    return [DeskNote(id=row["id"], segment_id=row["环节"], text=row["文字"]) for row in rows]


def load_run_of_show(rows: list[dict]) -> tuple[list[RunOfShowItem], dict[str, CueCard]]:
    """把岗位交接记录整理成卡片。

    同一 (环节, 岗位) 的话术记录归为一张卡的版本；更正、暂止、回执等
    调度记录先原样保留在 RunOfShowItem 中，由工作台层做跨岗对账。
    """
    items: list[RunOfShowItem] = []
    cards: dict[tuple[str, str], CueCard] = {}

    for row in rows:
        item = RunOfShowItem(
            id=row["id"],
            segment_id=row["环节"],
            post=row["岗位"],
            created=row["创建"],
            tense=row["时态"],
            text=row["正文"],
        )
        items.append(item)

        key = (row["环节"], row["岗位"])
        card = cards.setdefault(
            key,
            CueCard(
                id=row["id"],
                segment_id=row["环节"],
                post=row["岗位"],
                fallback_text=None,
            ),
        )
        tense = row["时态"]

        if tense in _CARD_TENSES:
            version = CardVersion(
                no=len(card.versions) + 1,
                created=row["创建"],
                text=row["正文"],
                raw_status=tense,
                source_refs=(row["id"],),
            )
            card.versions.append(version)
            if tense != "草稿":
                card.distributed = True
                version.acks[row["岗位"]] = "已执行" if tense in _EXECUTED_TENSES else tense
            if tense in _LOCK_TENSES:
                card.lead_locked = True

        if any(v.raw_status == "已执行" for v in card.versions):
            card.status = CardStatus.EXECUTED
        elif card.lead_locked:
            card.status = CardStatus.SUSPENDED
        elif card.distributed:
            card.status = CardStatus.DISTRIBUTED
        else:
            card.status = CardStatus.DRAFT

    by_id = {item.id: item for item in items}
    for card in cards.values():
        for version in card.versions:
            for ref in version.source_refs:
                if ref in by_id:
                    by_id[ref].linked_card_id = card.id
    return items, {f"{card.segment_id}:{card.post}": card for card in cards.values()}


def load_corpus(directory: Path) -> dict[str, list[dict]]:
    names = ["people", "requests", "programme", "run_of_show", "desk_notes"]
    return {name: read_json(directory / f"{name}.json") for name in names}
