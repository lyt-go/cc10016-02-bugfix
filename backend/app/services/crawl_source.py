"""采集源管理业务规则：状态流转、连接校验、接入校验记录生成与跨模块回写。

采集源接入校验记录（主处理模块统一口径）：
- 采集源管理是唯一生成校验记录的模块，记录按源 id 同源存储在
  ``source_access_validation_records``，重复校验原地更新、版本递增，
  不会追加互相矛盾的确认记录。
- 来源确认必须以真实连通结果为准：确认接入前先做连接校验，
  不可达或协议不支持的来源会被拒绝确认，来源保持「待校验」。
- 健康检查失败时统一采用“保留接入记录”口径：不撤销接入、不删除原种子引用，
  校验记录显示「来源异常」/「拒绝确认」，由种子管理读取记录后把种子标记为
  待修复（暂停调度但保留历史绑定与人工修复入口），避免错误关闭来源让后续
  采集批次失去入口。
- 校验记录状态机按 ``RECORD_STATUS_ORDER`` 顺序逐个流转，每次校验只推进一步。
"""
from __future__ import annotations

import urllib.parse
from datetime import datetime, timezone
from typing import Any

from app.store import store

MODULE = "crawl_source"
LIST_FIELDS = ["源标识", "源名称", "协议类型", "基础URL", "认证方式", "地区归属", "标签分类", "最近采集时间"]
REQUIRED_FIELDS = ["源标识", "源名称", "协议类型"]
STATUS_ORDER = ["草稿", "待校验", "已接入", "已停用"]
ACTION_RULES = {"提交校验": "待校验", "确认接入": "已接入", "停用源": "已停用"}
NEGATIVE_ACTIONS = ["停用源"]
# 状态机按顺序逐个流转：每个动作只允许从紧邻的前置状态发起
STATUS_FLOW = {"提交校验": "草稿", "确认接入": "待校验", "停用源": "已接入"}
# 待接入（尚未完成接入确认）的来源状态，批量校验只处理这些
PENDING_ACCESS_STATUSES = ("草稿", "待校验")

# 采集源接入校验记录状态机：待确认 -> 已确认 / 已拒绝，逐个流转
RECORD_STATUS_ORDER = ["待确认", "已确认", "已拒绝"]
RECORD_TRANSITIONS = {
    "待确认": {"已确认", "已拒绝"},
    "已确认": {"已确认"},          # 保留接入记录：来源异常时记录保持已确认，仅判定变更
    "已拒绝": {"待确认", "已确认"},  # 来源恢复可达后回到待确认，或修复后重新确认接入
}
# 判定 -> 记录状态 的映射（来源异常保留接入记录，记录状态保持已确认）
DECISION_TO_RECORD_STATUS = {
    "待确认": "待确认",
    "确认接入": "已确认",
    "拒绝确认": "已拒绝",
    "来源异常": "已确认",
}


def _validate_url(url: str) -> tuple[bool, str, float]:
    """模拟连接校验：解析URL并返回可达性、协议类型、模拟延迟。"""
    if not url or not isinstance(url, str):
        return False, "URL为空", 0.0
    try:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False, f"不支持的协议: {parsed.scheme}", 0.0
        if not parsed.netloc:
            return False, "URL缺少主机名", 0.0
        # 模拟延迟计算：基于主机名长度+固定开销
        latency = 0.05 + (len(parsed.netloc) % 7) * 0.03
        return True, "连接正常", round(latency, 3)
    except Exception as exc:
        return False, f"URL解析异常: {exc}", 0.0


def _check_source_health(entry: dict[str, Any]) -> dict[str, Any]:
    """校验采集源健康状态并写回缓存。"""
    url = str(entry.get("基础URL") or "")
    reachable, message, latency = _validate_url(url)
    cache = store.pipeline_state("source_validation_cache") or {}
    cache[str(entry["id"])] = {
        "reachable": reachable,
        "message": message,
        "latency": latency,
        "checked_at": "2026-11-16T00:00:00Z",
    }
    store.set_pipeline_state("source_validation_cache", cache)
    entry["_health"] = cache[str(entry["id"])]
    return entry


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class CrawlSourceService:
    # ------------------------------------------------------------------
    # 列表与明细
    # ------------------------------------------------------------------
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
            rows = [row for row in rows if keyword.casefold() in str(row.get("源标识", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        # 补充健康缓存与接入校验记录（同源读取，保证页面不显示旧值）
        enriched = []
        cache = store.pipeline_state("source_validation_cache") or {}
        records = self._records()
        for row in rows[start:start + size]:
            d = dict(row)
            d["_health"] = cache.get(str(d["id"]), {"reachable": None, "message": "未校验", "latency": 0.0})
            # 计算关联种子数
            d["_seed_count"] = len(store.get_relations("source_to_seed", source_module=MODULE, source_id=d["id"]))
            record = records.get(str(d["id"]))
            d["_validation_decision"] = record["decision"] if record else "未校验"
            d["_validation_record_status"] = record["record_status"] if record else ""
            enriched.append(d)
        return enriched, total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        d = dict(entry)
        d = _check_source_health(d)
        d["_seed_count"] = len(store.get_relations("source_to_seed", source_module=MODULE, source_id=entry_id))
        d["_rate_limit_id"] = None
        rels = store.get_relations("source_to_rate_limit", source_module=MODULE, source_id=entry_id)
        if rels:
            d["_rate_limit_id"] = rels[-1]["target_id"]
        d["_validation_record"] = self.get_validation_record(entry_id)
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
        # 自动连接校验
        _check_source_health(entry)
        rows.append(entry)
        return entry, []

    # ------------------------------------------------------------------
    # 状态动作：按顺序逐个流转，确认接入以真实连通结果为准
    # ------------------------------------------------------------------
    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"采集源 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于采集源管理可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        # 状态机按顺序逐个流转：不允许跳级或回退
        expected_from = STATUS_FLOW[action]
        if entry.get("status") != expected_from:
            return None, (
                f"当前状态「{entry.get('status')}」不允许执行「{action}」，"
                f"采集源需按 {'→'.join(STATUS_ORDER)} 顺序逐个流转"
            )
        if action == "提交校验":
            _check_source_health(entry)
            self._upsert_validation_record(entry, trigger="提交校验")
        if action == "确认接入":
            # 先校验连通性再决定状态流转，采集源管理优先处理先后需判定
            _check_source_health(entry)
            health = entry["_health"]
            if not health["reachable"]:
                # 拒绝确认：来源保持待校验，不关闭来源，种子由校验记录回写为待修复
                self._upsert_validation_record(entry, trigger="确认接入")
                entry["abnormal"] = True
                return None, (
                    f"接入校验未通过（{health['message']}），已拒绝确认接入；"
                    f"来源保持「待校验」，关联种子已按校验记录标记待修复"
                )
            # 连通正常：先推进来源状态，再生成校验记录并回写种子，保证种子调度判定读到新状态
            entry["status"] = target
            entry["pending"] = False
            entry["abnormal"] = False
            self._upsert_validation_record(entry, trigger="确认接入")
            return entry, f"采集源已{action}（连通正常，延迟 {health['latency']}s）"
        entry["status"] = target
        entry["pending"] = target in PENDING_ACCESS_STATUSES
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"采集源已{action}"

    # ------------------------------------------------------------------
    # 连接校验与采集源接入校验记录
    # ------------------------------------------------------------------
    def validate_connection(self, entry_id: int) -> dict[str, Any]:
        """显式触发连接校验，生成/更新采集源接入校验记录并返回结果。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return {"ok": False, "error": "采集源不存在"}
        _check_source_health(entry)
        record = self._upsert_validation_record(entry, trigger="连接校验")
        cache = store.pipeline_state("source_validation_cache") or {}
        health = cache.get(str(entry_id), {"ok": False, "error": "无缓存"})
        return {**health, "ok": True, "record": record}

    def validate_pending_sources(self) -> list[dict[str, Any]]:
        """读取待接入采集源记录并逐个校验接入状态，生成采集源接入校验记录。"""
        pending = [row for row in store.rows(MODULE) if row.get("status") in PENDING_ACCESS_STATUSES]
        # 按源 id 排序逐个流转，保证处理先后可判定
        pending.sort(key=lambda row: int(row.get("id", 0)))
        records = []
        for entry in pending:
            _check_source_health(entry)
            records.append(self._upsert_validation_record(entry, trigger="批量校验"))
        return records

    def get_validation_record(self, entry_id: int) -> dict[str, Any] | None:
        """读取单个采集源的接入校验记录；未生成时返回 None。"""
        record = self._records().get(str(entry_id))
        return dict(record) if record else None

    def list_validation_records(self) -> list[dict[str, Any]]:
        """按源 id 顺序列出全部采集源接入校验记录。"""
        records = self._records()
        return [dict(records[key]) for key in sorted(records, key=lambda value: int(value))]

    def _records(self) -> dict[str, Any]:
        records = store.pipeline_state("source_access_validation_records")
        if records is None:
            records = {}
            store.set_pipeline_state("source_access_validation_records", records)
        return records

    def _upsert_validation_record(self, entry: dict[str, Any], *, trigger: str) -> dict[str, Any]:
        """生成或原地更新采集源接入校验记录（同源、幂等），并跨模块回写种子状态。"""
        records = self._records()
        key = str(entry["id"])
        health = dict(entry.get("_health") or {})
        record = records.get(key)
        if record is None:
            record = {
                "record_id": f"SAVR-{int(entry['id']):03d}",
                "source_id": int(entry["id"]),
                "源标识": entry.get("源标识"),
                "源名称": entry.get("源名称"),
                "协议类型": entry.get("协议类型"),
                "基础URL": entry.get("基础URL"),
                "record_status": RECORD_STATUS_ORDER[0],
                "decision": "待确认",
                "version": 0,
                "history": [],
                "seed_statuses": [],
            }
            records[key] = record
        # 以真实连通结果为准更新记录
        record["reachable"] = bool(health.get("reachable"))
        record["message"] = health.get("message")
        record["latency"] = health.get("latency")
        record["checked_at"] = health.get("checked_at")
        record["version"] = int(record.get("version", 0)) + 1
        record["updated_at"] = _utc_now()
        # 判定：确认接入以连通结果为准；已接入来源复查失败记为来源异常（保留接入记录）
        decision = self._decide(record, entry, trigger)
        target_status = DECISION_TO_RECORD_STATUS[decision]
        self._advance_record_status(record, target_status)
        record["decision"] = decision
        record["history"].append({
            "version": record["version"],
            "trigger": trigger,
            "decision": decision,
            "reachable": record["reachable"],
            "message": record["message"],
            "checked_at": record["checked_at"],
        })
        # 跨模块回写：种子管理读取校验记录，决定种子继续可用还是进入待修复
        record["seed_statuses"] = self._sync_seeds_from_record(record)
        return record

    def _decide(self, record: dict[str, Any], entry: dict[str, Any], trigger: str) -> str:
        reachable = bool(record.get("reachable"))
        if trigger == "确认接入":
            return "确认接入" if reachable else "拒绝确认"
        # 连接校验/提交校验/批量校验等健康探针
        was_confirmed = record.get("record_status") == "已确认" or entry.get("status") == "已接入"
        if was_confirmed:
            # 保留接入记录口径：失败不撤销接入，记录显示来源异常
            return "确认接入" if reachable else "来源异常"
        if record.get("record_status") == "已拒绝":
            # 已拒绝的来源：仍不可达时维持拒绝，不追加矛盾判定；恢复可达后回到待确认
            return "待确认" if reachable else "拒绝确认"
        return "待确认"

    def _advance_record_status(self, record: dict[str, Any], target: str) -> None:
        """校验记录状态机按顺序逐个流转，非法流转直接拒绝。"""
        current = record["record_status"]
        if current == target:
            return
        allowed = RECORD_TRANSITIONS.get(current, set())
        if target not in allowed:
            raise ValueError(f"校验记录状态不允许从「{current}」流转到「{target}」")
        record["record_status"] = target

    def _sync_seeds_from_record(self, record: dict[str, Any]) -> list[dict[str, Any]]:
        """把校验结果回写给种子管理，返回记录用的种子状态数据。"""
        from app.services.seed_config import SeedConfigService

        return SeedConfigService().apply_source_validation_record(record)

    # ------------------------------------------------------------------
    # 关联查询
    # ------------------------------------------------------------------
    def get_linked_seeds(self, entry_id: int) -> list[dict[str, Any]]:
        """获取该采集源关联的所有种子。"""
        rels = store.get_relations("source_to_seed", source_module=MODULE, source_id=entry_id)
        seeds = []
        for rel in rels:
            seed = store.find("seed_config", rel["target_id"])
            if seed:
                seeds.append(dict(seed))
        return seeds
