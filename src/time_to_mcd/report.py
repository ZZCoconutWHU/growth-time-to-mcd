"""报告渲染：把四个模块的结论变成「看得懂、能照做」的文字。

统一原则：
  - 先给结论，再给依据；
  - 估算值一定标注来源，不把经验值包装成实测值；
  - 每条结论都能对应到一次真实的工具调用或一次落库记录。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from . import coach
from .geo import Route
from .nutrition import NutritionReport, Suggestion
from .places import Store
from .store import Profile
from .wait import ETA, DeparturePlan, Order, progress_ratio, timeline
from .words import SessionResult, Word


def _bar(ratio: float, width: int = 12) -> str:
    ratio = max(0.0, min(1.0, ratio))
    filled = int(round(ratio * width))
    return "█" * filled + "░" * (width - filled)


# --------------------------------------------------------------------- 通勤
def render_commute(
    store: Store,
    route: Optional[Route],
    weight_kg: float,
    meal_kcal: Optional[float] = None,
    alternatives: Optional[Sequence[Route]] = None,
) -> str:
    lines = ["## 在路上", ""]
    lines.append(f"**{store.name}**　{store.distance_text}" + (f"　营业 {store.hours}" if store.hours else ""))
    if store.address:
        lines.append(f"地址：{store.address}")
    lines.append("")

    if route is None:
        lines.append("无法估算路线：门店未返回距离与坐标。")
        return "\n".join(lines)

    burned = route.calories(weight_kg)
    met = 3.5 if route.mode == "walk" else 6.8
    lines.append(f"- 方式：{route.mode_label}")
    lines.append(f"- 路程：{route.distance_text}，约 {route.minutes:g} 分钟")
    lines.append(f"- 消耗：**{burned:.0f} kcal**（MET {met} × 体重 {weight_kg:g} kg × {route.minutes:g} 分钟）")

    if meal_kcal:
        net = meal_kcal - burned
        lines.append(f"- 净摄入：{meal_kcal:.0f} − {burned:.0f} = **{net:.0f} kcal**")
        if burned >= meal_kcal * 0.15:
            lines.append("  走这一趟不是白走，相当于抵掉了这餐的一部分。")

    if route.steps:
        lines.append("")
        lines.append("**导航步骤**（腾讯位置服务）")
        for index, step in enumerate(route.steps[:6], start=1):
            lines.append(f"{index}. {step}")

    if alternatives:
        lines.append("")
        lines.append("**换个方式**")
        for alt in alternatives:
            if route and alt.mode == route.mode:
                continue
            lines.append(
                f"- {alt.mode_label}：{alt.distance_text}，{alt.minutes:g} 分钟，消耗 {alt.calories(weight_kg):.0f} kcal"
            )

    if route.source == "estimate":
        lines.append("")
        lines.append(
            "> 距离为按门店上报距离估算（含 1.25 绕行系数）。配置 `T2M_TMAP_KEY` 后可获取腾讯位置服务的真实步行/骑行路线与导航步骤。"
        )
    return "\n".join(lines)


# --------------------------------------------------------------------- 预点餐
def render_recommendation(
    profile: Optional[Profile],
    summary: Dict[str, Any],
    targets: coach.Targets,
    combos: Sequence[coach.Combo],
    menu_size: int = 0,
) -> str:
    lines = ["## 这一餐", ""]
    if profile:
        lines.append(
            f"档案：{profile.height_cm:g} cm / {profile.weight_kg:g} kg / {profile.age} 岁 · "
            f"BMI **{summary['bmi']}**（{summary['bmi_band']}）· 基础代谢 {summary['bmr']} kcal · "
            f"每日消耗 {summary['tdee']} kcal"
        )
    lines.append(
        f"本餐目标（{targets.label}）：**{targets.kcal:.0f} kcal**　蛋白 {targets.protein:.0f} g　"
        f"脂肪 {targets.fat:.0f} g　碳水 {targets.carb:.0f} g　钠 ≤ {targets.sodium:.0f} mg"
    )
    if menu_size:
        lines.append(f"营养库共 {menu_size} 个餐品，已枚举「主堡 + 小食 + 饮料」的全部组合后打分。")
    lines.append("")

    if not combos:
        lines.append("没有可推荐的组合：营养库为空或菜单未返回。")
        return "\n".join(lines)

    for index, combo in enumerate(combos[:3], start=1):
        head = "首选" if index == 1 else f"备选 {index - 1}"
        lines.append(f"**{head}：{combo.names}**")
        lines.append(
            f"- {combo.kcal:.0f} kcal　蛋白 {combo.protein:.0f} g　脂肪 {combo.fat:.0f} g　"
            f"碳水 {combo.carb:.0f} g　钠 {combo.sodium:.0f} mg"
        )
        for reason in combo.reasons:
            lines.append(f"- 达标：{reason}")
        for gap in combo.gaps:
            lines.append(f"- 缺口：{gap}")
        lines.append("")

    best = combos[0]
    lines.append(
        f"> 目标是「贴近热量目标 + 蛋白达标 + 钠可控」，不是越低越好。"
        f"首选组合与目标相差 {abs(best.kcal - targets.kcal):.0f} kcal。"
    )
    return "\n".join(lines)


# --------------------------------------------------------------------- 单词
def render_words(result: SessionResult, upcoming: Optional[Sequence[Word]] = None) -> str:
    lines = ["## 等餐的这几分钟", ""]
    lines.append(f"本局 {result.total} 题，答对 **{result.correct}** 题，正确率 **{result.accuracy:.0f}%**。")
    lines.append(f"`{_bar(result.correct / result.total if result.total else 0)}`")
    lines.append("")
    if result.new_mastered:
        lines.append(f"- 新进入长期记忆：**{result.new_mastered}** 个")
    if result.mistakes:
        words = "、".join(word.en for word in result.mistakes[:8])
        lines.append(f"- 今天错的词：{words}　明天会自动排进复习")
    if upcoming:
        lines.append(f"- 下次可以先复习：{'、'.join(w.en for w in upcoming[:5])}")
    lines.append("")
    lines.append(f"> 这 {result.duration_min:g} 分钟如果只是刷手机，不会有这 {result.total} 个词。")
    return "\n".join(lines)


# --------------------------------------------------------------------- 候餐
def render_wait(order: Order, eta: ETA, plan: DeparturePlan, word_count: int = 0) -> str:
    now = datetime.now()
    lines = ["## 还要等多久", ""]
    lines.append(f"订单 `{order.order_id or '未提供'}`" + (f" · {order.store_name}" if order.store_name else ""))
    lines.append(f"当前状态：**{order.stage_label}**（{order.status_text}）")
    lines.append("")
    ratio = progress_ratio(order, now)
    lines.append(f"`{_bar(ratio)}` {ratio * 100:.0f}%")
    lines.append("")

    if eta.remaining_min is None:
        lines.append(f"**等待时间**：无法估算 — {eta.basis}")
    elif eta.remaining_min <= 0:
        lines.append("**等待时间**：已到预计出餐时间，可以取餐了。")
    else:
        confidence = {"high": "高", "medium": "中", "low": "低"}.get(eta.confidence, "低")
        lines.append(f"**等待时间**：约 **{eta.remaining_min:.0f} 分钟**（置信度 {confidence}，依据：{eta.basis}）")

    tone = {"go": "现在出发", "wait": "再等等", "hurry": "尽快出发", "done": "已完成", "blocked": "待定"}.get(
        plan.tone, plan.tone
    )
    lines.append(f"**出发建议**：{tone} — {plan.advice}")
    steps = timeline(plan, now)
    if steps:
        lines.append("时间轴：" + " → ".join(steps))

    if word_count and eta.remaining_min:
        lines.append("")
        lines.append(f"这 {eta.remaining_min:.0f} 分钟够刷 **{word_count}** 个词，不用干等。")
    if order.pickup_code:
        lines.append("")
        lines.append(f"取餐码：**{order.pickup_code}**")
    return "\n".join(lines)


# --------------------------------------------------------------------- 营养体检
def render_nutrition(report: NutritionReport, suggestions: Sequence[Suggestion] = ()) -> str:
    lines = ["## 这餐的营养账", ""]
    if not report.items:
        lines.append("没有可分析的餐品。")
        return "\n".join(lines)

    lines.append(f"合计 **{report.total_energy:.0f} kcal**，占每日参考摄入的 {report.energy_share_of_day:.0f}%。")
    if report.budget:
        lines.append(f"目标 {report.budget:.0f} kcal，已用 {report.budget_used:.0f}%。")
        lines.append(f"`{_bar(report.total_energy / report.budget)}`")
    lines.append("")
    lines.append("| 餐品 | 数量 | 热量 | 蛋白质 | 脂肪 | 碳水 | 钠 |")
    lines.append("|---|---|---|---|---|---|---|")
    for item in report.items:
        lines.append(
            f"| {item.food.name} | {item.qty:g} | {item.energy:.0f} | {item.protein:.1f} g | "
            f"{item.fat:.1f} g | {item.carb:.1f} g | {item.sodium:.0f} mg |"
        )
    lines.append("")

    marker = {"high": "超", "warn": "注意", "ok": "正常"}
    for flag in report.flags:
        lines.append(f"- [{marker.get(flag.level, flag.level)}] {flag.text}")
    if suggestions:
        lines.append("")
        lines.append("**更轻的吃法**")
        for suggestion in suggestions[:3]:
            tag = "估算" if suggestion.estimated else "按 MCP 营养数据"
            lines.append(f"- {suggestion.title}：{suggestion.detail}（-{suggestion.save_kcal:.0f} kcal，{tag}）")
    return "\n".join(lines)


# --------------------------------------------------------------------- 成就看板
def render_stats(stats: Dict[str, Any], badges: Sequence[Dict[str, Any]], profile: Optional[Profile] = None) -> str:
    lines = ["# 与麦麦相伴 · 成长看板", ""]
    if profile:
        summary = coach.profile_summary(profile)
        lines.append(
            f"档案：{profile.weight_kg:g} kg · BMI {summary['bmi']}（{summary['bmi_band']}） · "
            f"目标 {profile.goal} · 连续 **{stats['streak_days']}** 天"
        )
        lines.append("")

    lines.append("| 健康餐 | 学过的词 | 已掌握 | 通勤消耗 | 摄入蛋白 |")
    lines.append("|---|---|---|---|---|")
    lines.append(
        f"| {stats['meals_on_target']} / {stats['meals_total']} | {stats['words_studied']} | "
        f"{stats['words_mastered']} | {stats['kcal_burned']:.0f} kcal | {stats['protein_in']:.0f} g |"
    )
    lines.append("")

    accuracy = stats["word_accuracy"]
    lines.append(f"单词正确率 `{_bar(accuracy / 100)}` {accuracy:.0f}%")
    on_target_ratio = stats["meals_on_target"] / stats["meals_total"] if stats["meals_total"] else 0
    lines.append(f"健康餐占比 `{_bar(on_target_ratio)}` {on_target_ratio * 100:.0f}%")
    lines.append(f"累计里程 `{_bar(min(stats['distance_km'] / 20, 1))}` {stats['distance_km']:.2f} km")
    lines.append("")

    lines.append("**成就**")
    for badge in badges:
        mark = "√" if badge["unlocked"] else "·"
        lines.append(f"- {mark} {badge['name']} — {badge['desc']}")

    if stats["meals_total"] == 0 and stats["words_studied"] == 0:
        lines.append("")
        lines.append("> 还没有记录。先跑一次 `near --log`、`preorder --log` 或 `play`，这里就会长出来。")
    return "\n".join(lines)


# --------------------------------------------------------------------- 一条龙
def render_journey(
    address: str,
    profile: Optional[Profile],
    summary: Dict[str, Any],
    targets: coach.Targets,
    store: Optional[Store],
    route: Optional[Route],
    combos: Sequence[coach.Combo],
    order: Optional[Order] = None,
    eta: Optional[ETA] = None,
    plan: Optional[DeparturePlan] = None,
    stats: Optional[Dict[str, Any]] = None,
    word_count: int = 0,
) -> str:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = ["# 麦麦成长时刻 · 今日行动卡", "", f"生成时间 {stamp}　起点：{address or '未提供'}", ""]

    weight = profile.weight_kg if profile else 65.0
    parts: List[str] = []
    if store and route:
        parts.append(
            f"{route.mode_label} {route.minutes:g} 分钟到 **{store.name}**（消耗 {route.calories(weight):.0f} kcal）"
        )
    if combos:
        best = combos[0]
        parts.append(f"吃 {best.names}（{best.kcal:.0f} kcal / 蛋白 {best.protein:.0f} g）")
    if eta and eta.remaining_min:
        parts.append(f"等餐约 {eta.remaining_min:.0f} 分钟，够刷 {word_count} 个词")

    lines.append("## 一句话")
    lines.append("")
    lines.append("；".join(parts) + "。" if parts else "信息不足，请补充地址或先建立档案。")
    lines.append("")
    lines.append("---")
    lines.append("")

    if store:
        lines.append(
            render_commute(
                store,
                route,
                weight,
                meal_kcal=combos[0].kcal if combos else None,
            )
        )
        lines.append("")
    if combos:
        lines.append(render_recommendation(profile, summary, targets, combos))
        lines.append("")
    if order and eta and plan:
        lines.append(render_wait(order, eta, plan, word_count=word_count))
        lines.append("")
    if stats:
        lines.append("## 累计")
        lines.append("")
        lines.append(
            f"已经和麦麦一起：吃了 **{stats['meals_on_target']}** 顿达标的饭、学了 **{stats['words_studied']}** 个词、"
            f"走了 **{stats['distance_km']:.1f} km**、消耗 **{stats['kcal_burned']:.0f} kcal**、"
            f"摄入 **{stats['protein_in']:.0f} g** 蛋白质。"
        )

    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(
        "> 营养目标按 Mifflin-St Jeor 与活动系数推算，通勤消耗按 MET 公式粗算，"
        "均不构成医疗或营养学建议；餐品与门店信息以麦当劳官方渠道为准。"
    )
    return "\n".join(lines)
