"""接口契约注册表：沙箱测试读取契约的唯一入口。

设计要点：
- 同一份接口配置（按接口编号）的每次相邻更新都只追加新版本，历史版本永不覆盖；
- 版本以不可变对象保存，对外只给深拷贝快照，调用方改不到注册表里的冻结数据；
- 沙箱的「开始测试」与「差异比对」两个入口都只能通过这里取快照，不允许各自翻
  apigen 表各写一遍读取逻辑。
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

# 初始契约：接口编号 -> 按版本顺序排列的版本定义（第一个即 v1）。
SEED_CONTRACTS: dict[str, list[dict[str, Any]]] = {
    "APIG-0001": [{"预期输出字段": ("product_id", "product_name")}],
    "APIG-0002": [{"预期输出字段": ("order_id", "amount")}],
    "APIG-0003": [{"预期输出字段": ("report_id", "summary")}],
}


class ContractNotFoundError(LookupError):
    """契约或指定版本不存在。"""


@dataclass(frozen=True)
class ContractVersion:
    """不可变的契约版本：版本号与预期输出字段一旦冻结就不能再改。"""

    api_id: str
    version: int
    expected_fields: tuple[str, ...]
    sample_output: tuple[tuple[str, Any], ...]

    def to_snapshot(self) -> dict[str, Any]:
        """转成普通 dict 快照（深拷贝），供沙箱测试版本冻结存档。"""
        return {
            "接口编号": self.api_id,
            "契约版本": self.version,
            "预期输出字段": list(self.expected_fields),
            "响应样例": dict(self.sample_output),
        }


class ContractRegistry:
    def __init__(self) -> None:
        self._contracts: dict[str, list[ContractVersion]] = {}
        self.reset()

    def reset(self) -> None:
        """按种子数据重建注册表，仅供初始化与测试隔离使用。"""
        self._contracts = {}
        for api_id, versions in SEED_CONTRACTS.items():
            for index, definition in enumerate(versions, start=1):
                self._contracts.setdefault(api_id, []).append(
                    ContractVersion(
                        api_id=api_id,
                        version=index,
                        expected_fields=tuple(definition["预期输出字段"]),
                        sample_output=tuple(definition.get("响应样例", {}).items()),
                    )
                )

    def register(self, api_id: str, expected_fields: list[str]) -> ContractVersion:
        """登记一份全新接口的 v1 契约。"""
        if api_id in self._contracts:
            raise ValueError(f"接口 {api_id} 已存在契约，配置更新应发布新版本而不是重建")
        version = ContractVersion(api_id, 1, tuple(expected_fields), ())
        self._contracts[api_id] = [version]
        return version

    def publish_new_version(
        self, api_id: str, expected_fields: list[str]
    ) -> ContractVersion:
        """相邻业务面接口配置更新：只能追加新版本，老版本原样保留。"""
        history = self._contracts.get(api_id)
        if not history:
            raise ContractNotFoundError(f"接口 {api_id} 尚无契约，无法发布新版本")
        version = ContractVersion(
            api_id=api_id,
            version=history[-1].version + 1,
            expected_fields=tuple(expected_fields),
            sample_output=(),
        )
        history.append(version)
        return version

    def get(self, api_id: str, version: int | None = None) -> ContractVersion:
        history = self._contracts.get(api_id)
        if not history:
            raise ContractNotFoundError(f"接口 {api_id} 的契约不存在")
        if version is None:
            return history[-1]
        for item in history:
            if item.version == version:
                return item
        raise ContractNotFoundError(f"接口 {api_id} 的契约版本 v{version} 不存在")

    def latest_version_no(self, api_id: str) -> int:
        return self.get(api_id).version

    def snapshot(self, api_id: str, version: int | None = None) -> dict[str, Any]:
        """统一契约读取写法：返回深拷贝快照，调用方怎么改都污染不到注册表。"""
        return copy.deepcopy(self.get(api_id, version).to_snapshot())


# 进程内单例：开始测试、重新比对、接口配置更新全部走这一个实例。
contract_registry = ContractRegistry()
