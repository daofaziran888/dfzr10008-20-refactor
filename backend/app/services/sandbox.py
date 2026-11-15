"""产品沙箱测试业务规则。

核心约束：
1. 每次「开始测试」都冻结当时的接口契约版本（契约快照 + 测试版本号），实例实际输出
   快照与差异比对全程以该冻结版本为准；测试期间接口配置再更新也只产生新版本，不影响
   在跑实例。
2. 「开始测试」与「重新比对」共用同一份冻结快照加载与执行代码，契约只读统一快照来源
   （app.contracts.contract_registry），不再各自读一遍最新契约。
3. 一次测试的预期输出、实际输出快照、差异对比先全部在内存构建，最后一次性提交；中途
   失败时实例上只保留上一份完整结果，绝不留下跨契约版本拼凑的半成品。
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any

from app.contracts import contract_registry
from app.store import store

MODULE = "sandbox"
LEDGER_MODULE = "sandbox_test_version"
REQUIRED_FIELDS = ["实例编号", "测试产品", "测试接口", "测试参数"]
STATUS_ORDER = ["草稿", "测试中", "测试完成", "已关闭"]
# 动作 -> 允许执行该动作的来源状态（执行后推进到状态序列的下一个状态）。
ACTION_RULES = {"开始测试": "草稿", "关闭实例": "测试中", "归档结果": "测试完成"}
NEGATIVE_ACTIONS: list[str] = []
RECHECK_ACTION = "重新比对"
TEST_VERSION_PREFIX = "SBV"


class SandboxTestFailure(RuntimeError):
    """测试执行或差异比对失败：此时不得向实例写入任何新结果。"""


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def _next_test_version_no() -> int:
    rows = store.rows(LEDGER_MODULE)
    return max((int(row.get("id", 0)) for row in rows), default=0) + 1


class SandboxRunner:
    """沙箱测试执行器：冻结快照加载、实例输出采集、差异对比的唯一实现。"""

    def load_frozen_snapshot(self, entry: dict[str, Any]) -> dict[str, Any]:
        """统一快照来源：开始测试与重新比对都走这里，绝不直读最新契约。

        开始测试时版本尚未冻结属于调用顺序错误，直接抛出而不是临时取最新契约兜底，
        避免实际输出与差异对比漂移到两个版本上。
        """
        version_no = entry.get("冻结契约版本")
        api_id = entry.get("测试接口")
        if not api_id or not isinstance(version_no, int):
            raise SandboxTestFailure(
                f"实例 {entry.get('实例编号')} 未冻结契约版本，无法执行测试比对"
            )
        return contract_registry.snapshot(api_id, version_no)

    def run(
        self,
        entry: dict[str, Any],
        *,
        actual_override: dict[str, Any] | None = None,
        force_failure: bool = False,
    ) -> dict[str, Any]:
        """按实例冻结的契约版本跑完一次测试，返回可一次性提交的完整结果（不落库）。"""
        snapshot = self.load_frozen_snapshot(entry)
        expected_fields = list(snapshot["预期输出字段"])
        # 先确认采集步骤会失败也不影响：预期输出在采集前已构建，失败直接抛出、不产生半份结果。
        expected_output = self._build_expected_output(snapshot, entry)
        actual_output = self._capture_actual_output(
            snapshot, entry, expected_output, actual_override, force_failure
        )
        diff = self._compare(expected_fields, expected_output, actual_output)

        return {
            "测试版本号": entry["测试版本号"],
            "冻结接口编号": snapshot["接口编号"],
            "冻结契约版本": snapshot["契约版本"],
            "预期输出": expected_output,
            "实际输出": actual_output,
            "差异对比": diff,
        }

    def _build_expected_output(
        self, snapshot: dict[str, Any], entry: dict[str, Any]
    ) -> dict[str, Any]:
        """预期输出只由冻结契约的字段集合决定，含字段的样例值。"""
        sample = snapshot.get("响应样例", {})
        return {field: deepcopy(sample.get(field)) for field in snapshot["预期输出字段"]}

    def _capture_actual_output(
        self,
        snapshot: dict[str, Any],
        entry: dict[str, Any],
        expected_output: dict[str, Any],
        actual_override: dict[str, Any] | None,
        force_failure: bool,
    ) -> dict[str, Any]:
        """采集实例实际输出。沙箱里用确定性规则模拟，且只认冻结版本的字段。"""
        if force_failure:
            raise SandboxTestFailure(
                f"实例 {entry.get('实例编号')} 的实际输出采集失败，本次测试结果不予提交"
            )
        if actual_override is not None:
            return {key: deepcopy(value) for key, value in actual_override.items()}
        # 默认实例按契约正确产出，与预期同构同值；字段差异由调用方注入的实际输出体现。
        return deepcopy(expected_output)

    def _compare(
        self,
        expected_fields: list[str],
        expected_output: dict[str, Any],
        actual_output: dict[str, Any],
    ) -> dict[str, Any]:
        missing = [field for field in expected_fields if field not in actual_output]
        unexpected = [field for field in actual_output if field not in expected_fields]
        mismatched = [
            field
            for field in expected_fields
            if field in actual_output and actual_output[field] != expected_output[field]
        ]
        return {
            "一致": not (missing or unexpected or mismatched),
            "缺失字段": missing,
            "多余字段": unexpected,
            "取值不符字段": mismatched,
        }


class SandboxService:
    runner = SandboxRunner()

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
            rows = [row for row in rows if keyword in str(row.get("实例编号", ""))]
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
        entry["实例状态"] = entry["status"]
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str, values: dict[str, Any] | None = None) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"沙箱实例 {entry_id} 不存在或已归档"
        if action == RECHECK_ACTION:
            return self._recheck(entry, values or {})
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于产品沙箱测试可执行范围"
        required_status = ACTION_RULES[action]
        if entry.get("status") != required_status:
            return None, (
                f"动作「{action}」仅允许在「{required_status}」状态执行，"
                f"当前为「{entry.get('status')}」"
            )
        if action == "开始测试":
            return self._start_test(entry, values or {})
        return self._advance_status(entry, action)

    def _start_test(self, entry: dict[str, Any], values: dict[str, Any]) -> tuple[dict[str, Any], str]:
        api_id = str(entry.get("测试接口") or "").strip()
        try:
            snapshot = contract_registry.snapshot(api_id)
        except LookupError as exc:
            return None, str(exc)

        # 先在内存里把整轮测试跑完：任何一步失败都不写实例、不建测试版本。
        version_seq = _next_test_version_no()
        frozen = self._freeze_contract(api_id, snapshot, version_seq)
        staged = dict(entry)
        staged.update(frozen)
        try:
            result = self.runner.run(
                staged, force_failure=bool(values.get("force_failure"))
            )
        except SandboxTestFailure as exc:
            return None, str(exc)

        # 全部就绪后一次性提交：测试版本台账与实例结果同批落库。
        store.rows(LEDGER_MODULE).append(
            self._build_ledger_row(entry, snapshot, version_seq)
        )
        entry.update(frozen)
        self._commit_result(entry, result, next_status="测试中")
        return entry, (
            f"沙箱实例已开始测试，冻结 {api_id} v{snapshot['契约版本']}，"
            f"测试版本 {frozen['测试版本号']}"
        )

    def _recheck(self, entry: dict[str, Any], values: dict[str, Any]) -> tuple[dict[str, Any], str]:
        if entry.get("status") not in ("测试中", "测试完成"):
            return None, f"动作「{RECHECK_ACTION}」仅允许在测试中或测试完成状态执行"
        override = values.get("actual_override")
        force_failure = bool(values.get("force_failure"))
        try:
            # 与开始测试共用同一个执行器与冻结快照加载，差异对比仍按冻结版本计算。
            result = self.runner.run(
                entry,
                actual_override=override if isinstance(override, dict) else None,
                force_failure=force_failure,
            )
        except SandboxTestFailure as exc:
            return None, str(exc)
        self._commit_result(entry, result)
        return entry, (
            f"差异对比已按冻结契约 "
            f"{entry.get('冻结接口编号')} v{entry.get('冻结契约版本')} 重新计算"
        )

    def _advance_status(self, entry: dict[str, Any], action: str) -> tuple[dict[str, Any], str]:
        index = STATUS_ORDER.index(entry["status"])
        target = STATUS_ORDER[index + 1]
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        entry["实例状态"] = target
        return entry, f"沙箱实例已{action}"

    def _freeze_contract(
        self, api_id: str, snapshot: dict[str, Any], version_seq: int
    ) -> dict[str, Any]:
        return {
            "测试版本号": f"{TEST_VERSION_PREFIX}-{version_seq:04d}",
            "冻结接口编号": api_id,
            "冻结契约版本": snapshot["契约版本"],
        }

    def _build_ledger_row(
        self, entry: dict[str, Any], snapshot: dict[str, Any], version_seq: int
    ) -> dict[str, Any]:
        return {
            "id": version_seq,
            "实例编号": entry["实例编号"],
            "测试版本号": f"{TEST_VERSION_PREFIX}-{version_seq:04d}",
            "冻结接口编号": snapshot["接口编号"],
            "冻结契约版本": snapshot["契约版本"],
            "契约快照": snapshot,
            "创建时间": _now(),
        }

    def _commit_result(self, entry: dict[str, Any], result: dict[str, Any], *, next_status: str | None = None) -> None:
        """整份结果一次性提交；上一份结果只在新结果完整构建后才被覆盖。"""
        entry["预期输出"] = deepcopy(result["预期输出"])
        entry["实际输出"] = deepcopy(result["实际输出"])
        entry["差异对比"] = deepcopy(result["差异对比"])
        # 三份数据都带上同一冻结版本标识，杜绝快照与比对来自不同契约版本。
        entry["输出契约版本"] = result["冻结契约版本"]
        entry["比对契约版本"] = result["冻结契约版本"]
        if next_status is not None:
            entry["status"] = next_status
            entry["pending"] = next_status != STATUS_ORDER[-1]
            entry["实例状态"] = next_status

    def list_test_versions(self) -> list[dict[str, Any]]:
        """测试版本台账：每次开始测试追加一条，记录当时冻结的契约快照。"""
        return store.rows(LEDGER_MODULE)
