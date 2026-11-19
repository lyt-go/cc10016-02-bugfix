"""采集源接入校验记录链路测试。

覆盖故障修复点：
1. 不可达/协议不支持的采集源被拒绝确认接入，来源保持待校验；
2. 正常 https 来源确认接入保留，种子继续可用；
3. 采集源接入校验记录同源存储，重复校验不追加互相矛盾的确认记录；
4. 状态机按顺序逐个流转，不允许跳级；
5. 健康检查失败统一“保留接入记录”口径：记录显示来源异常，种子待修复；
6. 种子管理读取校验记录决定种子可用/待修复，与校验记录保持一致。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.crawl_source import CrawlSourceService
from app.services.seed_config import SeedConfigService
from app.store import store


def setup_function() -> None:
    """每个测试函数前重置store状态。"""
    store._tables.clear()
    store._pipeline_states.clear()
    store._relations.clear()


def _make_source(svc: CrawlSourceService, code: str, scheme: str, url: str) -> dict:
    entry, missing = svc.create_entry({
        "源标识": code, "源名称": f"源{code}", "协议类型": scheme, "基础URL": url,
    })
    assert not missing
    assert entry is not None
    return entry


def _make_bound_seed(seed_svc: SeedConfigService, code: str, source_id: int, pattern: str) -> dict:
    entry, missing = seed_svc.create_entry({
        "种子编号": code, "目标URL模式": pattern, "列表页XPath": "//div[@class='item']",
        "关联采集源ID": source_id,
    })
    assert not missing
    seed_svc.run_action(entry["id"], "提交校验")
    seed_svc.run_action(entry["id"], "标记生效")
    return entry


def test_wss_source_rejected_and_seed_kept_with_repair_flag() -> None:
    """wss 来源校验一次：确认接入被拒，原种子保持可用但进入待修复。"""
    src_svc = CrawlSourceService()
    seed_svc = SeedConfigService()
    wss = _make_source(src_svc, "SRC-WSS-101", "wss", "wss://fin-x.example.com/ws")
    seed = _make_bound_seed(seed_svc, "SEED-WSS-101", wss["id"], "wss://fin-x.example.com/ws?symbol={symbol}")

    src_svc.run_action(wss["id"], "提交校验")
    entry, message = src_svc.run_action(wss["id"], "确认接入")

    # 确认被拒绝，来源保持待校验，不被错误关闭
    assert entry is None
    assert "拒绝确认接入" in message
    source = store.find("crawl_source", wss["id"])
    assert source["status"] == "待校验"

    # 生成采集源接入校验记录：拒绝确认、真实连通结果
    record = src_svc.get_validation_record(wss["id"])
    assert record is not None
    assert record["decision"] == "拒绝确认"
    assert record["record_status"] == "已拒绝"
    assert record["reachable"] is False
    assert "wss" in record["message"]

    # 校验记录中的种子状态数据：原种子保持可用（状态不被改写），但待修复、不可调度
    assert len(record["seed_statuses"]) == 1
    seed_status = record["seed_statuses"][0]
    assert seed_status["种子编号"] == "SEED-WSS-101"
    assert seed_status["status"] == "已生效"
    assert seed_status["repair_state"] == "待修复"
    assert seed_status["schedulable"] is False

    # 种子清单保留入口并标注待修复原因
    stored = store.find("seed_config", seed["id"])
    assert stored["status"] == "已生效"
    assert stored["repair_state"] == "待修复"
    assert "不支持的协议" in stored["repair_reason"]
    detail = seed_svc.get_entry(seed["id"])
    assert detail["_repair_state"] == "待修复"
    assert detail["_schedulable"] is False
    # 原种子引用保留
    rels = store.get_relations("source_to_seed", source_module="crawl_source", source_id=wss["id"])
    assert len(rels) == 1 and rels[0]["target_id"] == seed["id"]


def test_https_source_confirmed_and_seed_stays_available() -> None:
    """正常 https 来源校验一次：确认接入保留，种子与校验记录一致。"""
    src_svc = CrawlSourceService()
    seed_svc = SeedConfigService()
    https = _make_source(src_svc, "SRC-HTTPS-102", "https", "https://mall-x.example.com/api")
    seed = _make_bound_seed(seed_svc, "SEED-HTTPS-102", https["id"], "https://mall-x.example.com/api?page={page}")

    src_svc.run_action(https["id"], "提交校验")
    entry, message = src_svc.run_action(https["id"], "确认接入")

    assert entry is not None
    assert entry["status"] == "已接入"
    assert entry["pending"] is False

    record = src_svc.get_validation_record(https["id"])
    assert record["decision"] == "确认接入"
    assert record["record_status"] == "已确认"
    assert record["reachable"] is True

    # 种子继续可用、可调度，与校验记录一致
    seed_status = record["seed_statuses"][0]
    assert seed_status["repair_state"] == ""
    assert seed_status["schedulable"] is True
    detail = seed_svc.get_entry(seed["id"])
    assert detail["_repair_state"] == ""
    assert detail["_schedulable"] is True
    assert detail["_source_validation"]["decision"] == "确认接入"


def test_repeated_validation_never_appends_contradictory_records() -> None:
    """重复校验同一来源只更新同源记录，不追加互相矛盾的确认记录。"""
    src_svc = CrawlSourceService()
    https = _make_source(src_svc, "SRC-HTTPS-103", "https", "https://mall-y.example.com/api")
    wss = _make_source(src_svc, "SRC-WSS-103", "wss", "wss://fin-y.example.com/ws")

    src_svc.run_action(https["id"], "提交校验")
    src_svc.run_action(https["id"], "确认接入")
    src_svc.run_action(wss["id"], "提交校验")
    src_svc.run_action(wss["id"], "确认接入")

    # 重复校验
    src_svc.validate_connection(https["id"])
    src_svc.validate_connection(https["id"])
    src_svc.validate_connection(wss["id"])

    records = src_svc.list_validation_records()
    assert len(records) == 2  # 每个来源仅一条同源记录
    https_record = src_svc.get_validation_record(https["id"])
    wss_record = src_svc.get_validation_record(wss["id"])
    # 判定保持自洽：健康源始终确认接入，失败源始终拒绝确认
    assert https_record["decision"] == "确认接入"
    assert https_record["version"] >= 3
    assert wss_record["decision"] == "拒绝确认"
    assert wss_record["record_status"] == "已拒绝"
    # 历史按版本逐个追加，无矛盾判定并存（健康源不会出现拒绝/异常）
    decisions = {item["decision"] for item in https_record["history"]}
    assert decisions <= {"待确认", "确认接入"}
    assert https_record["decision"] == "确认接入"
    wss_decisions = {item["decision"] for item in wss_record["history"]}
    assert "确认接入" not in wss_decisions


def test_state_machine_flows_in_order() -> None:
    """状态机按顺序逐个流转：草稿不能直接确认接入，待校验不能直接停用。"""
    src_svc = CrawlSourceService()
    entry = _make_source(src_svc, "SRC-ORD-104", "https", "https://mall-z.example.com/api")

    jumped, message = src_svc.run_action(entry["id"], "确认接入")
    assert jumped is None
    assert "顺序" in message
    assert store.find("crawl_source", entry["id"])["status"] == "草稿"

    stopped, message = src_svc.run_action(entry["id"], "停用源")
    assert stopped is None
    assert "顺序" in message

    # 正常顺序：草稿 -> 待校验 -> 已接入 -> 已停用
    e1, _ = src_svc.run_action(entry["id"], "提交校验")
    assert e1["status"] == "待校验"
    e2, _ = src_svc.run_action(entry["id"], "确认接入")
    assert e2["status"] == "已接入"
    e3, _ = src_svc.run_action(entry["id"], "停用源")
    assert e3["status"] == "已停用"
    # 记录状态机同样按顺序流转
    record = src_svc.get_validation_record(entry["id"])
    assert record["record_status"] == "已确认"


def test_health_recheck_failure_marks_source_abnormal_but_keeps_access_record() -> None:
    """已接入来源复查失败：保留接入记录，记录显示来源异常，种子待修复。"""
    src_svc = CrawlSourceService()
    seed_svc = SeedConfigService()
    https = _make_source(src_svc, "SRC-HTTPS-105", "https", "https://mall-w.example.com/api")
    seed = _make_bound_seed(seed_svc, "SEED-HTTPS-105", https["id"], "https://mall-w.example.com/api?page={page}")
    src_svc.run_action(https["id"], "提交校验")
    src_svc.run_action(https["id"], "确认接入")

    # 模拟来源失效（协议不再支持）
    store.find("crawl_source", https["id"])["基础URL"] = "wss://mall-w.example.com/ws"
    src_svc.validate_connection(https["id"])

    record = src_svc.get_validation_record(https["id"])
    # 统一口径：保留接入记录，显示来源异常而非来源已撤销
    assert record["decision"] == "来源异常"
    assert record["record_status"] == "已确认"
    assert store.find("crawl_source", https["id"])["status"] == "已接入"

    # 种子暂停调度但保留历史绑定与人工修复入口
    detail = seed_svc.get_entry(seed["id"])
    assert detail["_repair_state"] == "待修复"
    assert detail["_schedulable"] is False
    assert store.find("seed_config", seed["id"])["status"] == "已生效"
    rels = store.get_relations("source_to_seed", source_module="crawl_source", source_id=https["id"])
    assert len(rels) == 1

    # 来源恢复后复查通过，种子回到可用
    store.find("crawl_source", https["id"])["基础URL"] = "https://mall-w.example.com/api"
    src_svc.validate_connection(https["id"])
    record = src_svc.get_validation_record(https["id"])
    assert record["decision"] == "确认接入"
    detail = seed_svc.get_entry(seed["id"])
    assert detail["_repair_state"] == ""
    assert detail["_schedulable"] is True


def test_validate_pending_sources_generates_records() -> None:
    """批量读取待接入采集源并校验接入状态，逐个生成校验记录。"""
    src_svc = CrawlSourceService()
    _make_source(src_svc, "SRC-PEND-106", "https", "https://mall-v.example.com/api")
    wss = _make_source(src_svc, "SRC-PEND-107", "wss", "wss://fin-v.example.com/ws")
    done = _make_source(src_svc, "SRC-PEND-108", "https", "https://mall-u.example.com/api")
    src_svc.run_action(done["id"], "提交校验")
    src_svc.run_action(done["id"], "确认接入")

    records = src_svc.validate_pending_sources()

    # 只处理待接入（草稿/待校验）来源，已接入不重复处理
    assert len(records) == 2
    assert records[0]["source_id"] < records[1]["source_id"]  # 按顺序逐个流转
    by_code = {record["源标识"]: record for record in records}
    assert by_code["SRC-PEND-106"]["decision"] == "待确认"
    assert by_code["SRC-PEND-106"]["reachable"] is True
    assert by_code["SRC-PEND-107"]["reachable"] is False
    # 已接入来源的既有记录不被批量校验改写
    assert src_svc.get_validation_record(done["id"])["decision"] == "确认接入"


def test_seed_scheduling_follows_validation_record() -> None:
    """种子管理读取校验记录：失败来源不能生成可调度种子。"""
    src_svc = CrawlSourceService()
    seed_svc = SeedConfigService()
    wss = _make_source(src_svc, "SRC-WSS-109", "wss", "wss://fin-t.example.com/ws")
    seed = _make_bound_seed(seed_svc, "SEED-WSS-109", wss["id"], "wss://fin-t.example.com/ws?symbol={symbol}")

    # 校验前：来源未接入，种子不可调度
    assert seed_svc.get_entry(seed["id"])["_schedulable"] is False

    src_svc.run_action(wss["id"], "提交校验")
    src_svc.run_action(wss["id"], "确认接入")

    # 校验失败回写后：种子待修复、不可调度，清单数据与校验记录同源一致
    items, _ = seed_svc.list_entries()
    row = next(item for item in items if item["id"] == seed["id"])
    record = src_svc.get_validation_record(wss["id"])
    assert row["_repair_state"] == record["seed_statuses"][0]["repair_state"] == "待修复"
    assert row["_schedulable"] == record["seed_statuses"][0]["schedulable"] is False
