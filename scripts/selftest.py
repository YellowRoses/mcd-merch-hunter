#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mcd-merch-hunter · scripts/selftest.py
确定性自测：所有断言基于内置脱敏样例数据，无需 MCP Token。

运行：python scripts/selftest.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from optimizer import (  # noqa: E402
    Coupon,
    Meal,
    build_plans,
    identify_merch,
    looks_like_food,
    looks_like_merch,
    membership_fee_for,
    pick_anchor_meals,
    render_report,
    run_offline_demo,
)

PASS = 0
FAIL = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [PASS] {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}  {detail}")


def main() -> int:
    sample_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "sample_meals.json",
    )

    print("== 1. 周边识别 ==")
    meals = run_offline_demo.__globals__["load_sample_menu"](sample_path)
    merch = identify_merch(meals)
    merch_codes = [m.code for m in merch]
    check("识别出联名棒球帽 521954", "521954" in merch_codes, f"实际={merch_codes}")
    check("未把蘸酱 521966 误判为周边", "521966" not in merch_codes)
    check("未把圆筒冰淇淋误判为周边", "515837" not in merch_codes)
    check("周边按价格升序", all(
        merch[i].current_price <= merch[i + 1].current_price for i in range(len(merch) - 1)
    ))

    print("== 2. 关键词启发式 ==")
    check("「联名复古棒球帽」判为周边", looks_like_merch("麦当劳联名复古棒球帽"))
    check("「开心乐园餐」不判为周边", not looks_like_merch("麦乐鸡开心乐园餐"))
    check("「韩式烟熏芝士风味酱」判为食品", looks_like_food("韩式烟熏芝士风味酱"))
    check("「巨无霸」判为食品", looks_like_food("巨无霸"))

    print("== 3. 锚点餐品挑选 ==")
    aggressive = pick_anchor_meals(meals, aggressive=True, top_n=3)
    safe = pick_anchor_meals(meals, aggressive=False, top_n=3)
    check("激进模式取到绝对最低价 3.0 元", aggressive[0].current_price == 3.0,
          f"实际={aggressive[0].name} {aggressive[0].current_price}")
    check("保守模式排除酱料", all(
        not any(k in m.name for k in ("酱", "配料")) for m in safe
    ))
    check("保守模式最低价 > 激进模式最低价",
          safe[0].current_price > aggressive[0].current_price)
    check("锚点不含周边本身", all(m.code != "521954" for m in aggressive + safe))

    print("== 4. 净到手成本计算（含会员卡成本建模） ==")
    merch_item = next(m for m in merch if m.code == "521954")
    fee = membership_fee_for(merch_item)  # 随单购需先办麦金卡体验卡 ¥9.9
    plans = build_plans(
        merch=merch_item,
        anchor_meals=safe,
        coupons=[],
        food_need_value=30.0,
        membership_fee=fee,
    )
    check("生成了多个方案", len(plans) >= 3, f"实际={len(plans)}")
    check("方案按净成本升序", all(
        plans[i].net_cost <= plans[i + 1].net_cost for i in range(len(plans) - 1)
    ))
    # 直购单买是净成本最低方案（绝对现金底线）
    check("直购单买为净成本最低方案",
          plans[0].label == "直购（单买）", f"实际榜首={plans[0].label}")

    # 手工核算：苹果片 7.0 + 随单购价 34.9 + 会员卡 9.9 = 51.8，减刚需 30 = 21.8
    manual = None
    for p in plans:
        if p.anchor and p.anchor.code == "6102" and "随单购" in p.label:
            manual = p
            break
    check("苹果片 + 随单购 方案存在", manual is not None)
    if manual:
        check("随单购方案含会员卡，现金 ≥ 直购价",
              manual.cash >= (merch_item.original_price or merch_item.current_price) - 0.01,
              f"随单购现金={manual.cash}")
        check("手工核算净成本 = 21.8", abs(manual.net_cost - 21.8) < 0.01,
              f"实际={manual.net_cost}")

    # 最便宜餐品（保守模式应为苹果片 7.0）+ 直接购买价 39.9 = 46.9
    direct = None
    for p in plans:
        if p.anchor and p.anchor.code == "6102" and "随单购" not in p.label and p.coupon is None:
            direct = p
            break
    check("苹果片 + 直接购买 方案存在", direct is not None)
    if direct:
        check("手工核算净成本 = 16.9", abs(direct.net_cost - 16.9) < 0.01,
              f"实际={direct.net_cost}")

    # 随单购方案必须标注会员卡成本
    wo_with_card = [p for p in plans if "随单购" in p.label and "需办卡" in p.label]
    check("随单购方案标注了会员卡成本",
          any("麦金卡" in p.note for p in wo_with_card), "未标注")

    print("== 5. 优惠券门槛与排序 ==")
    coupons = [
        Coupon("C1", "满35减7", 7.0, min_spend=35.0),
        Coupon("C2", "无门槛5元", 5.0, min_spend=0.0),
    ]
    plans_with_coupon = build_plans(merch_item, safe, coupons, food_need_value=30.0)
    below = [p for p in plans_with_coupon
             if p.coupon and p.coupon.coupon_id == "C1" and p.anchor
             and (p.anchor.current_price + 39.9) < 35.0]
    check("未满门槛的券被正确过滤", len(below) == 0)

    print("== 6. 报告渲染 ==")
    report = render_report(merch_item, plans, top_n=3)
    check("报告包含周边名称", "麦当劳联名复古棒球帽" in report)
    check("报告包含净成本列", "净到手成本" in report)
    check("报告包含排名", "| 1 |" in report)

    print("== 7. 离线演示端到端 ==")
    demo = run_offline_demo(sample_path)
    check("演示输出激进与保守两套", "激进模式" in demo and "保守模式" in demo)
    check("演示输出包含结论", "最优与最差方案相差" in demo)

    print()
    print(f"结果：{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
