"""种子管理业务规则：URL模板校验、XPath合法性检查、跨模块绑定。

种子管理读取采集源接入校验记录（采集源管理生成的同源数据），按校验结果决定
种子继续可用还是进入待修复状态：
- 校验通过（确认接入）：种子清除待修复标记，恢复可调度；
- 校验失败（拒绝确认/来源异常）：种子暂停调度进入待修复，保留历史绑定与
  人工修复入口，种子清单保留入口并标注待修复原因；
- 失败来源不能生成可调度种子，种子自身状态（草稿/待校验/已生效/已过期）
  不被校验结果改写，原种子保持可用、引用保留。
"""
from __future__ import annotations

import re
from typing import Any

from app.store import store

MODULE = "seed_config"
LIST_FIELDS = ["种子编号", "目标URL模式", "列表页XPath", "详情页XPath", "分页参数", "排序字段", "去重键", "种子优先级"]
REQUIRED_FIELDS = ["种子编号", "目标URL模式", "列表页XPath"]
STATUS_ORDER = ["草稿", "待校验", "已生效", "已过期"]
ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "标记过期": "已过期"}
NEGATIVE_ACTIONS = []
# 校验失败判定：种子进入待修复；确认接入：种子恢复可用
REPAIR_DECISIONS = ("拒绝确认", "来源异常")


def _validate_xpath(xpath: str) -> tuple[bool, str]:
    """基础XPath语法校验：检查是否以/或//开头，括号是否匹配。"""
    if not xpath or not isinstance(xpath, str):
        return False, "XPath为空"
    stripped = xpath.strip()
    if not stripped.startswith(("/", "//")):
        return False, "XPath必须以/或//开头"
    if stripped.count("(") != stripped.count(")"):
        return False, "XPath括号不匹配"
    return True, "语法合法"


def _validate_url_pattern(pattern: str) -> tuple[bool, str]:
    """校验URL模式是否包含通配符或分页参数占位。"""
    if not pattern or not isinstance(pattern, str):
        return False, "URL模式为空"
    if not pattern.startswith(("http://", "https://")):
        return False, "URL模式必须以http://或https://开头"
    # 检查是否包含至少一个可替换参数（{page}、{id}、{}等）
    has_param = bool(re.search(r"\{[^}]*\}", pattern))
    return True, "包含分页参数" if has_param else "固定URL"


class SeedConfigService:
    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        for field, expected in (filters or {}).items():
            rows = [row for row in rows if expected.casefold() in str(row.get(field, "")).casefold()]
        if keyword:
            rows = [row for row in rows if keyword.casefold() in str(row.get("种子编号", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        enriched = []
        for row in rows[start:start + size]:
            d = dict(row)
            d["_parse_rule_count"] = len(store.get_relations("seed_to_parse_rule", source_module=MODULE, source_id=d["id"]))
            d["_cursor_count"] = len(store.get_relations("seed_to_cursor", source_module=MODULE, source_id=d["id"]))
            self._attach_scheduling_info(d)
            enriched.append(d)
        return enriched, total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        d = dict(entry)
        d["_parse_rule_count"] = len(store.get_relations("seed_to_parse_rule", source_module=MODULE, source_id=entry_id))
        d["_cursor_count"] = len(store.get_relations("seed_to_cursor", source_module=MODULE, source_id=entry_id))
        self._attach_scheduling_info(d)
        d["_source_validation"] = self._source_validation_summary(entry_id)
        return d

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in LIST_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        # 自动校验
        url_ok, url_msg = _validate_url_pattern(str(entry.get("目标URL模式") or ""))
        xpath_ok, xpath_msg = _validate_xpath(str(entry.get("列表页XPath") or ""))
        entry["_validation"] = {"url_ok": url_ok, "url_msg": url_msg, "xpath_ok": xpath_ok, "xpath_msg": xpath_msg}
        rows.append(entry)
        # 如果指定了关联采集源，建立关系
        source_id = values.get("关联采集源ID")
        if source_id:
            try:
                sid = int(source_id)
                store.add_relation("source_to_seed", "crawl_source", sid, MODULE, entry["id"])
            except (ValueError, TypeError):
                pass
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"采集种子 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于种子管理可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        if action == "提交校验":
            url_ok, url_msg = _validate_url_pattern(str(entry.get("目标URL模式") or ""))
            xpath_ok, xpath_msg = _validate_xpath(str(entry.get("列表页XPath") or ""))
            entry["_validation"] = {"url_ok": url_ok, "url_msg": url_msg, "xpath_ok": xpath_ok, "xpath_msg": xpath_msg}
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"采集种子已{action}"

    def get_linked_parse_rules(self, entry_id: int) -> list[dict[str, Any]]:
        rels = store.get_relations("seed_to_parse_rule", source_module=MODULE, source_id=entry_id)
        rules = []
        for rel in rels:
            r = store.find("parse_rule", rel["target_id"])
            if r:
                rules.append(dict(r))
        return rules

    def bind_source(self, entry_id: int, source_id: int) -> dict[str, Any]:
        """将种子绑定到采集源。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return {"ok": False, "error": "种子不存在"}
        store.add_relation("source_to_seed", "crawl_source", source_id, MODULE, entry_id)
        return {"ok": True, "seed_id": entry_id, "source_id": source_id}

    # ------------------------------------------------------------------
    # 采集源接入校验记录：读取同源记录并决定种子可用/待修复
    # ------------------------------------------------------------------
    def apply_source_validation_record(self, record: dict[str, Any]) -> list[dict[str, Any]]:
        """读取采集源接入校验记录，按校验结果回写绑定种子的修复状态。

        返回写入校验记录的种子状态数据；原种子引用保留，不解除绑定。
        """
        source_id = int(record.get("source_id") or 0)
        decision = str(record.get("decision") or "待确认")
        rels = store.get_relations("source_to_seed", source_module="crawl_source", source_id=source_id)
        snapshots: list[dict[str, Any]] = []
        for rel in rels:
            seed = store.find(MODULE, rel["target_id"])
            if seed is None:
                continue
            if decision in REPAIR_DECISIONS:
                # 保留接入记录口径：种子暂停调度进入待修复，标注原因，保留入口
                seed["repair_state"] = "待修复"
                seed["repair_reason"] = (
                    f"采集源{record.get('源标识')}接入校验未通过：{record.get('message')}"
                )
                seed["abnormal"] = True
            elif decision == "确认接入" and record.get("reachable"):
                # 校验通过：种子继续可用，清除待修复标记
                seed["repair_state"] = ""
                seed["repair_reason"] = ""
                seed["abnormal"] = False
            snapshots.append(self._seed_snapshot(seed))
        return snapshots

    def _seed_snapshot(self, seed: dict[str, Any]) -> dict[str, Any]:
        """校验记录中的种子状态数据：状态、修复标记与调度入口。"""
        return {
            "seed_id": int(seed["id"]),
            "种子编号": seed.get("种子编号"),
            "status": seed.get("status"),
            "repair_state": str(seed.get("repair_state") or ""),
            "repair_reason": str(seed.get("repair_reason") or ""),
            "schedulable": self._compute_schedulable(seed),
        }

    def _attach_scheduling_info(self, d: dict[str, Any]) -> None:
        """列表/详情同源展示：修复状态、待修复原因与是否可调度。"""
        d["_repair_state"] = str(d.get("repair_state") or "")
        d["_repair_reason"] = str(d.get("repair_reason") or "")
        d["_schedulable"] = self._compute_schedulable(d)

    def _compute_schedulable(self, seed: dict[str, Any]) -> bool:
        """失败来源不能生成可调度种子；待修复种子暂停调度。"""
        if seed.get("status") != "已生效":
            return False
        if seed.get("repair_state"):
            return False
        rels = store.get_relations("source_to_seed", target_module=MODULE, target_id=int(seed["id"]))
        if not rels:
            return True
        records = store.pipeline_state("source_access_validation_records") or {}
        for rel in rels:
            record = records.get(str(rel["source_id"]))
            if record and record.get("decision") in REPAIR_DECISIONS:
                return False
            source = store.find("crawl_source", rel["source_id"])
            if source is None or source.get("status") != "已接入":
                return False
            if record and record.get("decision") not in ("确认接入",):
                return False
        return True

    def _source_validation_summary(self, entry_id: int) -> dict[str, Any] | None:
        """详情面板展示绑定来源的接入校验结果（同源读取，避免旧值）。"""
        rels = store.get_relations("source_to_seed", target_module=MODULE, target_id=entry_id)
        if not rels:
            return None
        records = store.pipeline_state("source_access_validation_records") or {}
        record = records.get(str(rels[-1]["source_id"]))
        if record is None:
            return None
        return {
            "record_id": record.get("record_id"),
            "source_id": record.get("source_id"),
            "源标识": record.get("源标识"),
            "decision": record.get("decision"),
            "record_status": record.get("record_status"),
            "reachable": record.get("reachable"),
            "message": record.get("message"),
            "version": record.get("version"),
        }
