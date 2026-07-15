# Docker 快速启动指南

## ✨ 核心特性

- **ocr CLI 自动安装检测**: 启动时自动检查，如未安装或二进制损坏则自动安装/重装
- **非 root 用户运行**: 安全加固，uid 1000
- **二进制完整性验证**: 防止 npm 下载截断（历史踩坑点）
- **运行时配置注入**: 所有敏感信息不打包进镜像

## 前置条件

- Docker 20.10+
- Docker Compose 2.0+
- LLM 端点网络可达 (Anthropic 兼容协议)
- GitLab Project/Group Access Token (api scope)

## 快速启动

### 1. 配置环境变量

```bash
cp .env.example .env
```

编辑 `.env`，填入以下必填项：

```bash
# LLM 配置
OCR_LLM_URL=http://your-llm-endpoint:port
OCR_LLM_TOKEN=your-llm-token
OCR_LLM_MODEL=GLM-AUTO

# GitLab 配置
GITLAB_URL=https://gitlab.your-company.com
GITLAB_TOKEN=your-gitlab-project-access-token
```

### 2. 启动服务

```bash
# 构建镜像并启动
docker-compose up -d

# 查看构建日志
docker-compose logs -f

# 查看容器状态
docker-compose ps
```

### 3. 验证服务

```bash
# 健康检查
curl http://localhost:8000/health

# 预期输出:
# {"status":"ok","ocr_bin":"ocr","llm_url":"http://..."}
```

### 4. 测试审核功能

使用公开仓库测试审核流程：

```bash
curl -X POST http://localhost:8000/review \
  -H "Content-Type: application/json" \
  -d '{
    "project_id": "test-docker",
    "project_url": "https://github.com/python/cpython.git",
    "source_branch": "main",
    "target_branch": "3.11",
    "mr_iid": "1",
    "commit_sha": ""
  }'
```

## 常用运维命令

```bash
# 查看日志
docker-compose logs -f
docker logs ocr-server -f --tail 100

# 重启服务
docker-compose restart

# 停止服务
docker-compose stop

# 停止并删除容器 (保留 volume 数据)
docker-compose down

# 停止并删除所有 (含 volume，清理缓存)
docker-compose down -v

# 重新构建镜像 (代码更新后)
docker-compose build
docker-compose up -d

# 进入容器调试
docker exec -it ocr-server bash

# 清理仓库缓存 (空间不足时)
docker exec ocr-server rm -rf /var/ocr/repos/*.git

# 查看资源使用
docker stats ocr-server
```

## 配置调整

### 修改并发数

编辑 `docker-compose.yml`：

```yaml
environment:
  - MAX_CONCURRENT_REVIEWS=4    # 同时处理的 MR 数
  - OCR_CONCURRENCY=16          # 单任务文件并发
```

### 修改卡点策略

```yaml
environment:
  - BLOCKING_SEVERITIES=critical      # 只有 critical 阻止 merge
  # 或: critical,high,medium (所有问题都阻止)
```

### 资源限制调整

```yaml
deploy:
  resources:
    limits:
      cpus: '8'
      memory: 16G
    reservations:
      cpus: '4'
      memory: 8G
```

## 网络问题排查

### GitLab 无法访问

```bash
# 检查容器内网络连通性
docker exec ocr-server curl -I https://gitlab.your-company.com

# 如使用内网 GitLab，考虑使用 host 网络模式
# 编辑 docker-compose.yml，取消注释:
# network_mode: "host"
```

### LLM 端点无法访问

```bash
# 测试 LLM 连通性
docker exec ocr-server ocr llm test
```

## GitLab CI 集成

参考 `deploy/gitlab-ci-template.yml`，在项目 `.gitlab-ci.yml` 中：

```yaml
include:
  - project: 'devops/ocr-server'
    file: '/deploy/gitlab-ci-template.yml'

variables:
  OCR_SERVER_URL: http://ocr-server-host:8000
```

## 安全建议

1. **端口保护**: 确保 8000 端口仅对 GitLab Runner IP 开放
2. **HTTPS**: 生产环境建议配置 Nginx 反向代理 + SSL
3. **Secrets**: Docker Swarm 环境使用 Docker Secrets 管理 token
4. **非 root 用户**: 镜像已配置使用 uid 1000 的非 root 用户运行
