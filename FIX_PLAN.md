# MCP Server 修复方案：Hermes / 严格客户端报错

> 状态：方案设计（含已验证证据 + 待验证项）
> 背景：Claude Code 调用正常，Hermes（Python MCP SDK 客户端）调用 `delete`/`query` 报错

---

## 一、问题现象

Hermes 调用工具时报以下错误，而 Claude Code 同样的工具调用正常：

| 调用 | Hermes 结果 | Claude Code |
|---|---|---|
| `deleteProjectUsingDELETE` | `RuntimeError: Tool ... has an output schema but did not return structured content` | 正常（走到 confirm 提示） |
| `queryProjectByCodeUsingGET`（查空） | `Output validation error: None is not of type object` | 正常 |
| `queryProjectListPagingUsingGET_3` | `HTTP 415 Unsupported Media Type` | （_3 工具组同样坏） |

---

## 二、根因分析（四层，均已实验/源码验证）

### 第 1 层：客户端 SDK 差异 —— 为什么 Claude Code 行、Hermes 不行

- **Claude Code** 使用 **TypeScript 版** MCP SDK，**不强制验证** `outputSchema`，对 `structuredContent` 缺失/不符采用容错处理。
- **Hermes** 使用 **Python 版** MCP SDK，`mcp/client/session.py:399-423` 的 `_validate_tool_result` **强制验证**：

```python
# mcp/client/session.py:411-421（已读源码确认）
if output_schema is not None:                         # 工具声明了 outputSchema
    if result.structuredContent is None:              # ← Hermes 命中
        raise RuntimeError("...did not return structured content")
    validate(result.structuredContent, output_schema) # 非 None 也要符合 schema
```

> 结论：这是**客户端实现严格度差异**，不是 server/模型的 bug。但 server 应保证在严格客户端下也能工作。

### 第 2 层：confirm 中间件破坏了 outputSchema 契约

confirm 级工具被 `SecurityMiddleware` 短路拦截，返回的是「暂停提示」：

- 旧代码：`structuredContent = None` → 命中 `did not return structured content`
- 方案 B（已写未部署）：`structuredContent = {status:pending_confirmation,...}` → 命中 `Invalid structured content`（不符合 `data:boolean`）

> 结论：confirm 的「暂停」语义**本质上无法返回符合工具 outputSchema 的内容**，因为它不是工具的真实执行结果。这是方案 A（is_error）/ 方案 B（自定义 structured_content）无效的根本原因——验证逻辑不看 `isError`，且暂停内容天然不符 schema。

### 第 3 层：DolphinScheduler 真实响应与 spec 生成的 outputSchema 不符

FastMCP 从 OpenAPI spec 的 `responses` 自动提取 outputSchema（`extract_output_schema_from_responses`），生成的是严格类型：

```jsonc
// deleteProjectUsingDELETE outputSchema
{"data": {"type": "boolean"}, ...}      // 不允许 null
// queryProjectByCodeUsingGET outputSchema
{"data": {"type": "object", ...}, ...}  // 不允许 null
```

但 DolphinScheduler 真实返回：

- 删除成功 / 查询不存在 → `{"code":0,"msg":"success","data":null}`

`data: null` 不符合 schema → `Output validation error: None is not of type object/boolean`。

> 结论：**spec 生成的 schema 不准**（未表达 nullable），与后端真实响应冲突。

### 第 4 层（附加）：_3 重复工具组请求构造错误（HTTP 415）

- DolphinScheduler spec 中存在**重复的 operationId**，FastMCP 去重时生成 `_1`/`_3` 两组同名工具。
- 实验确认：`_1` 组请求正常，`_3` 组请求被后端拒绝（415）。
- Hermes 的工具选择策略命中了 `_3`（坏的）。

---

## 三、为什么「清空所有 outputSchema」不是正确方案

| 维度 | 问题 |
|---|---|
| 治标不治本 | 逃避验证，而非让 schema 与实际一致 |
| 副作用大 | 所有工具失去输出结构描述，LLM 丧失类型信息，回归"纯文本猜测" |
| 违反契约 | MCP 规范约定 outputSchema 与 structuredContent 应一致；清空是放弃契约 |
| 掩盖问题 | null 响应、_3 工具等真实问题被一并掩盖，后续仍会以别的形式爆发 |

> 正确思路：**让 schema 准确反映实际（nullable）**，并用 **MCP 规范的交互机制（elicitation）** 替代 confirm 链接 hack。

---

## 四、正确的修复方向（分层，按优先级）

### 修复 1（架构正解）：用 MCP elicitation 替代 confirm 链接 hack

**目标**：confirm 级操作在**一次 `tools/call` 内**完成「请求用户确认 → 执行 → 返回真实结果」，返回的 structuredContent 是真实 API 响应，天然符合 outputSchema。

**做法**：
- 工具执行时，server 通过 MCP **elicitation** 请求客户端向用户弹出确认框（展示工具名、参数）。
- 用户确认 → server 执行真实 API → 返回真实响应（符合 schema）。
- 用户拒绝 → 返回标准错误结果。
- 移除 confirm 确认链接、自定义 structured_content、`is_error` hack 等 workaround。

**优点**：
- 符合 MCP 规范，不破坏 outputSchema 契约。
- Claude Code、Cursor 等支持 elicitation 的客户端原生体验更好。
- 去掉自建 HTTP 确认页的复杂度（跨线程 executor、ConfirmQueue 等）。

**待验证**：
- FastMCP 3.4.2 中**在 middleware 的 `on_call_tool` 内发起 elicitation** 的具体 API（确认 `context` 是否暴露 elicit 方法；mcp SDK 已见 `UrlElicitationRequiredError`，elicitation 能力存在）。
- **Hermes 是否支持 elicitation**。若不支持，需保留 confirm 链接作为 fallback，但要让它返回符合 outputSchema 的内容（见修复 4 折中）。

### 修复 2（必需）：修正 outputSchema 为 nullable，匹配真实响应

**目标**：让 DolphinScheduler 的 `data:null` 真实响应能通过验证。

**做法**：在 `fix_openapi_spec` 中，对每个 operation 的 `responses` schema，把 `data` 等字段放宽为允许 null。jsonschema 标准（Python SDK 用 jsonschema 验证）写法：

```jsonc
// 放宽前
{"data": {"type": "object"}}
// 放宽后（二选一）
{"data": {"type": ["object", "null"]}}
{"data": {"anyOf": [{"type": "object"}, {"type": "null"}]}}
```

**优点**：schema 与实际一致，是「修正」而非「放弃」，保留结构信息。
**待验证**：jsonschema 对 `["object","null"]` 与 `anyOf` 两种写法的接受度，择优。

### 修复 3（必需）：spec 去重，消除 _3 工具组的 415

**目标**：消除重复 operationId 产生的坏工具。

**做法**：
- 调研 spec 中 `queryProjectListPaging`、`deleteProject` 等为何出现重复定义（可能多个 API group / 多 path 映射同一 operationId）。
- 在 `fix_openapi_spec` 中**去重**：同一 operationId 只保留请求构造正确的那份（对照 _1 vs _3 的 method/path/Content-Type 差异，保留正确的）。
- 或在 FastMCP 工具注册后过滤掉坏的重复项。

**待验证**：spec 中重复定义的具体位置与差异（需 dump spec 对比 _1/_3 对应的原始 path item）。

### 修复 4（可选折中）：若 elicitation 不可用，仅对 confirm 级工具不声明 outputSchema

**适用场景**：Hermes 不支持 elicitation，且必须保留 confirm 链接机制时。

**做法**：仅对命中 confirm 规则的工具，在注册后清除其 `output_schema`（设为 None），其余工具保留。这样 confirm 拦截返回任何内容都不触发验证。

**定位**：比「清空所有 outputSchema」精准（只影响 confirm 类），但仍是 workaround，不及修复 1 干净。

---

## 五、建议实施路径

```
阶段一（最小风险，先让真实调用跑通）
  ├─ 修复 2：nullable outputSchema      ← 修 query/delete 真实响应的验证错误
  └─ 修复 3：spec 去重                  ← 修 _3 工具 415

阶段二（架构正解）
  └─ 修复 1：elicitation 替代 confirm   ← 根除 confirm 与 schema 的契约冲突

阶段三（清理）
  └─ 回退方案 A/B 的 is_error / 自定义 structured_content hack
     （它们是在「confirm 必须短路返回」错误前提下产生的，elicitation 后不再需要）

前置：当前 server 跑的是旧代码（confirm 返回 structuredContent=None），
       任何改动都需重新部署 / 重启才生效。
```

---

## 六、待验证清单（动手前需确认）

1. [ ] Hermes 是否支持 MCP elicitation（决定修复 1 是否能彻底替代 confirm 链接）
2. [ ] FastMCP 3.4.2 中 middleware 内发起 elicitation 的 API
3. [ ] jsonschema 对 `["object","null"]` vs `anyOf` 的接受度（修复 2 择优）
4. [ ] spec 中重复 operationId 的具体位置与 _1/_3 差异（修复 3）
5. [ ] nullable 修正后，Python SDK `_validate_tool_result` 是否真能放行 `data:null`

---

## 七、已验证证据（实验 + 源码）

- 客户端强制验证：`mcp/client/session.py:399-423`（已读源码）
- 服务端验证逻辑：`mcp/server/lowlevel/server.py:559-569`（已读源码）
- outputSchema 来源：`fastmcp/server/providers/openapi/provider.py:237 extract_output_schema_from_responses`
- 实验：`_1` 工具组 list 正常、delete 报 `did not return structured content`；`_3` 工具组 list 报 415
- 测试项目：code = `22025998089152`（尚未删除，delete 被 confirm 拦下）
