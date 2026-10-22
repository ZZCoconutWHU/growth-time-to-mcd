# MCP 接入说明 · MCP Integration

本文说明「麦麦成长时刻」实际调用了哪些麦当劳 MCP 能力、在什么条件下调用、拿到数据后做了什么，以及这些调用带来的业务价值。工具名与参数均以麦当劳 MCP Server 官方工具列表为准。

---

## 一、接入的 MCP Server

| 项目 | 值 |
|---|---|
| Server 名称 | `mcd-mcp` |
| 接入地址 | `https://mcp.mcd.cn` |
| 传输协议 | MCP Streamable HTTP（单端点 POST，JSON-RPC 2.0） |
| 鉴权方式 | `Authorization: Bearer <MCD_MCP_TOKEN>` |
| 会话机制 | `initialize` 后服务端可能下发 `Mcp-Session-Id`，后续请求回传 |
| 协议版本 | `2025-06-18`（可用 `MCD_MCP_PROTOCOL` 覆盖） |
| 限流 | 单 Token 600 次/分钟，超限返回 429 |
| 开放平台 | <https://open.mcd.cn/mcp> |

客户端实现：[`src/time_to_mcd/mcp_client.py`](src/time_to_mcd/mcp_client.py)，零第三方依赖。

```
initialize → notifications/initialized → tools/list → tools/call
```

三个容易踩的实现细节：

1. **响应可能是 SSE。** 服务端可能返回 `text/event-stream`，需逐行读 `data:` 事件、在空行边界拼 JSON，而不是直接 `read()`。
2. **会话要带回去。** `initialize` 响应头若含 `Mcp-Session-Id`，后续每次 POST 都要回传。
3. **`notifications/initialized` 不能等响应。** 它没有 `id`，服务端返回 202 空体，按"期望无响应"处理。

### 实测联调记录（2026-10-22）

| 场景 | 实际响应 | 客户端处理 |
|---|---|---|
| 不带 Token | `403` + `{"code":"0001","msg":"系统错误"}` | 抛 `MCPAuthError`，提示去控制台激活 |
| 无效 Token | `401` + `当前鉴权码不存在或已失效!` | 抛 `MCPAuthError`，带服务端原文 |
| 端点在 `https://mcp.mcd.cn` 可达 | ✅ | 握手正常 |

> 403 这档最初被漏判成普通错误，是联调时发现的真实缺陷，已修复并补测试（`tests/test_client.py::test_403_is_auth_error`）。

> 订单结构的三个坑（都会让界面静默显示成空/怪值）：
> 1. 明细字段叫 **`orderProductList`**，不是 `productList`——漏了就永远解析不出商品；
> 2. **`status` 是状态码（"40"），`orderStatus` 才是人话（"订单已完成"）**——取错就显示一串数字；
> 3. `order-list` **只给订单骨架，没有商品明细**，要逐单用 `query-order` 补齐。
> 套餐（`comboItemList`）名常常不在营养库里，匹配不到时退回子项逐项累加，绝不当成 0。

> `now-time-info` 的返回结构坑：时间**不在顶层 `timestamp`**，而是顶层 `datetime` + `data` 子对象里的
> `timestamp`(毫秒) / `datetime` / `formatted`。最初只找顶层 `timestamp/time/currentTime/serverTime`，
> 全部落空后静默退回本机时间，界面上完全看不出来。现在两层六个字段都找，并保留 `source` 标记
> （`mcp` / `local`）如实显示。

---

## 二、实际使用的 Tool 清单（9 个）

### 在路上

| Tool | 用途 | 何时调用 | 关键参数 | 代码位置 |
|---|---|---|---|---|
| `query-nearby-stores` | 查询地址附近门店（含距离、营业、服务能力、经纬度） | `near` / `map` / `journey` | `keyword` | `service.find_stores` |
| `delivery-query-stores` | 查询收货地址附近可配送门店 | 用户选麦乐送时优先 | `address` | `service.find_stores` |
| `delivery-query-addresses` | 查询已保存的配送地址 | 用户说"送到公司"但没给地址 | 无 | `service.delivery_addresses` |

### 这一餐

| Tool | 用途 | 何时调用 | 关键参数 | 代码位置 |
|---|---|---|---|---|
| `list-nutrition-foods` | 餐品营养库（能量/蛋白/脂肪/碳水/钠/钙） | 每次推荐前拉一次并缓存 | 无 | `service.catalog` |
| `query-meals` | 门店在售菜单 | 需要按门店限定候选时 | `storeCode` | `service.menu` |
| `query-meal-detail` | 套餐组成与可替换项 | 用户点套餐时展开成单品 | `storeCode`、`mealCode` | `service.menu`（预留） |

### 等餐

| Tool | 用途 | 何时调用 | 关键参数 | 代码位置 |
|---|---|---|---|---|
| `query-order` | 订单状态、内容、承诺完成时间、取餐码 | 有订单号时 | `orderId` | `service.order_detail` |
| `order-list` | 近期历史订单 | 没有订单号时取最近一单；**「记录」屏回填真实吃过的东西** | — | `service.latest_order` / `service.order_history` |
| `now-time-info` | 服务端当前时间 | 估算等待时间前，避免本地时钟漂移；**进入「这一餐吃什么」时按它自动选餐次** | 无 | `service.now` / `service.current_meal` |

### 未使用的工具（24 个）

| Tool | 不用的原因 |
|---|---|
| `create-order` / `calculate-price` / `cancel-order` | 本产品只做记录与建议，不替用户下单、支付、取消 |
| `auto-bind-coupons` / `mall-create-order` / `delivery-create-address` | 会变更账户资产或资料，超出"成长记录"问题域 |
| `draw-lottery` / `query-lottery-info` / `query-my-prizes` | 抽奖属娱乐，与"把时间变成成长"无关 |
| 其余商城 / 主题活动类 | 与本次三个方向（等待、营养、路线）无直接关系 |

**单词模块不依赖 MCP**：词库是本地 JSON，作答记录写本地 SQLite。这是有意为之——等待时间通常是弱网环境，背单词不能因为网络抖动而卡住。

---

## 三、四条调用链

### 3.1 在路上

```
地址 + 步行/骑行
   │
   ├─ tools/call query-nearby-stores { keyword }
   │      失败/为空 → delivery-query-stores { address }
   ▼
places.extract_stores()      ← 从任意嵌套结构挖出门店数组
places.normalize_store()     ← 距离 "1.2km"→1200m；服务标签中文→枚举；经纬度归一
places.rank_stores()         ← 满足所需服务 > 营业中 > 距离近
   ▼
geo.route_between()          ← 有腾讯 Key → 真实路径；无 Key → 距离 × 1.25 绕行估算
   ▼
MET 公式 → 消耗 kcal；再与这一餐热量相减得净摄入
```

### 3.2 这一餐

```
档案(身高/体重/年龄/活动量/目标)
   ▼
coach.bmr() → coach.tdee() → 本餐占比 → 热量/蛋白/脂肪/碳水目标
   ▲
   ├─ tools/call list-nutrition-foods   （进程内缓存）
   └─ tools/call query-meals { storeCode }（候选菜单，可选）
   ▼
枚举「主堡 + 小食 + 饮料」全部组合 → 打分 → Top3
   ▼
输出：达标项 / 缺口项 / 与目标相差多少 kcal
```

### 3.3 等餐

```
有订单号 → query-order { orderId }
无订单号 → order-list → 取 createTime 最新一单
   │
   ├─ now-time-info 取服务端时间
   ▼
wait.normalize_order()  ← 状态文案归一为 7 个阶段
wait.estimate_eta()     ← 有承诺时间→高置信度；否则阶段经验值→中；无法识别→低
   ▼
remaining_min × 6 题/分钟 → plan_count() → 单词局长度
   ▼
出发建议（go / wait / hurry）+ 时间轴 + 可刷词数
```

### 3.4 一条龙（`journey`）

```
地址 → 门店 + 路线 + 消耗 ─┐
档案 → 目标 + 推荐组合 ────┼─► report.render_journey() ─► 今日行动卡
订单 → 剩余时间 + 刷词数 ──┘
                            └─► SQLite：commutes / meals / word_progress
```

---

## 四、参数装配：不硬编码字段名

麦当劳 MCP 的参数在不同版本/场景下可能叫 `storeCode`、`beCode`、`storeId`。硬编码会在升级时整条链失效。

做法（[`schema.py`](src/time_to_mcd/schema.py)）：

1. `tools/list` 拿每个工具真实的 `inputSchema`（字段名 + `required`）；
2. 调用方传入的键做归一化（小写、去下划线/连字符）后与 `properties` 对齐；
3. 未命中的必填项用语义别名表兜底（`storecode ← store_code / beCode / storeId`）；
4. 仍缺失则抛错，错误信息直接打印**该工具的真实可用字段**。

---

## 五、错误处理

| 场景 | 处理 | 用户看到什么 |
|---|---|---|
| 未配置 Token | `MCPAuthError` | 申请地址 + 环境变量写法 |
| 401 / 403 | `MCPAuthError` | 指向重新获取 Token，附服务端原文 |
| 429 限流 | 读 `Retry-After`，指数退避重试 3 次 | 仍失败提示降低频率 |
| 5xx | 指数退避重试 3 次 | 打印状态码与响应片段 |
| 网络不可达 | `MCPError` | 提示检查代理与端点 |
| 工具 `isError` | `MCPError` | 指明哪个工具失败 |
| 工具不存在 | `SchemaError` | 列出服务端实际提供的工具 |
| 门店接口为空 | 自动退化到备用工具 | 正常输出，无需干预 |
| 营养库为空 | 推荐直接返回空并提示 | 提示先跑 `doctor` |
| 地图 Key 缺失 | 降级为距离估算 | 报告标注"估算 1.25 绕行系数" |
| 地图 Key 未分配某服务（腾讯 121） | 降级为距离估算 | 界面写明"腾讯位置服务返回 121：…（该服务当日额度用尽或未在控制台分配配额）" |
| 地址解析命中同名异地 | 自动加城市前缀重解析，仍异常则估算 | 界面标注"路线异常（疑地址重名）" |
| 地图调用超过 1 次/秒 | 排队等待（≤3 秒）后继续 | 未等到才降级并标注原因 |

`doctor` 一次性检查端点、Token、地图 Key、档案库，并逐项列出 9 个关键工具是否齐备。

---

## 六、业务价值

### 对用户

| 价值 | 可量化部分 |
|---|---|
| 等待不再是空的 | 7 分钟 = 42 个词，答错的词次日自动复习 |
| 点餐前有依据 | 热量目标误差控制在 ±15%，并显式给出蛋白缺口 |
| 走路也有的算 | 320 m 步行 5.3 分钟 ≈ 23 kcal，直接抵掉这餐的一部分 |
| 坚持看得见 | 达标餐数、学词数、里程、消耗、连续天数实时聚合 |

### 对麦当劳

| 价值 | 机制 |
|---|---|
| 提升到店取餐履约体验 | 准时到店率提高，取餐口不拥堵 |
| 让营养数据真正被使用 | 把官方营养表翻译成"这一餐该吃多少"的个人语言 |
| 增加门店能力曝光 | 得来速、24 小时、麦乐送在推荐理由里被显式说明 |
| 延长用户生命周期 | 成长记录形成留存钩子，把"偶尔吃一次"变成"有理由常来" |

### 技术侧

- **零依赖**：评委 clone 即跑。
- **Schema 自适应**：MCP 服务升级不需要改代码。
- **可离线验证**：`--mock` 让逻辑脱离 Token 也能被评审。
- **可嵌入**：`--json` 可被任意工作流消费，`SKILL.md` 可直接装进 WorkBuddy。

---

## 七、数据与合规

- 只读调用，不产生任何账户资产变更（不领券、不下单、不扣积分）。
- 地址、订单号仅在进程内存中使用，不写日志、不落盘。
- 成长记录仅写本地 SQLite（`data/records.db`，已 gitignore），不上传。
- 地图底图仅用腾讯位置服务；前端默认零 Key（代理模式）；Key 只在本地进程内使用。
- 遵循《麦当劳 MCP 服务规则》与麦当劳中国《使用条款》；餐品信息、价格与供应状态以官方渠道实时结果为准。
