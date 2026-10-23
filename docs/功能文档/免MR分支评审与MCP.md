# 免 MR 分支评审 + 远程 MCP

## 1. 背景

MR webhook 评审要求先发起 MR。分支评审（`source="branch"`）允许**不发起 MR**，
直接给「项目 + 源分支 + 目标分支」触发一次完整 OCR 审核，适合:

- 早期开发阶段自查（commit 推上分支即审）
- 内部平台（multica 等）经 MCP 集成，在平台侧直接发起/跟踪评审

审核核心（`ocr review`）本来就是 MR 无关的，分支评审与 MR 评审共用同一 worker
线程池、bare 仓库缓存与补发循环，仅入口与结果投递不同。

## 2. 与 MR 评审的差异

| 维度 | MR 评审（webhook/api） | 分支评审（branch） |
|---|---|---|
| 触发 | MR webhook / `POST /review` | `POST /api/review-branch` / MCP `trigger_review` |
| 触发门禁 | 目标分支 / 标题前缀策略 | 无（手动触发即显式意图） |
| 任务标识 | `mr_iid` 非空 | `mr_iid = ''`（worker 门控标记） |
| commit 钉死 | 提交时取 webhook last_commit，worker 再比对 MR head | 提交时解析分支 head 并钉死，不再变更 |
| 结果投递 | MR inline 评论 + 结论 discussion | 源分支 head commit 评论 |
| 去重 | (project_id, mr_iid, commit_sha) | (project_id, '', commit_sha) |
| 超级替换 | MR head 前进则旧任务 superseded | 同项目同分支新提交则旧 queued 任务 superseded |
| 推送 | 艾特 MR 作者 | 艾特提交时传入的 `author` + 控制台报告链接 |

## 3. 对外接口

### `POST /api/review-branch`

```json
{
  "project_url": "http://code.jms.com/group/repo.git",
  "source_branch": "feature-x",
  "target_branch": "main",
  "commit_sha": null,
  "author": "chenbo.chen1"
}
```

- **项目以仓库 URL 为主标识**（本地 clone 里只有 remote URL，数字 ID 要去 GitLab 查）：
  `project_url` 支持 http(s)/`ssh://git@host:port/`/`git@host:` 三种 remote 形式，
  归一化（去协议/userinfo/端口/`.git`，host 小写）后匹配注册行。
- `project_id`（数字）可选，兼容老调用方；两者至少提供一个。
- `commit_sha`：可选；缺省取源分支当前 head。
- `author`：可选；GitLab 用户名，推送通知时艾特。

身份解析顺序：显式 `project_id` 直接用 → URL 命中已注册行则复用该行 →
未注册时 GitLab API best-effort 解析数字 ID → 解析不出（如全局 token 无权限）
用 URL-编码路径 ID（`group%2Fproject`，GitLab API 合法形态）**一等注册**，
之后照样可绑专属 git token / 推送配置。

返回（`project_id` 为解析后的生效行 ID）：

```json
{ "task_id": "uuid", "status": "queued", "commit_sha": "40位sha", "duplicate": false, "project_id": "123" }
```

错误码（`detail.code`，与控制台同风格）：

| HTTP | code | 场景 |
|---|---|---|
| 400 | `project_not_registered` | 无法确定项目身份（ID 与 URL 均不可用） |
| 404 | `branch_not_found` | 源分支不存在 / 无法访问 |
| 429 | `queue_full` | 排队任务数达 `MAX_QUEUED_REVIEWS` |
| 422 | - | 请求体校验失败（ID 与 URL 都没给等） |

之后用 `GET /status/{task_id}` 轮询，`status=done` 后控制台
`/console?task_id=...` 看报告，问题明细在任务详情页。

### MCP（streamable HTTP）

配置 `MCP_TOKEN` 后挂载，端点 **`{base}/mcp/mcp`**（注意两层 `/mcp`），
鉴权 `Authorization: Bearer <MCP_TOKEN>`；未配置 token 则完全不挂载。

5 个工具（均直连服务内部 storage/orchestrator，不占审核线程池）：

| 工具 | 说明 |
|---|---|
| `trigger_review` | 提交分支评审（同 `POST /api/review-branch`），`project_url` 为主标识，返回 task_id 与解析后的 project_id |
| `search_tasks` | 列任务，按项目/分支/状态/approve/来源过滤 |
| `get_findings` | 取任务全部问题明细（`min_severity` 过滤，按严重度降序） |
| `wait_review` | 轮询等待任务终态（≤300s，超时返回 `state=timeout` 可续调） |
| `dashboard` | 控制台概览（任务量/通过率/严重度分布/token 消耗） |

multica / Claude 等客户端接入示例（.mcp.json / mcpServers 配置）：

```json
{
  "mcpServers": {
    "ocr": {
      "type": "http",
      "url": "https://ocr.example.com/mcp/mcp",
      "headers": { "Authorization": "Bearer <MCP_TOKEN>" }
    }
  }
}
```

命令行验证握手：

```bash
curl -X POST https://ocr.example.com/mcp/mcp \
  -H "Authorization: Bearer <MCP_TOKEN>" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}'
```

## 4. 结果投递（三处）

1. **控制台报告**：任务详情页（结论 + 问题明细 + 原始结果），永久可查。
2. **源分支 head commit 评论**（GitLab commit comments API）:
   - 汇总评论（不带行定位，永远可发）：结论头 + 总结 + 全部问题明细，
     超 `NOTE_BODY_LIMIT`(40000) 字符自动拆多条；
   - 行级评论（best-effort）：每条问题定位 `path:line`，上限 50 条；
     行号基于 parent..commit diff，与 target..source diff 可能错位，
     失败仅记日志（完整内容已在汇总评论，不丢失）。
3. **群推送**：同现有飞书/企微/钉钉通道；分支评审额外带「查看报告」链接
   （`OCR_PUBLIC_BASE_URL` 配置时），艾特提交时的 `author`。

投递失败补偿：`gitlab_posted=0` 的终态任务由 repost_worker 每 30s 轮询，
分支任务从库里（summary + findings）重建报告补发，按 `REPOST_MAX_ATTEMPTS`
重试后放弃。分支任务在 repost 路径被显式分流，绝不落到 MR note API。

## 5. 实现要点

- `app/orchestrator.py`：`start_branch_review()` 入口；`do_review_async()` 以
  `is_branch = not task.mr_iid` 门控 5 处 MR 步骤（MR 状态校验、diff_refs
  超级替换、pending discussion、inline 回写、失败结论 note）。
- `app/branch_report.py`：报告构建（复用 `repost.sort_findings_by_severity` /
  `split_chunks`）与 commit 评论投递、repost 重建。
- `app/gitlab_client.py`：`post_commit_comment()`（POST
  `/projects/{id}/repository/commits/{sha}/comments`）。
- `app/mcp_server.py`：官方 `mcp` SDK（v2 `MCPServer`）streamable HTTP 子应用
  + 纯 ASGI Bearer 鉴权中间件；挂载于 `app/main.py`，lifespan 手动接线
  session manager（mounted 子应用的 lifespan 不随 FastAPI 自动执行）。
- 存储迁移：`review_task` 新增 `author` 列（`_ensure_columns` 自动补列）。

## 6. 项目身份与单行保证（URL 一等身份）

数字 project_id 只存在 GitLab 服务端、本地拿不到；且全局 `GITLAB_TOKEN` 可能对
某些仓库无权限（连数字 ID 都解析不出）。因此 **URL 是一等的注册身份**，数字 ID
只是"能解析到就用"的优选值。

- `review_project` 行同时持有 id（主键）与 url（`project_url` + `url_key`
  归一化匹配键，部分唯一索引）；存量行启动时自动回填 `url_key`。
- **同一仓库无论数字 ID 还是 URL 先到，最终只有一行**：
  - 数字行先建、URL 后到 → URL 补进该行，不插新行；
  - URL 行先建（路径 ID `group%2Fproject`）、数字 ID 后到 → 该行**原地升级**
    为主键数字 ID（标签绑定随迁），不删行；
  - 仅"数字无 URL 行 + 路径行"历史裂行才走合并分支删掉多余行，自愈回一行。
- **token 权限场景闭环**：控制台贴 URL 注册（解析失败也成功，得路径 ID 行）
  → 绑专属 git token → `trigger_review(project_url=...)` → 命中该行用专属
  token 拉代码/发评论。全局 token 无权限的仓库全程可用。
- 任务里存的 ID 形态与注册行不一致时（如路径行升级成数字主键后，历史任务还带
  旧路径 ID），token/通道解析按 `task.project_url` 归一化兜底找回同一行。
- 控制台「添加项目」同样支持只贴 URL；重复贴同一 URL 幂等返回现有行。

## 7. 配置

| 变量 | 默认 | 说明 |
|---|---|---|
| `OCR_PUBLIC_BASE_URL` | 空 | 服务对外根地址；推送里拼接 `/console?task_id=` 报告链接，空则不带 |
| `MCP_TOKEN` | 空 | 远程 MCP Bearer 令牌；空则不挂载 `/mcp` |

## 8. 风险与边界

- **行级评论定位错位**：parent..commit ≠ target..source diff → 行评 best-effort、
  失败静默；完整报告在汇总评论。
- **同 commit 去重**：cherry-pick 到多分支的同一 sha 只审一次（可接受）。
- **wait_review 长阻塞**：占 MCP SDK 线程池线程至 300s（低频可接受），
  不占审核 executor。
- **DNS rebinding 防护**：SDK 对 localhost host 自动开启会拒绝外部域名请求，
  `build_asgi_app()` 传 `host="0.0.0.0"` 关闭，鉴权由 Bearer 中间件负责。
