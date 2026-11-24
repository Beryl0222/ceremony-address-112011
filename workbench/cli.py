"""司仪/礼宾协同工作台命令行入口。

用法（在仓库根目录，用 -I 隔离环境运行）：
    python3 -I -m workbench.cli overview
    python3 -I -m workbench.cli mentions M1
    python3 -I -m workbench.cli tasks
    python3 -I -m workbench.cli host S2
    python3 -I -m workbench.cli concierge S2
    python3 -I -m workbench.cli thanks S2
    python3 -I -m workbench.cli corrections
    python3 -I -m workbench.cli evidence 外婆
    python3 -I -m workbench.cli archive
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .models import to_plain
from .workbench import Workbench

DEFAULT_DATA = Path(__file__).resolve().parents[1] / "data"


def _print(obj: object) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def _wb(args: argparse.Namespace) -> Workbench:
    return Workbench(args.data)


def cmd_overview(args: argparse.Namespace) -> None:
    wb = _wb(args)
    _print({
        "人物": [
            {
                "id": person.id,
                "家庭": person.family,
                "台上可用名": person.stage_name,
                "读音可见岗位": sorted(person.pronunciation_visibility),
            }
            for person in wb.people.values()
        ],
        "环节": [to_plain(segment) for segment in wb.segments.values()],
        "留言数": len(wb.messages),
        "动作数": len(wb.actions),
        "待核实数": len(wb.open_tasks()),
    })


def cmd_mentions(args: argparse.Namespace) -> None:
    wb = _wb(args)
    rows = []
    for clause in wb.clauses.get(args.message_id, []):
        rows.append({
            "语段": clause.text,
            "性质": clause.kind.value,
            "环节提示": clause.segment_hint,
            "指称": [
                {
                    "词形": mention.text,
                    "类型": mention.kind.value,
                    "限定": mention.qualifier,
                    "解析到": mention.person_id,
                    "已解决": mention.resolved,
                    "候选": list(mention.candidates),
                }
                for mention in clause.mentions
            ],
            "动作": [
                {
                    "动词": action.verb.value,
                    "否定": action.negated,
                    "目标": action.target.person_id,
                    "位置": action.location,
                }
                for action in clause.actions
            ],
        })
    _print(rows)


def cmd_tasks(args: argparse.Namespace) -> None:
    wb = _wb(args)
    _print([
        {
            "id": task.id,
            "类型": task.kind.value,
            "问题": task.issue,
            "向谁核实": task.ask_person_id or task.ask_post,
            "来源": list(task.source_ids),
            "状态": task.status.value,
        }
        for task in wb.open_tasks()
    ])


def cmd_host(args: argparse.Namespace) -> None:
    wb = _wb(args)
    view = wb.host_view(args.segment)
    line = view["讲稿"]
    _print({
        "环节": view["环节"],
        "当前讲稿": to_plain(line) if line else None,
        "可直接朗读": view["可朗读稿"],
        "泛称兜底": view["泛称兜底"],
        "司仪可见读音": view["司仪可见读音"],
        "待核实": view["待核实"],
    })


def cmd_concierge(args: argparse.Namespace) -> None:
    wb = _wb(args)
    _print(wb.concierge_view(args.segment))


def cmd_thanks(args: argparse.Namespace) -> None:
    wb = _wb(args)
    result = {}
    for family in ("甲", "乙", "丙"):
        try:
            plan = wb.build_thanks_plan(args.segment, family)
        except Exception:  # 该家庭在此环节无方案
            continue
        result[family] = to_plain(plan)
    _print(result)


def cmd_corrections(args: argparse.Namespace) -> None:
    wb = _wb(args)
    cm = wb.card_manager
    _print({
        "更正": [to_plain(c) for c in cm.corrections],
        "按岗回执": {
            segment_id: cm.ack_status_by_post(segment_id)
            for segment_id in wb.segments
        },
    })


def cmd_evidence(args: argparse.Namespace) -> None:
    wb = _wb(args)
    _print(wb.evidence_for(args.term))


def cmd_archive(args: argparse.Namespace) -> None:
    wb = _wb(args)
    _print(wb.public_archive())


def cmd_grants(args: argparse.Namespace) -> None:
    wb = _wb(args)
    _print([to_plain(grant) for grant in wb.consent.grants])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="司仪/礼宾协同工作台")
    parser.add_argument("--data", default=str(DEFAULT_DATA), help="资料目录")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("overview", help="总览").set_defaults(func=cmd_overview)

    p_mentions = sub.add_parser("mentions", help="查看一条留言的指称解析")
    p_mentions.add_argument("message_id")
    p_mentions.set_defaults(func=cmd_mentions)

    sub.add_parser("tasks", help="列出待核实的指代/同意/冲突").set_defaults(func=cmd_tasks)
    sub.add_parser("grants", help="列出称呼与参与方式同意记录").set_defaults(func=cmd_grants)

    p_host = sub.add_parser("host", help="司仪视图：当前可用讲稿")
    p_host.add_argument("segment")
    p_host.set_defaults(func=cmd_host)

    p_con = sub.add_parser("concierge", help="礼宾视图：当前可执行行动卡")
    p_con.add_argument("segment")
    p_con.set_defaults(func=cmd_concierge)

    p_thanks = sub.add_parser("thanks", help="两方各自的致谢方案")
    p_thanks.add_argument("segment")
    p_thanks.set_defaults(func=cmd_thanks)

    sub.add_parser("corrections", help="跨岗更正与按岗回执").set_defaults(func=cmd_corrections)

    p_ev = sub.add_parser("evidence", help="事后核对某称呼/带位的依据")
    p_ev.add_argument("term")
    p_ev.set_defaults(func=cmd_evidence)

    sub.add_parser("archive", help="公开存档（仅同意表述）").set_defaults(func=cmd_archive)
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
