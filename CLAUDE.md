# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**OCR Server** - A FastAPI-based HTTP service that wraps Alibaba's OpenCodeReview (`ocr`) CLI tool to provide automated GitLab MR code review. The service runs LLM-powered code reviews on MR diffs, posts inline comments back to GitLab, and blocks merges based on configured severity thresholds.

## Architecture

```
GitLab MR ──CI trigger──> POST /review ──> OCR Server ──> ocr review (LLM)
                                                  │
                                                  ├── git fetch (bare repo cache)
                                                  └── post inline comments + summary note

CI Job: approve=False → exit 1 → pipeline fails → blocks merge
```

**Key Components** (`app/`):
- **`main.py`** - FastAPI entrypoint, `/review` endpoint, thread pool concurrency control, review orchestration
- **`reviewer.py`** - Core logic: calls `ocr review`, parses JSON output, decides approve/reject, formats comments
- **`repo_cache.py`** - Git bare repo caching + worktree management (incremental fetch, seconds instead of full clone)
- **`gitlab_client.py`** - GitLab API client with rate limiting / retries, discussions (inline comments) and notes (summary)
- **`config.py`** - All config via environment variables (no hardcoded secrets)

## Commonly Used Commands

### Docker Deployment (Recommended)

```bash
# Build and start
cp .env.example .env
# Edit .env with LLM/GitLab config
docker-compose up -d

# View logs
docker-compose logs -f

# Health check
curl http://localhost:8000/health

# Clean repo cache (when disk is full)
docker exec ocr-server rm -rf /var/ocr/repos/*.git

# Rebuild after code changes
docker-compose build
docker-compose up -d
```

See `docs/Docker部署.md` for detailed Docker operations.

### Local Development (Native)

```bash
# Install dependencies
python -m venv .venv
.venv/bin/pip install -r requirements.txt

# Run server locally (loads .env)
cp .env.example .env
# Edit .env with your values
.venv/bin/python -m app.main
# or
.venv/bin/uvicorn app.main:app --reload

# Health check
curl http://localhost:8000/health

# Test review endpoint
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
```

### Deployment (Linux)

See `deploy/README.md` for full instructions. Key commands:

```bash
# ocr CLI installation (required, latest version)
sudo npm install -g @alibaba-group/open-code-review@latest

# Verify ocr binary (critical: ~45MB, not truncated)
ls -la $(npm root -g)/@alibaba-group/open-code-review/node_modules/@alibaba-group/ocr-linux-x64/bin/opencodereview

# Test LLM connection
ocr llm test

# systemd operations
sudo systemctl status ocr
sudo systemctl restart ocr
journalctl -u ocr -f

# Clear repo cache (accumulates over time)
sudo rm -rf /var/ocr/repos/*.git /var/ocr/work/*
```

## Key Configuration

All configuration via environment variables (see `.env.example`):

| Variable | Default | Purpose |
|---|---|---|
| `MAX_CONCURRENT_REVIEWS` | `2` | Concurrent MR reviews (LLM capacity limited) |
| `OCR_CONCURRENCY` | `8` | File-level concurrency within one ocr run |
| `BLOCKING_SEVERITIES` | `critical,high` | Severities that block merge (reject) |
| `OCR_LLM_URL` | `http://your-llm-endpoint.example.com:8320` | Internal LLM endpoint (Anthropic-compatible) |
| `GITLAB_TOKEN` | - | Project/Group Access Token for posting comments |
| `OCR_NO_UPDATE` | `true` | Disable ocr auto-update (prevents broken versions) |
| `REVIEW_LANGUAGE` | `Chinese` | ocr review output language (problem descriptions & summary); empty = ocr default (English) |

## API Endpoints

- **`GET /health`** - Health check, returns ocr_bin and llm_url status
- **`POST /review`** - Submit MR for review (synchronous, ~3-7 min), returns `{approve, summary, comments, ...}`

## Code Patterns

- **Error Handling**: Custom exceptions `RepoError`, `ReviewError`, `GitLabError`
- **Concurrency**: `ThreadPoolExecutor` limits concurrent MR reviews
- **Git Strategy**: Bare repos for incremental fetch + git worktrees for review workspaces
- **Retries**: GitLab API has exponential backoff + jitter for rate limits / 5xx
- **Comment Formatting**: Uses GitLab suggestion blocks (` ```suggestion:-0+0 `)

## Production Notes

- Single review consumes **hundreds of thousands of tokens** - monitor LLM quota
- Typical review time: **3-7 minutes** per MR
- `OCR_NO_UPDATE=true` is critical - ocr auto-update has caused binary truncation issues
- Repo cache at `/var/ocr/repos` accumulates over time, periodic cleanup recommended
- GitLab **Pipelines must succeed** must be enabled for the blocking mechanism to work
- ocr warnings like "failed to parse LLM response" are non-fatal (quality slightly degraded)
