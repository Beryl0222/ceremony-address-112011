#!/usr/bin/env python3
"""端到端演示：从原始留言到司仪稿、礼宾行动卡与公开存档。

用隔离模式运行：
    python3 -I demo/run_demo.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from workbench.workbench import Workbench  # noqa: E402


def show(title: str, obj) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def main() -> None:
    wb = Workbench(ROOT / "data")

    # 1. 重名昵称按家庭消歧：两位“阿槐”
    show("1) 重名消歧：M1 的阿槐=甲家 P1，M2 的阿槐=乙家 P3", [
        {"留言": mid, "词形": "阿槐", "解析到": next(
            m.person_id for m in wb.mentions_of(mid) if m.text == "阿槐"
        )}
        for mid in ("M1", "M2")
    ])

    # 2. “林女士是我姐姐”与“对方外婆”的限定解析
    show("2) 同位语与称谓限定", {
        "林女士->": next(m.person_id for m in wb.mentions_of("M1") if m.text == "林女士"),
        "对方外婆->": next(
            m.person_id for m in wb.mentions_of("M1") if m.kind.value == "kin_qualified"
        ),
        "座位图": wb.resolver.seating.location_to_person,
    })

    # 3. 亲属建议不等于当事人同意
    show("3) 待核实清单（向谁核实）", [
        {"id": t.id, "类型": t.kind.value, "问题": t.issue,
         "核实对象": t.ask_person_id or t.ask_post}
        for t in wb.open_tasks()
    ])

    # 4. 司仪各环节可朗读稿
    show("4) 司仪当前可用讲稿", {
        seg: wb.host_view(seg)["可朗读稿"] for seg in ("S1", "S2", "S3", "S4")
    })

    # 5. 礼宾行动卡：冲突与可执行动作分离
    show("5) 礼宾视图（致谢 S2 / 致礼 S3）", {
        "S2": wb.concierge_view("S2"),
        "S3": wb.concierge_view("S3"),
    })

    # 6. 跨岗更正与按岗回执
    correction = wb.card_manager.corrections[0]
    show("6) 跨岗更正 C6 的按岗回执", {
        "状态": correction.state.value,
        "仅负责人可启用": correction.lead_only,
        "按岗回执": wb.card_manager.ack_status_by_post("S2"),
    })

    # 7. 负责人启用回退说法
    card = wb.cards["S2:礼宾"]
    card.fallback_text = "保留外婆称呼，不请起身；统称亲友继续，不指派新行动"
    wb.card_manager.activate_fallback("S2", "礼宾")
    show("7) 启用家庭同意的回退说法后，礼宾生效卡", {
        "生效正文": card.active_version().text,
        "司仪仍可用": wb.host_view("S2")["可朗读稿"],
    })

    # 8. 两方致谢方案各自独立
    show("8) 两方致谢方案（各自决定、互不代签）", {
        fam: wb.build_thanks_plan("S2", fam).approved_terms for fam in ("甲", "乙")
    })

    # 9. 事后依据核对
    show("9) “外婆”这一称呼/带位的原始依据", wb.evidence_for("外婆"))

    # 10. 公开存档只留同意表述
    archive = wb.public_archive()
    show("10) 公开存档（已剔除内部留言与后台登记名）", {
        "公开留言": [row["id"] for row in archive["公开留言"]],
        "同意表述": [(row["person_id"], row["表述"]) for row in archive["同意表述"]],
        "行动卡可执行性": [(row["卡"], row["当前可执行"]) for row in archive["行动卡存档"]],
    })


if __name__ == "__main__":
    main()
