# OCR Server 部署指南

基于 GitLab MR 的自动代码审核服务。审核逻辑由 `ocr`(阿里 open-code-review)完成,本服务封装为 HTTP API,供 GitLab CI 调用,按 severity 卡点(critical/high 阻止 merge)。

完整设计见 `~/.claude/plans/ocr-server-mr-review.md`。

## 部署方式

### 方式一: Docker Compose 部署 (推荐)

```bash
# 1. 复制并配置环境变量
cp .env.example .env
# 编辑 .env，填入必填项: OCR_LLM_URL, OCR_LLM_TOKEN, GITLAB_URL, GITLAB_TOKEN

# 2. 构建并启动
docker-compose up -d

# 3. 查看日志
docker-compose logs -f

# 4. 验证服务
curl http://localhost:8000/health
```

详细 Docker 配置见根目录 `Dockerfile` 和 `docker-compose.yml`。

---

### 方式二: systemd 原生部署 (以下为传统方式)

## 架构

```
GitLab MR ──触发──> CI Job ──POST /review──> OCR Server ──> ocr review --format json
                                                   │                │
                                                   │   git fetch    ▼
                                                   │  (增量缓存)   LLM 内网端点
                                                   │
                                                   └──> 回写 MR 评论 + 返回 approve
CI Job: approve=False 则 exit 1 ──> pipeline 失败 ──> 阻止 merge
```

## 一、服务器环境准备(Linux)

```bash
# 1. Node.js(装 ocr)
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo bash -
sudo apt-get install -y nodejs

# 2. Python 3.10+ 和 git
sudo apt-get install -y python3 python3-venv python3-pip git

# 3. 装 ocr(固定版本,避免自动更新引入坏版本)
sudo npm install -g @alibaba-group/open-code-review@1.7.6

# 4. ★ 校验 ocr 二进制完整性(重要!踩过 exe 截断的坑)
ls -la $(npm root -g)/@alibaba-group/open-code-review/node_modules/@alibaba-group/ocr-linux-x64/bin/opencodereview
# 大小应为 ~45MB。若远小于此(如几 MB)说明被截断,需 npm pack 重下替换。

# 5. 测试 ocr + LLM 连通
ocr llm test
# 期望: Connection test successful
```

## 二、部署 OCR Server

```bash
# 1. 建目录 + 用户
sudo useradd -r -s /sbin/nologin ocr || true
sudo mkdir -p /opt/ocr-server /var/ocr/repos /var/ocr/work
sudo chown -R ocr:ocr /var/ocr

# 2. 拷贝项目代码到 /opt/ocr-server
#    (把 app/ requirements.txt 等放进去)
sudo cp -r app requirements.txt /opt/ocr-server/
cd /opt/ocr-server

# 3. 建虚拟环境装依赖
sudo -u ocr python3 -m venv .venv
sudo -u ocr .venv/bin/pip install -r requirements.txt

# 4. 配置环境变量
sudo cp .env.example .env
sudo nano .env   # 填 GITLAB_URL / GITLAB_TOKEN 等
```

## 三、配置 GitLab Token

1. GitLab 后台创建 **Project Access Token** 或 **Group Access Token**(不要用个人 token):
   - Scope: `api`
   - Role: Reporter 或 Developer
2. 把 token 填入 `/opt/ocr-server/.env` 的 `GITLAB_TOKEN`
3. 填 `GITLAB_URL`(如 `https://gitlab.yourcompany.com`)

## 四、注册 systemd 服务

```bash
sudo cp deploy/ocr.service /etc/systemd/system/
# 编辑服务文件,确认 ExecStart 路径、GITLAB_TOKEN 等环境变量
sudo systemctl daemon-reload
sudo systemctl enable --now ocr
sudo systemctl status ocr

# 验证
curl http://localhost:8000/health
# 期望: {"status":"ok","ocr_bin":"ocr","llm_url":"http://your-llm-endpoint.example.com:8320"}
```

## 五、手动测试(不接 GitLab)

```bash
# 准备一个可访问的 git 仓库(可用公开仓库先验证链路)
curl -X POST http://localhost:8000/review \
  -H "Content-Type: application/json" \
  -d '{
    "project_id": "test-1",
    "project_url": "https://github.com/some/repo.git",
    "source_branch": "feature-x",
    "target_branch": "main",
    "mr_iid": "1",
    "commit_sha": ""
  }'

# 数分钟后返回 JSON,关注 approve / comments 字段
```

> 不配 `GITLAB_TOKEN` 时,Server 只跑审核、不回写评论(适合先验证 ocr 链路)。

## 六、接入 GitLab CI

1. 在被审核项目的 `.gitlab-ci.yml` 中 include 模板:
   ```yaml
   include:
     - project: 'devops/ocr-server'        # 模板所在仓库
       file: '/deploy/gitlab-ci-template.yml'
   ```
   或直接把 `deploy/gitlab-ci-template.yml` 内容复制到项目 `.gitlab-ci.yml`。

2. 项目 Settings -> CI/CD -> Variables 加:
   - `OCR_SERVER_URL` = `http://<server-ip>:8000`

3. 项目 Settings -> Merge Requests 勾选:
   - ✅ **Pipelines must succeed**(关键:pipeline 失败阻止 merge)

4. 提交一个 MR 验证:CI job 触发 -> MR 上出现 inline 评论 + 汇总 -> 有 high 问题则 merge 被阻。

## 七、运维

```bash
# 查日志
journalctl -u ocr -f

# 重启
sudo systemctl restart ocr

# 清仓库缓存(空间不足时,bare repo 会累积)
sudo rm -rf /var/ocr/repos/*.git /var/ocr/work/*
```

## 八、关键参数调优

| 变量 | 默认 | 说明 |
|---|---|---|
| `MAX_CONCURRENT_REVIEWS` | 2 | 同时审核的 MR 数,调大需确保 LLM 端点扛得住 |
| `OCR_CONCURRENCY` | 8 | ocr 单次审核的文件级并发 |
| `OCR_TIMEOUT_MIN` | 10 | ocr 单任务超时 |
| `BLOCKING_SEVERITIES` | critical,high | 命中即 reject,可调整为 `critical` 单独卡 |

## 九、已知问题

- ocr 过滤步骤偶发 `Review filter: failed to parse LLM response` warning(glm 返回非 JSON) -> 不影响结果,仅质量略降,见 `warnings[]`
- 单次审核 3-7 分钟、数十万 token,大 MR 注意 LLM 配额
- ocr 自动更新可能引入坏版本 -> 已设 `OCR_NO_UPDATE=true`,固定 `@1.7.6`
