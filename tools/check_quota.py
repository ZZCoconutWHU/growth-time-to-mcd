"""配额体检工具：直接打腾讯位置服务，验证已分配的服务是否可用。

用法：
    python tools/check_quota.py

会依次测试：
  必需（缺了就只能用估算路线）
    1. 地址解析（地址转坐标）  /ws/geocoder/v1/?address=*
    2. 步行路线规划            /ws/direction/v1/walking/
    3. 骑行路线规划            /ws/direction/v1/bicycling/
  可选增强（缺了会自动降级，不影响主流程）
    4. 逆地址解析（坐标转地址）/ws/geocoder/v1/?location=*
  不需要（代码里只有 geocoder 触限时才会尝试的兜底路径，没分也照样跑）
    5. 地点搜索                /ws/place/v1/search/

只读体检，不写入项目数据（一次 5 次调用）。
"""

from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "src"))

from time_to_mcd.config import load_settings  # noqa: E402

BASE = "https://apis.map.qq.com/ws"
# 上海静安寺 → 南京西路 附近，两段都在市中心，路线距离合理
ORIGIN = (31.2244, 121.4453)
DEST = (31.2320, 121.4500)


def call(name: str, path: str, params: dict, key: str) -> dict:
    url = f"{BASE}/{path}?{urllib.parse.urlencode({**params, 'key': key})}"
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001
        return {"name": name, "ok": False, "status": "网络异常", "message": str(exc)}
    status = data.get("status")
    ok = status == 0
    info = {"name": name, "ok": ok, "status": status, "message": data.get("message") or ""}
    if ok:
        result = data.get("result") or {}
        if "location" in result:
            info["detail"] = result["location"]
        routes = (result.get("routes") or [])
        if routes:
            info["detail"] = {
                "distance": routes[0].get("distance"),
                "duration": routes[0].get("duration"),
            }
        if result.get("address"):
            info["detail"] = result.get("address")
        if result.get("data"):
            info["detail"] = (result["data"][0].get("title"), result["data"][0].get("location"))
    return info


def main() -> int:
    settings = load_settings()
    key = settings.tmap_key
    if not key:
        print("未配置 T2M_TMAP_KEY（检查 .env）")
        return 1
    print(f"Key: {key[:6]}...{key[-4:]}  长度 {len(key)}")
    print("-" * 60)

    # required=True 的服务缺了就出不来真实路线；其余只是增强/兜底
    cases = [
        ("地址解析(地址→坐标)", "geocoder/v1/", {"address": "上海市静安区南京西路1601号"}, True),
        ("步行路线规划", "direction/v1/walking/", {"from": f"{ORIGIN[0]},{ORIGIN[1]}", "to": f"{DEST[0]},{DEST[1]}"}, True),
        ("骑行路线规划", "direction/v1/bicycling/", {"from": f"{ORIGIN[0]},{ORIGIN[1]}", "to": f"{DEST[0]},{DEST[1]}"}, True),
        ("逆地址解析(坐标→地址)", "geocoder/v1/", {"location": f"{ORIGIN[0]},{ORIGIN[1]}"}, False),
        ("地点搜索(不需要)", "place/v1/search/", {"keyword": "麦当劳", "boundary": "region(上海,0)", "page_size": 1}, False),
    ]

    all_ok = True
    for name, path, params, required in cases:
        info = call(name, path, params, key)
        if info["ok"]:
            flag = "OK  "
        elif required:
            flag = "FAIL"
            all_ok = False
        else:
            flag = " -- "
        print(f"{flag} {name:22s} status={info['status']} {info['message']} {info.get('detail', '')}")

    print("-" * 60)
    if all_ok:
        print("结论：必需服务全部可用，真实路线能力已就绪（-- 为可选增强，缺了会自动降级）")
    else:
        print("结论：仍有必需服务未开通或额度不足 → 去 lbs.qq.com 控制台给该 Key 分配对应服务")
    return 0 if all_ok else 2


if __name__ == "__main__":
    sys.exit(main())
