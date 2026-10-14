#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mcd-merch-hunter · optimizer.py
麦当劳「周边到手成本」优化器

工程原则（与 MCP 分工一致）：
    取数交给 MCP，算术交给代码，表达交给 Agent。

核心公式：
    周边净到手成本 = 实付现金 + 消耗积分折算金额 - 餐食刚需价值

    · 餐食刚需价值 = 用户「本来今天就要花多少钱吃饭」
    · 净成本为负 => 这顿饭比日常还便宜，相当于白得一个周边

为什么不让大模型算：
    周边获取是典型的组合决策问题：菜单几十项、券十几张、每张券规则不同、
    还要判断「为了凑满减而加购是否划算」。多步算术 + 约束推理正是大模型
    错误率最高的场景，而价格错一位数就是事故。因此本模块做成纯标准库、
    零依赖的确定性计算，同一输入永远得到同一输出。

关键价格口径（来自 2026-10-13 / 10-14 对麦当劳 MCP 的真实接口实测）：
    merch.original_price  直接购买价。calculate-price 单独核算周边时返回的就是它
    merch.current_price   随单购价。需满足 merch.can_with_order 才可享受
    ⚠️ 实测发现：走麦金卡随单购时，calculate-price 会**强制捆绑一张麦金卡体验卡
       （默认 ¥9.9 / 15 天）**。因此「随单购标价更便宜」必须把这笔会员卡成本算进去，
       否则会得出"随单购更便宜"的片面结论——纯现金底线其实是直接购买。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, Iterable, List, Optional

# --------------------------------------------------------------------------
# 常量：周边识别启发式
# --------------------------------------------------------------------------

# 强周边关键词：命中任意一个即判定为「周边 / 非餐食商品」
MERCH_KEYWORDS: tuple = (
    "联名", "棒球帽", "帽子", "鸭舌帽", "徽章", "丝巾", "领带",
    "玩具", "公仔", "摆件", "盲盒", "钥匙扣", "挂件", "水杯",
    "礼盒", "礼包", "麻将", "沙发", "行李箱", "旅行箱", "斜挎包",
    "双肩包", "T恤", "睡衣", "袜子", "鞋", "相机", "键鼠", "回车键",
)

# 食品词：命中则不判为周边（防止「限定薯条」之类误判）
FOOD_KEYWORDS: tuple = (
    "堡", "霸", "卷", "派", "翅", "鸡", "牛", "鱼", "薯", "饮", "淇淋",
    "咖啡", "奶", "茶", "酱", "粥", "饭", "面", "虾", "蟹", "芝士条",
    "圆筒", "麦旋风", "麦乐鸡", "玉米", "苹果片", "纯悦",
)

# 锚点餐品排除项：这些虽便宜，但不算「正经餐品消费」
ANCHOR_EXCLUDE: tuple = ("酱", "配料", "加料", "餐具")

# 积分折算比例：1 元 ≈ 1 分（麦当劳会员规则：消费 1 元累积 1 分）
POINT_YUAN_RATIO = 1.0

# 随单购强制捆绑的会员卡默认成本（元）。来自 calculate-price 实测：走麦金卡随单购
# 会自动塞入一张体验卡；已是麦金卡会员则传 0。
DEFAULT_MEMBERSHIP_FEE = 9.9
MEMBERSHIP_NAME = "麦金卡体验卡(15天)"


# --------------------------------------------------------------------------
# 数据模型
# --------------------------------------------------------------------------

@dataclass
class Meal:
    """菜单中的单个商品（对应 query-meals 返回的 meals 映射项）"""
    code: str
    name: str
    current_price: float
    original_price: float = 0.0
    can_with_order: bool = False
    with_order: Optional[Dict[str, Any]] = None

    @classmethod
    def from_mcp(cls, code: str, raw: Dict[str, Any]) -> "Meal":
        return cls(
            code=code,
            name=str(raw.get("name", "")),
            current_price=_to_float(raw.get("currentPrice", 0)),
            original_price=_to_float(raw.get("originalPrice", 0)),
            can_with_order=bool(raw.get("canWithOrder", False)),
            with_order=raw.get("withOrder"),
        )

    @property
    def saving(self) -> float:
        return max(self.original_price - self.current_price, 0.0)


@dataclass
class Coupon:
    """优惠券（统一 query-my-coupons / query-store-coupons / available-coupons 三路）"""
    coupon_id: str
    name: str
    face_value: float          # 面额（元），满减券即减免金额
    min_spend: float = 0.0     # 使用门槛
    scope: str = "通用"        # 适用范围描述


@dataclass
class Plan:
    """一套「周边获取方案」"""
    merch: Meal
    anchor: Optional[Meal]
    cash: float                       # 实付现金
    points_value: float = 0.0         # 消耗积分折算金额
    coupon: Optional[Coupon] = None
    food_need_value: float = 0.0      # 餐食刚需价值（用户输入）
    label: str = ""
    note: str = ""

    @property
    def total_cost(self) -> float:
        return round(self.cash + self.points_value, 2)

    @property
    def net_cost(self) -> float:
        """周边净到手成本：为这个周边真正多掏了多少钱"""
        return round(self.total_cost - self.food_need_value, 2)

    @property
    def merch_unit_price(self) -> float:
        """把餐食按刚需价值抵扣后，折算出的周边单价"""
        if self.anchor is None:
            return self.total_cost
        # 锚点餐品视为「顺带解决吃饭」，抵扣其刚需价值
        return round(self.total_cost - self.food_need_value, 2)


# --------------------------------------------------------------------------
# 工具函数
# --------------------------------------------------------------------------

def _to_float(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def looks_like_food(name: str) -> bool:
    return any(k in name for k in FOOD_KEYWORDS)


def looks_like_merch(name: str) -> bool:
    if looks_like_food(name):
        return False
    return any(k in name for k in MERCH_KEYWORDS)


def membership_fee_for(merch: Meal, fee: float = DEFAULT_MEMBERSHIP_FEE,
                       name: str = MEMBERSHIP_NAME) -> float:
    """随单购类周边若要享受随单购价，calculate-price 实测会强制捆绑一张
    麦金卡体验卡（默认 ¥9.9）。对首次购买者这笔是必付成本，必须计入方案；
    已是麦金卡会员则传 fee=0 表示无需额外办卡。"""
    if merch.with_order and merch.with_order.get("membershipCode"):
        return fee
    return 0.0


# --------------------------------------------------------------------------
# 核心步骤 1：识别菜单中的周边商品
# --------------------------------------------------------------------------

def identify_merch(meals: Dict[str, Meal]) -> List[Meal]:
    """
    从 query-meals 的 meals 映射中识别周边商品。

    判定策略（两层，保守优先）：
      1) 名称命中强周边关键词且不含食品词  -> 判定为周边
      2) canWithOrder=True（可随单购）且名称不像食品 -> 判定为周边
      3) 其余一律不判，宁可漏判也不误判

    宁可漏判：漏判只是少推荐一个方案；误判会把食品当周边，直接算错钱。
    """
    found: List[Meal] = []
    for meal in meals.values():
        if looks_like_merch(meal.name):
            found.append(meal)
        elif meal.can_with_order and not looks_like_food(meal.name):
            found.append(meal)
    return sorted(found, key=lambda m: m.current_price)


# --------------------------------------------------------------------------
# 核心步骤 2：挑选「最便宜的达标餐品」
# --------------------------------------------------------------------------

def pick_anchor_meals(
    meals: Dict[str, Meal],
    aggressive: bool = False,
    top_n: int = 3,
) -> List[Meal]:
    """
    挑选可作为「任意餐品消费」的锚点餐品。

    规则背景（来自 campaign-calendar 的周边活动文案）：
        「任意餐品消费加 39.9 元即可得」

    两种模式：
      aggressive=True   取绝对最低价商品（如 3 元蘸酱），
                        成本最低，但「任意餐品」门槛是否放宽存疑
      aggressive=False  取最低价的正经餐品（排除酱料/配料），
                        更稳妥，保证方案一定能落地
    """
    candidates = [
        m for m in meals.values()
        if m.current_price > 0 and m.code != "" and not looks_like_merch(m.name)
    ]
    if not aggressive:
        candidates = [
            m for m in candidates
            if not any(k in m.name for k in ANCHOR_EXCLUDE)
        ]
    candidates.sort(key=lambda m: m.current_price)
    return candidates[:top_n]


# --------------------------------------------------------------------------
# 核心步骤 3：构建方案
# --------------------------------------------------------------------------

def build_plans(
    merch: Meal,
    anchor_meals: Iterable[Meal],
    coupons: Optional[List[Coupon]] = None,
    food_need_value: float = 0.0,
    membership_fee: float = 0.0,
    membership_name: str = MEMBERSHIP_NAME,
) -> List[Plan]:
    """
    为指定周边枚举可行方案。

    参数：
        merch              周边商品（来自 identify_merch）
        anchor_meals       候选锚点餐品（来自 pick_anchor_meals）
        coupons            可用优惠券（四路合并后的结果）
        food_need_value    餐食刚需价值：本来要花多少钱吃饭
        membership_fee     随单购强制捆绑的会员卡成本（元）。
                            来自 calculate-price 实测：走麦金卡随单购会
                            自动塞入一张体验卡，首次购买者必付；已是会员传 0

    价格口径（关键，来自真实接口实测）：
        merch.original_price  直接购买价。calculate-price 单独核算周边时返回的就是它
        merch.current_price   随单购价。需满足 merch.can_with_order 才可享受
        两者都存在时，说明「办麦金卡随单购」标价更低，但**要先把会员卡成本算进去**，
        否则会得出"随单购更便宜"的片面结论

    返回的方案已按周边净到手成本升序排列。
    """
    coupons = list(coupons or [])
    plans: List[Plan] = []

    # 直接购买价 vs 随单购价
    direct_price = merch.original_price or merch.current_price
    wo_price = (
        merch.current_price
        if merch.can_with_order and merch.current_price < direct_price
        else None
    )
    # 随单购是否需要先办会员卡（从 with_order 的 membershipCode 判断）
    requires_membership = bool(merch.with_order and merch.with_order.get("membershipCode"))

    # 方案 A：不搭配餐品，直接购买周边（不办卡、不凑餐，纯现金底线）
    plans.append(Plan(
        merch=merch,
        anchor=None,
        cash=round(direct_price, 2),
        food_need_value=food_need_value,
        label="直购（单买）",
        note=f"周边直接购买价 ¥{direct_price:.1f}，无需办卡、无需凑餐",
    ))

    # 方案 B：随单购直购（无餐品，但需先办会员卡）
    if wo_price:
        wo_cash = wo_price + (membership_fee if requires_membership else 0.0)
        wo_note = (
            f"周边随单购价 ¥{wo_price:.1f}"
            + (f"，但需先办{membership_name} ¥{membership_fee:.1f}"
               if requires_membership and membership_fee else "")
            + f"；标价比直接购买省 ¥{direct_price - wo_price:.1f}"
            + ("（不含会员卡成本）" if requires_membership and membership_fee else "")
        )
        plans.append(Plan(
            merch=merch,
            anchor=None,
            cash=round(wo_cash, 2),
            food_need_value=food_need_value,
            label="随单购直购（需办卡）" if (requires_membership and membership_fee) else "随单购直购",
            note=wo_note,
        ))

    for anchor in anchor_meals:
        base = anchor.current_price

        # 方案 C：最低价餐品 + 直接购买周边
        plans.append(Plan(
            merch=merch,
            anchor=anchor,
            cash=round(base + direct_price, 2),
            food_need_value=food_need_value,
            label=f"{anchor.name} + {merch.name}",
            note=f"锚点：{anchor.name} ¥{base:.1f}；周边直购价",
        ))

        # 方案 D：最低价餐品 + 随单购价周边（含强制会员卡成本）
        if wo_price:
            d_cash = base + wo_price + (membership_fee if requires_membership else 0.0)
            d_note = (
                f"锚点：{anchor.name} ¥{base:.1f}；周边走随单购价"
                + (f"；需先办{membership_name} ¥{membership_fee:.1f}"
                   if requires_membership and membership_fee else "")
            )
            plans.append(Plan(
                merch=merch,
                anchor=anchor,
                cash=round(d_cash, 2),
                food_need_value=food_need_value,
                label=f"{anchor.name} + {merch.name}（随单购）",
                note=d_note,
            ))

        # 方案 E：叠加优惠券
        subtotal = base + direct_price
        for c in coupons:
            if c.min_spend > 0 and subtotal < c.min_spend:
                continue
            plans.append(Plan(
                merch=merch,
                anchor=anchor,
                coupon=c,
                cash=round(subtotal - c.face_value, 2),
                food_need_value=food_need_value,
                label=f"{anchor.name} + 券「{c.name}」",
                note=f"锚点：{anchor.name}；用券减免 ¥{c.face_value:.1f}",
            ))

    plans.sort(key=lambda p: p.net_cost)
    return plans


# --------------------------------------------------------------------------
# 核心步骤 4：渲染报告
# --------------------------------------------------------------------------

def render_report(merch: Meal, plans: List[Plan], top_n: int = 3) -> str:
    """把 Top-N 方案渲染成 Markdown 表格（直接给用户看）"""
    if not plans:
        return "未找到可行方案。"

    lines: List[str] = []
    lines.append(f"### 目标周边：{merch.name}")
    lines.append("")
    direct = merch.original_price or merch.current_price
    lines.append(f"官方定价 ¥{direct:.1f} 元"
                 + (f"（随单购价 ¥{merch.current_price:.1f} 元，省 "
                    f"¥{direct - merch.current_price:.1f}）"
                    if merch.can_with_order and merch.current_price < direct else ""))
    if merch.with_order and merch.with_order.get("membershipCode"):
        lines.append("> ⚠️ 随单购价需先办麦金卡体验卡（¥9.9/15天），"
                     "计入后纯现金底线通常是直购价。")
    lines.append("")
    lines.append("| 排名 | 方案 | 实付现金 | 净到手成本 | 说明 |")
    lines.append("|---:|---|---:|---:|---|")
    for i, p in enumerate(plans[:top_n], 1):
        if p.anchor:
            scheme = f"{p.anchor.name}({p.anchor.current_price:.1f}) + {p.merch.name}"
        else:
            scheme = p.label
        coupon_desc = f" + 券[{p.coupon.name}]" if p.coupon else ""
        lines.append(
            f"| {i} | {scheme}{coupon_desc} "
            f"| ¥{p.total_cost:.2f} | **¥{p.net_cost:.2f}** | {p.note} |"
        )
    lines.append("")
    best = plans[0]
    worst = plans[-1]
    gap = worst.net_cost - best.net_cost
    if gap > 0:
        lines.append(f"> 最优与最差方案相差 **¥{gap:.2f}**。")
    if best.net_cost <= 0:
        lines.append("> 净成本为负：这顿饭本身比日常更便宜，相当于白得这个周边。")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 离线样例入口：无 MCP Token 也能完整跑通
# --------------------------------------------------------------------------

def load_sample_menu(path: str = "data/sample_meals.json") -> Dict[str, Meal]:
    """加载脱敏样例菜单（真实接口返回裁剪而来）"""
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    return {code: Meal.from_mcp(code, item) for code, item in raw["meals"].items()}


def run_offline_demo(path: str = "data/sample_meals.json") -> str:
    """
    用内置样例数据演示完整优化流程。
    无需 MCP Token，用于复现、审查与演示。

    会员卡成本：随单购类周边在 calculate-price 实测会被强制捆绑麦金卡体验卡，
    这里对需要会员的周边默认计入 ¥9.9（模拟首次购买者）；已是会员可传 membership_fee=0。
    """
    meals = load_sample_menu(path)

    merch_list = identify_merch(meals)
    if not merch_list:
        return "样例菜单中未识别到周边商品。"

    anchors_aggressive = pick_anchor_meals(meals, aggressive=True, top_n=3)
    anchors_safe = pick_anchor_meals(meals, aggressive=False, top_n=3)

    # 示例：用户「本来今天就要花 30 元吃饭」
    food_need_value = 30.0

    output: List[str] = []

    output.append("## 激进模式：绝对最低价商品做锚点")
    output.append("> 成本最低，但「任意餐品」门槛是否放宽需以门店实际为准")
    for merch in merch_list:
        fee = membership_fee_for(merch)
        plans = build_plans(merch, anchors_aggressive, [],
                            food_need_value=food_need_value,
                            membership_fee=fee)
        output.append(render_report(merch, plans))

    output.append("## 保守模式：最低价正经餐品做锚点")
    output.append("> 排除酱料/配料，方案一定能落地")
    for merch in merch_list:
        fee = membership_fee_for(merch)
        plans = build_plans(merch, anchors_safe, [],
                            food_need_value=food_need_value,
                            membership_fee=fee)
        output.append(render_report(merch, plans))

    return "\n\n".join(output)


if __name__ == "__main__":
    print(run_offline_demo())
