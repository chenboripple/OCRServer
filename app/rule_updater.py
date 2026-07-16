"""
审核规则更新器:每次 code review 前,从飞书电子表格读取自定义规则,
更新到 ocr CLI 的配置文件 (~/.opencodereview/config.json) 中。

表格格式约定:
  第一列:任意内容(如序号)
  第二列:审核规则文本(每行一条规则)
"""
import json
import logging
from pathlib import Path

from . import config
from .feishu_client import FeishuError, _get_client

log = logging.getLogger("ocr-server.rule-updater")


def ensure_ocr_config_dir() -> Path:
    """确保 ~/.opencodereview/ 目录存在并返回 config.json 路径。"""
    cfg_path = config.OCR_CONFIG_PATH
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    return cfg_path


def _load_existing_config(cfg_path: Path) -> dict:
    """加载已有的 ocr config.json，如果文件不存在或损坏则返回空 dict。"""
    if cfg_path.exists():
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            log.warning("ocr 配置文件 %s 读取失败,将重建: %s", cfg_path, e)
    return {}


def _write_config(cfg_path: Path, cfg: dict):
    """安全写回 config.json（原子写入）。"""
    tmp = cfg_path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    tmp.replace(cfg_path)
    log.info("ocr 配置文件已更新: %s", cfg_path)


def _extract_rules_from_rows(rows: list[list[str]]) -> dict:
    """
    从电子表格行中提取第二列作为规则列表。

    参数:
        rows: 行列表,每行为单元格值列表,如 [["1", "禁止使用 System.out.println"], ...]

    返回:
        ocr config 格式的 dict,如 {"customRules": ["禁止...", "所有异常..."]}
    """
    rules_list = []
    for row in rows:
        # 取第二列(下标1),跳过空行或只有一列的行
        if len(row) >= 2:
            rule = (row[1] or "").strip()
            if rule:
                rules_list.append(rule)

    result: dict = {}
    if rules_list:
        result["customRules"] = rules_list
        log.info("从飞书电子表格第二列提取了 %d 条自定义规则", len(rules_list))
    else:
        log.info("飞书电子表格第二列为空,未提取到规则")

    return result


def update_rules_from_feishu() -> None:
    """
    主入口:从飞书电子表格读取第二列规则 → 更新到 ocr config.json。
    如果飞书未启用/读取失败/表格为空,则静默跳过（不阻断审核流程）。
    """
    if not config.FEISHU_ENABLED:
        log.debug("飞书集成未启用,跳过规则更新")
        return

    if not config.FEISHU_SPREADSHEET_TOKEN:
        log.warning("FEISHU_SPREADSHEET_TOKEN 未配置,跳过规则更新")
        return

    client = _get_client()
    if not client:
        log.warning("飞书客户端不可用,跳过规则更新")
        return

    try:
        rows = client.get_sheet_values(config.FEISHU_SPREADSHEET_TOKEN, config.FEISHU_SHEET_RANGE)
        if not rows:
            log.info("飞书电子表格内容为空,跳过规则更新")
            return

        custom = _extract_rules_from_rows(rows)
        if not custom:
            log.info("飞书电子表格中未提取到规则,跳过更新")
            return

        cfg_path = ensure_ocr_config_dir()
        existing = _load_existing_config(cfg_path)

        # 合并:保留已有配置,更新 ruleConfig 字段
        existing["ruleConfig"] = custom
        _write_config(cfg_path, existing)

        log.info("✅ 飞书规则已同步到 %s (%d 条规则)", cfg_path, len(custom.get("customRules", [])))

    except FeishuError as e:
        log.warning("读取飞书电子表格失败(不影响审核流程): %s", e)
    except Exception as e:
        log.warning("更新 ocr 配置时发生意外错误(不影响审核流程): %s", e)


def apply_review_language() -> None:
    """
    把 REVIEW_LANGUAGE 写入 ocr config.json 顶层 language 字段。

    ocr 用该字段驱动 LLM 用指定语言输出问题描述与总结(默认 English)。
    每次审核前调用,与飞书规则更新共用同一份 config.json。
    未配置(空串)则不写,沿用 ocr 默认;已是目标值则跳过,避免无谓写入。
    """
    lang = (config.REVIEW_LANGUAGE or "").strip()
    if not lang:
        log.debug("REVIEW_LANGUAGE 未配置,跳过语言设置")
        return

    cfg_path = ensure_ocr_config_dir()
    try:
        existing = _load_existing_config(cfg_path)
        if existing.get("language") == lang:
            log.debug("ocr 配置 language 已是 %s,跳过", lang)
            return
        existing["language"] = lang
        _write_config(cfg_path, existing)
        log.info("✅ ocr 审核输出语言已设为: %s", lang)
    except Exception as e:
        log.warning("写入 ocr 输出语言失败(不影响审核流程): %s", e)