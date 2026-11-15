"""API接口生成业务规则：状态流转、字段校验与筛选口径都收在这里。

接口契约的版本化不在本服务内另写一套：相邻配置更新统一经 ContractRegistry 追加新版本，
沙箱测试也只从注册表取冻结快照，保证「写契约」和「读契约」是同一份来源。
"""
from __future__ import annotations

from typing import Any

from app.contracts import ContractNotFoundError, contract_registry
from app.store import store

MODULE = "apigen"
REQUIRED_FIELDS = ["接口编号", "接口名称", "请求方法"]
STATUS_ORDER = ["草稿", "测试中", "已发布", "已废弃"]
ACTION_RULES = {"发布接口": "草稿", "废弃接口": "已发布", "删除接口": "已废弃"}
NEGATIVE_ACTIONS = []


class ApigenService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("接口编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str, values: dict[str, Any] | None = None) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"接口配置 {entry_id} 不存在或已归档"
        if action == "更新契约":
            return self._publish_contract(entry, values or {})
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于API接口生成可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"接口配置已{action}"

    def _publish_contract(self, entry: dict[str, Any], values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """相邻业务面接口配置更新：只创建新契约版本，历史版本与在跑沙箱测试不受影响。"""
        raw_fields = values.get("预期输出字段")
        if isinstance(raw_fields, str):
            expected_fields = [item.strip() for item in raw_fields.split(",") if item.strip()]
        elif isinstance(raw_fields, (list, tuple)):
            expected_fields = [str(item).strip() for item in raw_fields if str(item).strip()]
        else:
            return None, "更新契约需提供「预期输出字段」列表或逗号分隔字符串"
        if not expected_fields:
            return None, "更新契约的预期输出字段不能为空"
        api_id = str(entry["接口编号"])
        try:
            version = contract_registry.publish_new_version(api_id, expected_fields)
        except ContractNotFoundError as exc:
            return None, str(exc)
        entry["契约版本"] = version.version
        entry["预期输出字段"] = list(version.expected_fields)
        return entry, f"接口 {api_id} 契约已更新为 v{version.version}，历史版本保持不变"
