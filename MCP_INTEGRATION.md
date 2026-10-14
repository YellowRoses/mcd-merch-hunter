# MCP_INTEGRATION.md

本项目对麦当劳 MCP（McDonald's China MCP Server）的接入说明。

## MCP Server

| 项目 | 内容 |
|---|---|
| 接入地址 | `https://mcp.mcd.cn` |
| 传输协议 | Streamable HTTP |
| 认证方式 | 请求头 `Authorization: Bearer ${MCD_MCP_TOKEN}` |
| Token 申请 | [open.mcd.cn/mcp](https://open.mcd.cn/mcp)，手机号登录后在控制台激活（免费） |
| 限流 | 每 Token 每分钟 600 次请求 |
| 官方仓库 | [M-China/mcd-mcp-server](https://github.com/M-China/mcd-mcp-server) |

## 实际使用的 Tool

| Tool | 调用时机 | 在本项目中的用途 |
|---|---|---|
| `campaign-calendar` | 第 1 步 | 抽取周边发售规则、加价金额、开售与抢先购时间窗 |
| `now-time-info` | 第 1 步前 | 获取准确当前时间，用于抢购倒计时（不依赖模型记忆） |
| `query-nearby-stores` | 第 2 步 | 定位门店，获取 `storeCode` |
| `query-meals` | 第 3 步 | 拉取门店菜单，识别周边商品与最低价餐品 |
| `query-meal-detail` | 第 3 步（按需） | 确认套餐组成，判断是否含周边 |
| `available-coupons` | 第 4 步 | 麦麦省可领券列表 |
| `auto-bind-coupons` | 第 4 步（需确认） | 一键领取后新增的券 |
| `query-my-coupons` | 第 4 步 | 账户已有券 |
| `query-store-coupons` | 第 4 步 | 当前门店专属券 |
| `query-my-account` | 第 4 步 | 积分余额、**即将过期积分** |
| `calculate-price` | 第 5 步 | 对每个候选组合实算价格，最终核价 |

> **券必须从四路查全**：麦麦省可领券、账户已有券、门店专属券、一键领取后新增的券。少查一路，用户就少省钱。

## 调用时序

```
用户：我想拿 XX 周边 / 这个月有什么周边值得抢
  │
  ├─ now-time-info ──────────────▶ 当前时间（倒计时基准）
  ├─ campaign-calendar ──────────▶ 活动正文
  │     └─▶ LLM 抽取结构化规则：
  │           { 周边名称, 加价金额, 开售时间, 抢先购时间 }
  │
  ├─ query-nearby-stores ────────▶ storeCode
  ├─ query-meals ────────────────▶ 门店菜单
  │     ├─▶ identify_merch()      识别周边商品
  │     └─▶ pick_anchor_meals()   挑选最低价达标餐品
  │
  ├─ 四路券池合并 ───────────────▶ 可用券列表
  ├─ query-my-account ───────────▶ 积分余额 / 即将过期积分
  │
  ├─ calculate-price ────────────▶ 每个候选组合的实算价格
  │
  └─ optimizer.py ───────────────▶ 按「周边净到手成本」排序
        └─▶ 输出 Top-N 方案 + 抢购时间窗 + 风险提示
```

## 关键发现：周边本身就是菜单里的商品

这是本项目最核心的技术判断，也是把它与"读营销文案"型方案区分开的地方。

### 证据

`query-meals` 返回的菜单中，周边以标准商品实体存在（实测于 2026-10-13）：

```json
"521954": {
  "name": "麦当劳联名复古棒球帽",
  "currentPrice": "34.9",
  "originalPrice": "39.9",
  "discountType": "随单购麦金卡优惠",
  "withOrder": { "membershipCode": "106", "specId": "880" },
  "canWithOrder": true
}
```

由此得出三个可计算的结论：

1. **周边可被枚举** —— 不需要猜，遍历 `meals` 映射即可
2. **周边有双价格口径** —— `originalPrice 39.9` 是直接购买价，`currentPrice 34.9` 是随单购价，差价 ¥5 是隐藏优惠
3. **周边可进入算价与下单流程** —— `calculate-price` 对周边单独核算与组合核算均正常返回

### 算价闭环验证

| 输入组合 | `calculate-price` 返回 |
|---|---|
| 棒球帽 ×1 | `price: 3990`（¥39.90） |
| 韩式烟熏芝士风味酱(¥3.0) + 棒球帽 | `price: 4290`（¥42.90） |

组合核算无任何业务限制报错，证明「任意餐品 + 加价购周边」可被精确计算。

## 四条周边获取路径的能力盘点

| 路径 | 工具 | 数据可用性 |
|---|---|---|
| **加价购（随单购）** | `query-meals` + `calculate-price` | ✅ 完整，当期最优路径 |
| **积分兑换** | `mall-points-products`（`catRuleIds="2>8"` 周边类目） | ✅ 完整，含 `point` / `price` / `status` / `upTime` / `downTime` |
| **随餐赠玩具** | `campaign-calendar` + 菜单 `tags` | ⚠️ 半结构化（如开心乐园餐 tag「童年童款」） |
| **积分抽奖** | `query-lottery-info` | ⚠️ 消耗型操作，需用户确认后才调用 |

## 业务价值

| 维度 | 说明 |
|---|---|
| **决策价值** | 把"要不要买周边"从感性冲动变成可量化的成本比较 |
| **省钱价值** | 实测单件最多省 ¥31.5，集齐全套最多省 ¥109.5 |
| **时间价值** | 自动区分"APP 积分抢先购"与"正式开售"的时间窗 |
| **积分价值** | 识别即将过期积分，避免白白浪费 |

## 接口踩坑记录

| 坑 | 说明 |
|---|---|
| 价格单位为**分** | `calculate-price` 返回 `price: 3990`，需 ÷100 |
| `delivery-query-addresses` 不接受入参 | 传参会因 schema 校验失败 |
| `query-nearby-stores` 收藏模式常为空 | 需传 `city` + `keyword`，`searchType=2` |
| 编码体系不统一 | 优惠券商品编码与菜单商品编码是两套体系 |
| 券不承诺可用 | `query-my-coupons` 不校验门店/渠道，需 `calculate-price` 最终核实 |

## 合规约束

- 本项目**不提供**自动下单、自动支付、自动抽奖能力
- 写操作工具（`create-order` / `mall-create-order` / `draw-lottery` / `auto-bind-coupons`）仅在用户明确确认后由 Agent 提示，不由本 Skill 自动调用
- Token 只通过环境变量注入，仓库内仅保留 `${MCD_MCP_TOKEN}` 占位符
