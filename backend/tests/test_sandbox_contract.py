"""沙箱契约冻结与差异比对的测试实现。

覆盖需求：
- SBX-0003 启动时冻结 APIG-0002 v1（字段 order_id、amount），期间接口更新到 v2 后，
  实例实际输出与差异对比仍按 v1 计算；新启动的测试才引用 v2；
- 已有 SBX-0001（已关闭）、SBX-0002（测试中）重构后状态与关闭顺序不变；
- 测试失败时只保留上一份完整结果，不产生跨契约版本的半成品；
- 契约只经统一注册表快照读取，冻结版本不可变。
"""
from __future__ import annotations

import unittest

from app.contracts import ContractNotFoundError, contract_registry
from app.services.apigen import ApigenService
from app.services.sandbox import (
    LEDGER_MODULE,
    MODULE,
    STATUS_ORDER,
    SandboxService,
    SandboxTestFailure,
)
from app.store import store


class SandboxContractFreezeTest(unittest.TestCase):
    def setUp(self) -> None:
        store.reset()
        contract_registry.reset()
        self.sandbox = SandboxService()
        self.apigen = ApigenService()

    def _find(self, code: str) -> dict:
        for row in store.rows(MODULE):
            if row["实例编号"] == code:
                return row
        raise AssertionError(f"种子数据缺少 {code}")

    # 既有实例状态与关闭顺序 -------------------------------------------------

    def test_existing_instances_keep_status_and_close_order(self) -> None:
        sbx1 = self._find("SBX-0001")
        sbx2 = self._find("SBX-0002")
        self.assertEqual(sbx1["status"], "已关闭")
        self.assertEqual(sbx2["status"], "测试中")
        # 关闭顺序仍是状态序列里的既定次序，没有新增或打乱状态。
        self.assertEqual(STATUS_ORDER, ["草稿", "测试中", "测试完成", "已关闭"])
        # 历史实例的完整结果与冻结版本保持一致，没有半成品字段。
        for entry in (sbx1, sbx2):
            self.assertEqual(entry["冻结契约版本"], 1)
            self.assertEqual(entry["输出契约版本"], entry["比对契约版本"])
            self.assertTrue(entry["差异对比"]["一致"])

    def test_close_still_requires_testing_status(self) -> None:
        # 草稿实例不能直接关闭，关闭顺序约束保持不变。
        sbx3 = self._find("SBX-0003")
        entry, message = self.sandbox.run_action(sbx3["id"], "关闭实例", {})
        self.assertIsNone(entry)
        self.assertIn("仅允许在「测试中」状态执行", message)
        # 测试中的 SBX-0002 仍可正常关闭并落到「测试完成」。
        sbx2 = self._find("SBX-0002")
        entry, message = self.sandbox.run_action(sbx2["id"], "关闭实例", {})
        self.assertIsNotNone(entry)
        self.assertEqual(entry["status"], "测试完成")

    # SBX-0003：冻结 v1，中途更新 v2 不漂移 -----------------------------------

    def test_sbx0003_freezes_v1_and_ignores_later_v2(self) -> None:
        sbx3 = self._find("SBX-0003")
        self.assertEqual(contract_registry.latest_version_no("APIG-0002"), 1)

        entry, message = self.sandbox.run_action(sbx3["id"], "开始测试", {})
        self.assertIsNotNone(entry, message)
        self.assertEqual(entry["冻结接口编号"], "APIG-0002")
        self.assertEqual(entry["冻结契约版本"], 1)
        self.assertEqual(entry["测试版本号"], "SBV-0003")
        self.assertEqual(set(entry["预期输出"]), {"order_id", "amount"})
        self.assertEqual(set(entry["实际输出"]), {"order_id", "amount"})
        self.assertTrue(entry["差异对比"]["一致"])
        self.assertEqual(entry["status"], "测试中")

        # 测试进行期间相邻接口配置更新，只产生 v2，不回写 v1。
        api_entry, msg = self.apigen.run_action(
            self._apig_0002_id(),
            "更新契约",
            {"预期输出字段": ["order_id", "amount", "currency"]},
        )
        self.assertIsNotNone(api_entry, msg)
        self.assertEqual(contract_registry.latest_version_no("APIG-0002"), 2)
        v1 = contract_registry.snapshot("APIG-0002", 1)
        self.assertEqual(v1["预期输出字段"], ["order_id", "amount"])

        # 重新比对仍读冻结的 v1：即便实际输出带 v2 字段，差异也按 v1 口径算。
        entry, message = self.sandbox.run_action(
            sbx3["id"],
            "重新比对",
            {"actual_override": {
                "order_id": f"{entry['实例编号']}-order_id",
                "amount": f"{entry['实例编号']}-amount",
                "currency": "CNY",
            }},
        )
        self.assertIsNotNone(entry, message)
        self.assertEqual(entry["输出契约版本"], 1)
        self.assertEqual(entry["比对契约版本"], 1)
        self.assertEqual(entry["差异对比"]["多余字段"], ["currency"])
        self.assertFalse(entry["差异对比"]["一致"])

        # 台账里这次测试冻结的快照仍是 v1 两字段。
        ledger = [
            row for row in store.rows(LEDGER_MODULE)
            if row["实例编号"] == "SBX-0003"
        ]
        self.assertEqual(len(ledger), 1)
        self.assertEqual(ledger[0]["冻结契约版本"], 1)
        self.assertEqual(
            ledger[0]["契约快照"]["预期输出字段"], ["order_id", "amount"]
        )

    def test_newly_started_test_references_v2(self) -> None:
        # 先把 APIG-0002 推进到 v2，再新建实例启动测试，应直接冻结 v2。
        self.apigen.run_action(
            self._apig_0002_id(),
            "更新契约",
            {"预期输出字段": ["order_id", "amount", "currency"]},
        )
        entry, missing = self.sandbox.create_entry({
            "实例编号": "SBX-0004",
            "测试产品": "订单履约数据产品",
            "测试接口": "APIG-0002",
            "测试参数": "{}",
        })
        self.assertEqual(missing, [])
        started, message = self.sandbox.run_action(entry["id"], "开始测试", {})
        self.assertIsNotNone(started, message)
        self.assertEqual(started["冻结契约版本"], 2)
        self.assertEqual(
            set(started["预期输出"]), {"order_id", "amount", "currency"}
        )
        self.assertEqual(started["比对契约版本"], 2)

    # 失败原子性：不留下半份沙箱差异对比结果 ----------------------------------

    def test_failed_start_keeps_previous_complete_result(self) -> None:
        sbx2 = self._find("SBX-0002")
        before = {
            "预期输出": dict(sbx2["预期输出"]),
            "实际输出": dict(sbx2["实际输出"]),
            "差异对比": dict(sbx2["差异对比"]),
            "比对契约版本": sbx2["比对契约版本"],
        }
        ledger_count = len(store.rows(LEDGER_MODULE))

        entry, message = self.sandbox.run_action(
            sbx2["id"], "重新比对", {"force_failure": True}
        )
        self.assertIsNone(entry)
        self.assertIn("实际输出采集失败", message)

        # 实例三份数据原样保留，且仍同属 v1，没有任何半成品。
        self.assertEqual(sbx2["预期输出"], before["预期输出"])
        self.assertEqual(sbx2["实际输出"], before["实际输出"])
        self.assertEqual(sbx2["差异对比"], before["差异对比"])
        self.assertEqual(sbx2["输出契约版本"], sbx2["比对契约版本"])
        # 失败不产生新的测试版本台账。
        self.assertEqual(len(store.rows(LEDGER_MODULE)), ledger_count)

    def test_failed_start_before_freeze_changes_nothing(self) -> None:
        sbx3 = self._find("SBX-0003")
        entry, message = self.sandbox.run_action(
            sbx3["id"], "开始测试", {"force_failure": True}
        )
        self.assertIsNone(entry)
        self.assertIsNone(sbx3["实际输出"])
        self.assertIsNone(sbx3["差异对比"])
        self.assertNotIn("冻结契约版本", sbx3)
        self.assertEqual(sbx3["status"], "草稿")

    def test_start_requires_draft_status(self) -> None:
        sbx2 = self._find("SBX-0002")
        entry, message = self.sandbox.run_action(sbx2["id"], "开始测试", {})
        self.assertIsNone(entry)
        self.assertIn("仅允许在「草稿」状态执行", message)

    # 统一快照来源与不可变性 ---------------------------------------------------

    def test_contract_snapshot_is_unified_and_immutable(self) -> None:
        snapshot = contract_registry.snapshot("APIG-0002", 1)
        snapshot["预期输出字段"].append("hacked")
        again = contract_registry.snapshot("APIG-0002", 1)
        self.assertEqual(again["预期输出字段"], ["order_id", "amount"])

        with self.assertRaises(ContractNotFoundError):
            contract_registry.snapshot("APIG-0002", 99)
        with self.assertRaises(ContractNotFoundError):
            contract_registry.snapshot("APIG-X")

    def test_runner_refuses_without_frozen_version(self) -> None:
        sbx3 = self._find("SBX-0003")
        with self.assertRaises(SandboxTestFailure):
            SandboxService.runner.run(sbx3)

    def test_unknown_contract_blocks_start(self) -> None:
        entry, _ = self.sandbox.create_entry({
            "实例编号": "SBX-0005",
            "测试产品": "x",
            "测试接口": "APIG-404",
            "测试参数": "{}",
        })
        started, message = self.sandbox.run_action(entry["id"], "开始测试", {})
        self.assertIsNone(started)
        self.assertIn("契约不存在", message)
        self.assertEqual(entry["status"], "草稿")

    def _apig_0002_id(self) -> int:
        from app.services.apigen import MODULE as APIG_MODULE

        for row in store.rows(APIG_MODULE):
            if row["接口编号"] == "APIG-0002":
                return int(row["id"])
        raise AssertionError("种子数据缺少 APIG-0002")


if __name__ == "__main__":
    unittest.main()
