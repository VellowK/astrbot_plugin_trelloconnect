"""Durable last-submission receipt, without credentials, task text or images."""

import hashlib
import json
import os
from pathlib import Path

UNRESOLVED = {"preparing", "creating", "unknown", "created"}


class ReceiptStore:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def _path(self, scope: str) -> Path:
        return self.root / (hashlib.sha256(scope.encode()).hexdigest() + ".json")

    def get(self, scope: str) -> dict | None:
        path = self._path(scope)
        if not path.exists():
            return None
        try:
            receipt = json.loads(path.read_text(encoding="utf-8"))
            if (
                not isinstance(receipt, dict)
                or not isinstance(receipt.get("phase"), str)
                or receipt["phase"]
                not in UNRESOLVED
                | {
                    "failed",
                    "partial",
                    "complete",
                    "cleared",
                }
            ):
                raise ValueError
            if not all(
                isinstance(receipt.get(key), str) and receipt[key]
                for key in ("operation", "board_id")
            ):
                raise ValueError
            return receipt
        except (ValueError, UnicodeError):
            raise OSError(
                "提交回执损坏，请管理员检查插件 receipts 目录；为防重复已停止写入。"
            ) from None

    def save(self, scope: str, receipt: dict):
        target = self._path(scope)
        temp = target.with_suffix(".tmp")
        with temp.open("w", encoding="utf-8") as stream:
            json.dump(receipt, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, target)


def receipt_message(receipt: dict | None) -> str:
    if not receipt:
        return "没有提交记录或待确认草稿。"
    phase = receipt.get("phase")
    if receipt.get("url"):
        text = f"卡片已存在：{receipt['url']}"
        verification = receipt.get("verification", {})
        labels = {
            "verified": "已回读核对",
            "not_requested": "不需要，未创建",
            "mismatch": "缺失或与草稿不一致",
            "unverified": "暂时无法核验（不代表内容为空）",
        }
        if verification:
            for key, label in (("description", "描述"), ("checklist", "任务清单")):
                text += f"\n{label}：{labels.get(verification.get(key), labels['unverified'])}"
        if receipt.get("failed_step") == "checklist":
            text += "\n清单写入请求未完成，图片尚未继续上传。"
        elif receipt.get("failed_step") == "attachments":
            text += "\n图片上传未完成，请手动核对附件。"
        if phase != "complete":
            text += "\n描述、清单或图片可能未完整写入/核验，请打开卡片检查。可 /trello 核对 再次只读核验；缺失内容需手动补齐，不会自动重复创建。"
        return text
    if phase in UNRESOLVED:
        return f"上次提交结果未决，不能重复创建。\n操作编号：astrbot-operation:{receipt['operation']}\n请用 /trello 核对；若未找到仍需手动核实看板。"
    if phase == "failed":
        return "上次提交明确失败，尚未创建卡片，可重新确认当前草稿。"
    return "上次未决记录已由你手动清除。"
