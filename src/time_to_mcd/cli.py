"""命令行入口。

    python run.py profile set --height 178 --weight 72 --age 30 --goal lose
    python run.py near    --address "上海市人民广场" --mode walk
    python run.py preorder --meal lunch
    python run.py play    --minutes 7
    python run.py wait    --travel 6
    python run.py map     --address "上海市人民广场"
    python run.py journey --address "上海市人民广场" --meal lunch
    python run.py stats

任何命令加 --mock 即可用 fixtures 离线跑通。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional, Sequence

from . import coach
from . import words as wds
from .config import load_settings
from .geo import estimate_route, render_map_html
from .mcp_client import MCPAuthError, MCPClient, MCPError
from .report import (
    render_commute,
    render_journey,
    render_recommendation,
    render_stats,
    render_wait,
    render_words,
)
from .service import USED_TOOLS, CompassService, build_service
from .wait import timeline

SERVICE_KEYS = ["dinein", "takeaway", "drivethru", "delivery", "24h"]


def _prepare_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass


def _emit(text: str, out: Optional[str]) -> None:
    print(text)
    if out:
        path = Path(out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8")
        print(f"\n已写入 {path}", file=sys.stderr)


def _need_set(values: Optional[Sequence[str]]) -> set:
    need = set()
    for value in values or []:
        for token in str(value).replace("，", ",").split(","):
            token = token.strip().lower()
            if token in SERVICE_KEYS:
                need.add(token)
    return need


def _weight(service: CompassService) -> float:
    profile = service.profile()
    return profile.weight_kg if profile else 65.0


# --------------------------------------------------------------------- 子命令
def cmd_doctor(args, service: CompassService, client: MCPClient, settings) -> int:
    print("# 环境自检\n")
    print(f"- MCP 端点：{settings.url}")
    print(f"- Token：{settings.masked_token}")
    print(f"- 地图 Key：{'已配置（腾讯位置服务）' if settings.tmap_key else '未配置（路线按距离估算）'}")
    print(f"- 成长档案：{service.db.path}")
    print(f"- 模式：{'离线 fixtures（--mock）' if settings.mock else '真实 MCP'}")

    if settings.mock:
        tools = service.available_tools()
        print(f"- 可用工具：{len(tools)} 个（mock）")
        print("\n当前为离线模式，链路可直接跑通；申请到 Token 后去掉 --mock 即切换真实调用。")
        return 0

    if not settings.token:
        print("\n未检测到 MCD_MCP_TOKEN。")
        print("申请地址：https://open.mcd.cn/mcp → 登录 → 控制台 → 激活 → 复制 Token")
        print("配置方式：export MCD_MCP_TOKEN=xxx  或写入项目根目录 .env")
        return 2

    try:
        info = client.ensure_ready().server_info or {}
        tools = service.available_tools()
    except MCPAuthError as exc:
        print(f"\n鉴权失败：{exc}")
        return 2
    except MCPError as exc:
        print(f"\n连接失败：{exc}")
        return 3

    if info:
        print(f"- 服务端：{info.get('name', '未知')} {info.get('version', '')}")
    print(f"- 可用工具：{len(tools)} 个")

    print(f"\n本项目依赖 {len(USED_TOOLS)} 个工具：\n")
    print("| 角色 | 工具 | 状态 |")
    print("|---|---|---|")
    missing = [tool for tool in USED_TOOLS if tool not in tools]
    for tool, role in USED_TOOLS.items():
        print(f"| {role} | `{tool}` | {'已提供' if tool in tools else '**缺失**'} |")
    if missing:
        print(f"\n缺失 {len(missing)} 个工具，相关能力将不可用：{', '.join(missing)}")
        return 1
    print("\n九个工具齐备，通勤 / 营养 / 候餐三条链路均可运行。")
    return 0


def cmd_tools(args, service: CompassService, client: MCPClient, settings) -> int:
    rows = service.box.describe()
    if args.used:
        rows = [row for row in rows if row["name"] in USED_TOOLS]
    if args.json:
        for row in rows:
            row["used_by_project"] = row["name"] in USED_TOOLS
            row["role"] = USED_TOOLS.get(row["name"], "")
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0

    print(f"# 可用工具（共 {len(service.box.tools)} 个，本项目使用 {len(USED_TOOLS)} 个）\n")
    print("| 使用 | 工具 | 必填参数 | 全部参数 | 说明 |")
    print("|---|---|---|---|---|")
    for row in rows:
        desc = (row["description"] or "").replace("|", "/")[:70]
        mark = "√" if row["name"] in USED_TOOLS else ""
        print(
            f"| {mark} | `{row['name']}` | {', '.join(row['required']) or '—'} | "
            f"{', '.join(row['properties']) or '—'} | {desc} |"
        )
    return 0


def cmd_profile(args, service: CompassService, client: MCPClient, settings) -> int:
    if args.action == "set":
        profile = service.save_profile(
            height_cm=args.height,
            weight_kg=args.weight,
            age=args.age,
            sex=args.sex,
            activity=args.activity,
            goal=args.goal,
        )
        print("档案已保存。\n")
    else:
        profile = service.profile()
        if profile is None:
            print("还没有档案。先建立一份：\n")
            print("  python run.py profile set --height 178 --weight 72 --age 30 --goal lose")
            return 1

    summary = coach.profile_summary(profile)
    print("# 我的档案\n")
    print(f"- 身高 {profile.height_cm:g} cm　体重 {profile.weight_kg:g} kg　年龄 {profile.age}")
    print(f"- 性别 {profile.sex}　活动量 {profile.activity}　目标 {profile.goal}")
    print(f"- BMI **{summary['bmi']}**（{summary['bmi_band']}）")
    print(f"- 基础代谢 {summary['bmr']} kcal　每日消耗 {summary['tdee']} kcal")
    print("\n| 餐次 | 热量目标 | 蛋白 | 脂肪 | 碳水 |")
    print("|---|---|---|---|---|")
    for meal in ("breakfast", "lunch", "dinner", "snack"):
        targets = service.targets(profile, meal)
        print(
            f"| {targets.label} | {targets.kcal:.0f} kcal | {targets.protein:.0f} g | "
            f"{targets.fat:.0f} g | {targets.carb:.0f} g |"
        )
    return 0


def cmd_near(args, service: CompassService, client: MCPClient, settings) -> int:
    need = _need_set(args.need)
    stores = service.find_stores(
        args.address,
        need=need,
        max_distance_m=args.max_distance,
        limit=max(args.limit, 1),
        prefer_delivery="delivery" in need,
    )
    if not stores:
        print("没有找到符合条件的门店。换个地标或放宽距离再试。")
        return 1

    weight = _weight(service)
    print("## 就近门店\n")
    print(f"按「服务能力 → 营业 → 距离」排序；消耗按 MET 公式 × 体重 {weight:g} kg 估算。\n")
    print("| 推荐 | 门店 | 距离 | 营业 | 耗时 | 消耗 |")
    print("|---|---|---|---|---|---|")
    for index, store in enumerate(stores, start=1):
        route = estimate_route(store.distance_m or 0, args.mode)
        print(
            f"| {index} | {store.name} | {store.distance_text} | {store.hours or '—'} | "
            f"{route.minutes:g} 分钟 | {route.calories(weight):.0f} kcal |"
        )
    print("")

    best = stores[0]
    route = service.commute(best, mode=args.mode)
    _emit(render_commute(best, route, weight), args.out)

    if args.log:
        service.log_commute(best, route, weight)
        print(
            f"\n已记录一次{route.mode_label}（{route.distance_m:.0f} m / {route.calories(weight):.0f} kcal）",
            file=sys.stderr,
        )
    return 0


def cmd_preorder(args, service: CompassService, client: MCPClient, settings) -> int:
    profile = service.profile_or_default()
    targets, combos, catalog_size = service.recommend(profile, meal=args.meal, limit=args.limit)
    if not combos:
        print("营养库为空或菜单未返回，无法生成推荐。先跑 doctor 检查 list-nutrition-foods。")
        return 1

    if args.json:
        payload = {
            "targets": targets.__dict__,
            "profile": coach.profile_summary(profile),
            "combos": [
                {
                    "items": [food.name for food in combo.items],
                    "kcal": combo.kcal,
                    "protein": combo.protein,
                    "fat": combo.fat,
                    "carb": combo.carb,
                    "sodium": combo.sodium,
                    "score": round(combo.score, 1),
                    "gaps": combo.gaps,
                }
                for combo in combos
            ],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    text = render_recommendation(profile, coach.profile_summary(profile), targets, combos, menu_size=catalog_size)
    _emit(text, args.out)

    if args.log:
        meal_id = service.log_combo(combos[0], targets)
        print(f"\n已记录一餐（#{meal_id}，{combos[0].kcal:.0f} kcal）", file=sys.stderr)
    return 0


def _next_words(service: CompassService, bank) -> List[wds.Word]:
    due = set(service.db.due_words(5))
    return [word for word in bank if word.en in due]


def cmd_play(args, service: CompassService, client: MCPClient, settings) -> int:
    minutes = args.minutes
    if args.from_order:
        order = service.order_detail(args.order_id)
        if order is None:
            print("没查到进行中的订单，先按 --minutes 指定的时长开始。", file=sys.stderr)
        else:
            eta, _ = service.wait_state(order, travel_min=0)
            if eta.remaining_min:
                minutes = max(2.0, min(20.0, eta.remaining_min))
                print(f"当前订单还要等约 {eta.remaining_min:.0f} 分钟，就练这么久。\n")

    bank = wds.load_wordbank()
    answers = None
    if args.answers:
        answers = [item.strip() for item in args.answers.replace("，", ",").split(",") if item.strip()]

    def ask(prompt: str) -> str:
        try:
            return input(prompt)
        except EOFError:
            return ""

    try:
        result = wds.play_session(
            service.db,
            minutes=minutes,
            bank=bank,
            ask=ask,
            answers=answers,
            seed=args.seed,
        )
    except RuntimeError as exc:
        print(f"无法开始：{exc}")
        return 1

    print("")
    _emit(render_words(result, upcoming=_next_words(service, bank)), args.out)
    return 0


def cmd_wait(args, service: CompassService, client: MCPClient, settings) -> int:
    order = service.order_detail(args.order_id)
    if order is None:
        print("没有查到订单。可用 --order-id 指定订单号。")
        return 1

    eta, plan = service.wait_state(order, travel_min=args.travel, buffer_min=args.buffer)
    word_count = wds.plan_count(eta.remaining_min or 0) if eta.remaining_min else 0

    if args.json:
        payload = {
            "order_id": order.order_id,
            "stage": order.stage_label,
            "remaining_min": eta.remaining_min,
            "confidence": eta.confidence,
            "basis": eta.basis,
            "tone": plan.tone,
            "leave_at": plan.leave_at.strftime("%H:%M") if plan.leave_at else None,
            "timeline": timeline(plan),
            "word_count": word_count,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    _emit(render_wait(order, eta, plan, word_count=word_count), args.out)
    return 0


def cmd_map(args, service: CompassService, client: MCPClient, settings) -> int:
    stores = service.find_stores(args.address, limit=1)
    if not stores:
        print("没有找到门店，无法生成地图。")
        return 1
    store = stores[0]
    if not store.has_coords:
        print("门店未返回经纬度，无法定位。")
        return 1

    route = service.commute(store, mode=args.mode)
    weight = _weight(service)
    html = render_map_html(
        store_name=store.name,
        lat=store.lat,
        lng=store.lng,
        route=route,
        origin_label=args.address,
        weight_kg=weight,
        api_key=settings.tmap_key if args.with_key else "",
        tip=(
            "路线来自腾讯位置服务。"
            if route and route.source == "tencent"
            else "未配置地图 Key，路线按门店距离估算，仅示意。"
        ),
    )
    if args.log:
        service.log_commute(store, route, weight)

    out = Path(args.out_file or "map.html")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"地图已生成：{out.resolve()}")
    print(f"门店：{store.name}　{route.distance_text}　{route.minutes:g} 分钟　消耗 {route.calories(weight):.0f} kcal")
    if not args.with_key:
        print("提示：当前为密钥代理模式；如需在普通浏览器独立打开，请配置 T2M_TMAP_KEY 并加 --with-key。")
    return 0


def cmd_journey(args, service: CompassService, client: MCPClient, settings) -> int:
    need = _need_set(args.need) or {"takeaway"}
    profile = service.profile_or_default()
    summary = coach.profile_summary(profile)
    targets, combos, _ = service.recommend(profile, meal=args.meal, limit=args.limit)

    stores = service.find_stores(args.address, need=need, limit=1)
    store = stores[0] if stores else None
    route = service.commute(store, mode=args.mode) if store else None

    order = service.order_detail(args.order_id)
    eta = plan = None
    word_count = 0
    if order is not None:
        travel = route.minutes if route else args.travel
        eta, plan = service.wait_state(order, travel_min=travel, buffer_min=2.0)
        word_count = wds.plan_count(eta.remaining_min or 0) if eta.remaining_min else 0

    stats = service.stats()
    text = render_journey(
        address=args.address,
        profile=profile,
        summary=summary,
        targets=targets,
        store=store,
        route=route,
        combos=combos,
        order=order,
        eta=eta,
        plan=plan,
        stats=stats,
        word_count=word_count,
    )
    _emit(text, args.out)

    if args.log:
        if store and route:
            service.log_commute(store, route, profile.weight_kg)
        if combos:
            service.log_combo(combos[0], targets, store.name if store else "")
        print("\n已记录本次通勤与这一餐。", file=sys.stderr)
    return 0


def cmd_stats(args, service: CompassService, client: MCPClient, settings) -> int:
    stats = service.stats()
    badges = service.badges(stats)
    profile = service.profile()

    if args.json:
        print(json.dumps({"stats": stats, "badges": badges}, ensure_ascii=False, indent=2))
        return 0

    _emit(render_stats(stats, badges, profile), args.out)
    return 0


# --------------------------------------------------------------------- 入口
def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--mock", action="store_true", help="使用 fixtures 离线运行")
    common.add_argument("--out", help="把 Markdown 报告写到指定文件")
    common.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    common.add_argument("--verbose", action="store_true", help="打印调用细节")

    sub_common = argparse.ArgumentParser(add_help=False)
    sub_common.add_argument("--mock", action="store_true", default=argparse.SUPPRESS)
    sub_common.add_argument("--out", default=argparse.SUPPRESS)
    sub_common.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    sub_common.add_argument("--verbose", action="store_true", default=argparse.SUPPRESS)

    parser = argparse.ArgumentParser(
        prog="growth-time-to-mcd",
        description="麦麦成长时刻：把去麦当劳的路和等麦当劳的时间，变成成长的时间。",
        parents=[common],
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("doctor", parents=[sub_common], help="检查 Token、地图、工具与本地档案")
    p_tools = sub.add_parser("tools", parents=[sub_common], help="列出 MCP 工具及其参数")
    p_tools.add_argument("--used", action="store_true", help="只列出本项目使用的工具")

    p_profile = sub.add_parser("profile", parents=[sub_common], help="查看或建立成长档案")
    p_profile.add_argument("action", nargs="?", default="show", choices=["show", "set"])
    p_profile.add_argument("--height", type=float, default=172, help="身高 cm")
    p_profile.add_argument("--weight", type=float, default=65, help="体重 kg")
    p_profile.add_argument("--age", type=int, default=28, help="年龄")
    p_profile.add_argument("--sex", default="male", choices=["male", "female"])
    p_profile.add_argument("--activity", default="light", choices=list(coach.ACTIVITY_FACTORS))
    p_profile.add_argument("--goal", default="lose", choices=list(coach.GOAL_ADJUST))

    p_near = sub.add_parser("near", parents=[sub_common], help="最近门店 + 通勤消耗")
    p_near.add_argument("--address", required=True, help="地址或地标")
    p_near.add_argument("--mode", default="walk", choices=["walk", "bike"], help="步行或骑行")
    p_near.add_argument("--need", action="append", help="需要的服务：dinein,takeaway,drivethru,delivery,24h")
    p_near.add_argument("--max-distance", type=float, default=None, help="最大距离（米）")
    p_near.add_argument("--limit", type=int, default=3, help="展示门店数")
    p_near.add_argument("--log", action="store_true", help="记录这次通勤")

    p_pre = sub.add_parser("preorder", parents=[sub_common], help="按身体数据推荐这一餐")
    p_pre.add_argument("--meal", default="lunch", choices=list(coach.MEAL_SHARE), help="餐次")
    p_pre.add_argument("--limit", type=int, default=3, help="推荐组合数")
    p_pre.add_argument("--log", action="store_true", help="记录这一餐")

    p_play = sub.add_parser("play", parents=[sub_common], help="等餐背单词")
    p_play.add_argument("--minutes", type=float, default=7.0, help="可用分钟数")
    p_play.add_argument("--from-order", action="store_true", help="按当前订单剩余时间自动定时长")
    p_play.add_argument("--order-id", default=None, help="订单号")
    p_play.add_argument("--answers", default=None, help="预设答案（无人值守演示用，逗号分隔）")
    p_play.add_argument("--seed", type=int, default=None, help="随机种子")

    p_wait = sub.add_parser("wait", parents=[sub_common], help="候餐进度与出发建议")
    p_wait.add_argument("--order-id", default=None, help="订单号，省略则取最近一单")
    p_wait.add_argument("--travel", type=float, default=10.0, help="到店所需分钟数")
    p_wait.add_argument("--buffer", type=float, default=2.0, help="安全余量分钟数")

    p_map = sub.add_parser("map", parents=[sub_common], help="生成合规地图页")
    p_map.add_argument("--address", required=True, help="地址或地标")
    p_map.add_argument("--mode", default="walk", choices=["walk", "bike"], help="步行或骑行")
    p_map.add_argument("--out-file", default="map.html", help="HTML 输出路径")
    p_map.add_argument("--with-key", action="store_true", help="使用 T2M_TMAP_KEY 生成可独立打开的页面")
    p_map.add_argument("--log", action="store_true", help="记录这次通勤")

    p_journey = sub.add_parser("journey", parents=[sub_common], help="一条龙：去、吃、等")
    p_journey.add_argument("--address", required=True, help="地址或地标")
    p_journey.add_argument("--meal", default="lunch", choices=list(coach.MEAL_SHARE), help="餐次")
    p_journey.add_argument("--mode", default="walk", choices=["walk", "bike"], help="通勤方式")
    p_journey.add_argument("--need", action="append", help="需要的门店服务")
    p_journey.add_argument("--limit", type=int, default=2, help="推荐组合数")
    p_journey.add_argument("--order-id", default=None, help="订单号")
    p_journey.add_argument("--travel", type=float, default=10.0, help="无路线时的到店分钟数")
    p_journey.add_argument("--log", action="store_true", help="记录通勤与这一餐")

    sub.add_parser("stats", parents=[sub_common], help="成长看板与成就")

    parser.set_defaults(mock=False, out=None, json=False, verbose=False)
    return parser


COMMANDS = {
    "doctor": cmd_doctor,
    "tools": cmd_tools,
    "profile": cmd_profile,
    "near": cmd_near,
    "preorder": cmd_preorder,
    "play": cmd_play,
    "wait": cmd_wait,
    "map": cmd_map,
    "journey": cmd_journey,
    "stats": cmd_stats,
}


def main(argv: Optional[Sequence[str]] = None) -> int:
    _prepare_console()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0

    settings = load_settings(mock=args.mock)
    try:
        service, client = build_service(settings, mock=args.mock, require_auth=args.command != "doctor")
    except MCPAuthError as exc:
        print(f"鉴权失败：{exc}", file=sys.stderr)
        return 2
    except MCPError as exc:
        print(f"连接失败：{exc}", file=sys.stderr)
        print("提示：先运行 doctor；没有 Token 时可加 --mock 离线体验完整链路。", file=sys.stderr)
        return 3

    try:
        return COMMANDS[args.command](args, service, client, settings)
    except MCPAuthError as exc:
        print(f"鉴权失败：{exc}", file=sys.stderr)
        return 2
    except (MCPError, RuntimeError) as exc:
        print(f"执行失败：{exc}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
