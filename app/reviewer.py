"""
审核核心:调用 ocr review、解析 JSON、判定 approve/reject、生成评论 markdown。

ocr 命令:
  ocr review --repo <worktree> --from origin/<target> --to <source_sha> \
             --format json --audience agent \
             --concurrency <n> --timeout <min>

输出 JSON 结构(实测):
  {
    "status": "success",
    "message": "...",
    "summary": {"files_reviewed", "comments", "total_tokens", "elapsed"},
    "comments": [
      {"path","start_line","end_line","content","existing_code",
       "suggestion_code","category","severity"}
    ],
    "warnings": [...],
    "session_id": "..."
  }
"""
import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import config


@dataclass
class ReviewResult:
    approve: bool
    status: str
    summary_text: str           # 给 CI 的一行汇总
    reject_reason: str          # approve=False 时的原因
    stats: dict = field(default_factory=dict)
    comments: list[dict] = field(default_factory=list)
    warnings: list = field(default_factory=list)
    session_id: str = ""
    markdown_summary: str = ""  # 回写 MR 的汇总 note(markdown)


class ReviewError(Exception):
    pass


def _ocr_env() -> dict:
    """构造 ocr 子进程环境:用环境变量驱动 LLM 配置 + 关闭自动更新。"""
    env = dict(os.environ)
    env["OCR_LLM_URL"] = config.OCR_LLM_URL
    env["OCR_LLM_TOKEN"] = config.OCR_LLM_TOKEN
    env["OCR_LLM_MODEL"] = config.OCR_LLM_MODEL
    if config.OCR_USE_ANTHROPIC:
        env["OCR_USE_ANTHROPIC"] = "true"
    if config.OCR_NO_UPDATE:
        env["OCR_NO_UPDATE"] = "1"
    return env


def run_ocr(repo_path: Path, target_branch: str, source_sha: str) -> dict:
    """跑一次 ocr review,返回解析后的 JSON dict。失败抛 ReviewError。"""
    # target_branch 可能是分支名也可能是 commit SHA
    # 如果传的是 SHA（40 位 hex），不加 origin/ 前缀
    _is_sha = len(target_branch) == 40 and all(c in "0123456789abcdef" for c in target_branch.lower())
    _from_ref = target_branch if _is_sha else f"origin/{target_branch}"

    cmd = [
        config.OCR_BIN,
        "review",
        "--repo", str(repo_path),
        "--from", _from_ref,
        "--to", source_sha,
        "--format", "json",
        "--audience", "agent",
        "--concurrency", str(config.OCR_CONCURRENCY),
        "--timeout", str(config.OCR_TIMEOUT_MIN),
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=_ocr_env(),
            timeout=config.OCR_TIMEOUT_MIN * 60 + 120,  # 比 ocr 自身 timeout 多 2 分钟兜底
        )
    except subprocess.TimeoutExpired as e:
        raise ReviewError(f"ocr 执行超时: {e}") from e
    except FileNotFoundError as e:
        raise ReviewError(
            f"找不到 ocr 可执行文件 '{config.OCR_BIN}',请检查 OCR_BIN 或 PATH: {e}"
        ) from e

    if proc.returncode != 0:
        # ocr 非零退出:把 stderr 带出来便于排查
        raise ReviewError(
            f"ocr 退出码 {proc.returncode}\nstderr: {proc.stderr.strip()}\nstdout: {proc.stdout.strip()[:2000]}"
        )

    # ocr --format json 的 stdout 即 JSON(可能前面有进度行,取最后一个 JSON 对象)
    return _parse_json_output(proc.stdout)


def _parse_json_output(stdout: str) -> dict:
    """从 ocr stdout 中提取 JSON。--audience agent 时 stdout 应为纯 JSON。"""
    stdout = stdout.strip()
    if not stdout:
        raise ReviewError("ocr stdout 为空")
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        # 兜底:找第一个 { 到最后一个 } 之间
        start = stdout.find("{")
        end = stdout.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(stdout[start : end + 1])
            except json.JSONDecodeError as e:
                raise ReviewError(f"ocr 输出无法解析为 JSON: {e}\n原始输出前 2000 字: {stdout[:2000]}") from e
        raise ReviewError(f"ocr 输出无法解析为 JSON\n原始输出前 2000 字: {stdout[:2000]}")


def _severity_rank(sev: str) -> int:
    return {"critical": 4, "high": 3, "medium": 2, "low": 1}.get((sev or "").lower(), 0)


def decide(result_json: dict) -> ReviewResult:
    """根据 ocr JSON 结果判定 approve/reject 并构造 ReviewResult。"""
    status = result_json.get("status", "")
    comments = result_json.get("comments", []) or []
    warnings = result_json.get("warnings", []) or []
    summary_obj = result_json.get("summary", {}) or {}
    session_id = result_json.get("session_id", "")
    message = result_json.get("message", "")

    # status 异常 -> 不放行(避免假绿)
    if status and status != "success":
        return ReviewResult(
            approve=False,
            status=status,
            summary_text=f"ocr 状态异常: {status}",
            reject_reason=f"ocr 审核异常(status={status}),请人工复核",
            stats=summary_obj,
            comments=comments,
            warnings=warnings,
            session_id=session_id,
            markdown_summary=_build_error_note(result_json),
        )

    # 按 severity 分组
    blocking = [c for c in comments if (c.get("severity", "") or "").lower() in config.BLOCKING_SEVERITIES]
    blocking.sort(key=lambda c: _severity_rank(c.get("severity", "")), reverse=True)

    approve = len(blocking) == 0

    # 分类统计
    by_severity: dict[str, int] = {}
    by_category: dict[str, int] = {}
    for c in comments:
        s = (c.get("severity") or "unknown").lower()
        cat = (c.get("category") or "other").lower()
        by_severity[s] = by_severity.get(s, 0) + 1
        by_category[cat] = by_category.get(cat, 0) + 1

    stats_str = (
        f"files={summary_obj.get('files_reviewed', '?')}, "
        f"comments={summary_obj.get('comments', len(comments))}, "
        f"elapsed={summary_obj.get('elapsed', '?')}, "
        f"tokens={summary_obj.get('total_tokens', '?')}"
    )

    if approve:
        summary_text = f"✅ 审核通过({len(comments)} 条非阻塞建议) | {stats_str}"
        reject_reason = ""
    else:
        sev_part = ", ".join(f"{k}:{v}" for k, v in sorted(by_severity.items(), key=lambda x: -_severity_rank(x[0])))
        summary_text = f"❌ 审核未通过({len(blocking)} 条 critical/high) | {sev_part} | {stats_str}"
        reject_reason = f"发现 {len(blocking)} 条 critical/high 问题,需修复后方可 merge"

    return ReviewResult(
        approve=approve,
        status=status or "success",
        summary_text=summary_text,
        reject_reason=reject_reason,
        stats=summary_obj,
        comments=comments,
        warnings=warnings,
        session_id=session_id,
        markdown_summary=_build_summary_note(result_json, approve, by_severity, by_category, blocking),
    )


def _build_summary_note(
    result_json: dict,
    approve: bool,
    by_severity: dict,
    by_category: dict,
    blocking: list,
) -> str:
    """生成回写 MR 的汇总 note(markdown)。"""
    summary = result_json.get("summary", {}) or {}
    session_id = result_json.get("session_id", "")
    total = sum(by_severity.values())

    icon = "✅" if approve else "❌"
    verdict = "审核通过,可 merge" if approve else "审核未通过,存在 critical/high 问题,请修复"

    lines = [f"## {icon} OpenCodeReview 自动审核", "", f"**结论:{verdict}**", ""]

    if total == 0:
        lines.append(result_json.get("message", "未发现问题。") + "")
    else:
        sev_order = ["critical", "high", "medium", "low", "unknown"]
        sev_line = "  ".join(f"`{s}`: {by_severity.get(s, 0)}" for s in sev_order if by_severity.get(s))
        lines.append(f"**问题统计(共 {total} 条)**:{sev_line}")
        if by_category:
            cat_line = "  ".join(f"`{k}`: {v}" for k, v in sorted(by_category.items(), key=lambda x: -x[1]))
            lines.append(f"**类别**:{cat_line}")

    lines += [
        "",
        f"| 指标 | 值 |",
        f"|---|---|",
        f"| 审查文件数 | {summary.get('files_reviewed', '?')} |",
        f"| 耗时 | {summary.get('elapsed', '?')} |",
        f"| Token | {summary.get('total_tokens', '?')} |",
    ]
    if blocking:
        lines += ["", f"**需修复的 critical/high 问题({len(blocking)} 条,详见上方 inline 评论)**:"]
        for c in blocking:
            path = c.get("path", "?")
            line = c.get("start_line") or c.get("end_line") or "?"
            sev = c.get("severity", "?")
            cat = c.get("category", "?")
            content = (c.get("content", "") or "").strip().split("\n")[0][:120]
            lines.append(f"- [`{path}:{line}`] [{sev}/{cat}] {content}")

    if result_json.get("warnings"):
        lines += ["", f"⚠️ 审核过程产生 {len(result_json['warnings'])} 条 warning(如 LLM 响应解析降级),质量可能略降。"]
    if session_id:
        lines += ["", f"<sub>session: `{session_id}`</sub>"]
    return "\n".join(lines)


def _build_error_note(result_json: dict) -> str:
    return (
        "## ⚠️ OpenCodeReview 审核异常\n\n"
        f"ocr 返回异常状态,未放行,请人工复核。\n\n"
        f"```\n{json.dumps(result_json, ensure_ascii=False, indent=2)[:2000]}\n```"
    )


def format_inline_comment(comment: dict) -> str:
    """把单条 comment 格式化为 inline 评论 markdown(含 suggestion)。借鉴官方脚本。"""
    body = comment.get("content", "")
    sev = (comment.get("severity") or "").lower()
    cat = (comment.get("category") or "").lower()
    badge = f"**[{sev}/{cat}]** " if sev or cat else ""
    body = f"{badge}{body}"

    existing = comment.get("existing_code", "")
    suggestion = comment.get("suggestion_code", "")
    if suggestion and existing:
        body += "\n\n**建议修改:**\n"
        body += f"```suggestion:-0+0\n{suggestion}\n```"
    return body
