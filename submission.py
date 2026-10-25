"""Two-phase local journal around non-idempotent Trello operations."""

from datetime import datetime, timezone

from .models import Draft, Picture, card_description
from .storage import UNRESOLVED, ReceiptStore, receipt_message
from .trello import TrelloClient, TrelloError, card_link, content_fingerprints, content_verified


class Submitter:
    def __init__(self, client: TrelloClient, store: ReceiptStore):
        self.client, self.store = client, store

    async def commit(
        self,
        scope: str,
        draft: Draft,
        pictures: list[Picture],
        target: dict,
        operation: str,
        code: str,
        created_at: str,
    ) -> dict:
        previous = self.store.get(scope)
        if previous and (
            previous.get("phase") in UNRESOLVED
            or (previous.get("code") == code and previous.get("phase") in {"partial", "complete"})
        ):
            raise TrelloError(receipt_message(previous))
        # Revalidate immediately before writing. Never silently redirect a preview
        # to a changed workspace/board after configuration changes.
        current = await self.client.verify()
        if (
            current["board_id"] != target["board_id"]
            or current["workspace_id"] != target["workspace_id"]
        ):
            raise TrelloError("工作区/看板配置已变化，请重新创建并确认草稿。")
        description = card_description(draft, created_at, operation)
        receipt = {
            "expected": content_fingerprints(description, draft.checklist),
            "operation": operation,
            "code": code,
            "board_id": target["board_id"],
            "phase": "preparing",
            "time": datetime.now(timezone.utc).isoformat(),
        }
        self.store.save(scope, receipt)
        try:
            list_id = await self.client.ensure_reminder(target["board_id"])
            receipt["phase"] = "creating"
            self.store.save(scope, receipt)
            card = await self.client.create_card(list_id, draft.title, description, draft.due)
        except TrelloError as exc:
            receipt["phase"] = "unknown" if exc.uncertain else "failed"
            self.store.save(scope, receipt)
            raise
        # If this save fails, the older 'creating' record still blocks resubmits.
        receipt.update(phase="created", card_id=card["id"], url=card_link(card))
        self.store.save(scope, receipt)
        step = "checklist"
        try:
            if draft.checklist:
                await self.client.add_checklist(card["id"], draft.checklist)
            step = "attachments"
            for picture in pictures:
                if picture.id in draft.image_ids:
                    await self.client.add_picture(
                        card["id"], picture.path, picture.name, picture.mime
                    )
            receipt["write_finished"] = True
        except (TrelloError, OSError):
            receipt["failed_step"] = step
        # Save the known card before readback: a read failure must never cause a repost.
        receipt["phase"] = "partial"
        self.store.save(scope, receipt)
        return await self._verify(scope, receipt)

    async def _verify(self, scope: str, receipt: dict) -> dict:
        receipt["verification"] = await self.client.verify_content(
            receipt["card_id"], receipt["expected"]
        )
        receipt["phase"] = (
            "complete"
            if receipt.get("write_finished") and content_verified(receipt["verification"])
            else "partial"
        )
        self.store.save(scope, receipt)
        return receipt

    async def reconcile(self, scope: str) -> str:
        receipt = self.store.get(scope)
        if not receipt:
            return receipt_message(receipt)
        if receipt.get("url"):
            if receipt.get("expected") and receipt.get("card_id"):
                return receipt_message(await self._verify(scope, receipt))
            if receipt.get("phase") in UNRESOLVED:
                receipt["phase"] = "partial"
                self.store.save(scope, receipt)
            return receipt_message(receipt)
        if receipt.get("phase") not in UNRESOLVED:
            return receipt_message(receipt)
        cards = await self.client.find_operation(receipt["board_id"], receipt["operation"])
        if len(cards) == 1:
            receipt.update(phase="partial", card_id=cards[0]["id"], url=card_link(cards[0]))
            self.store.save(scope, receipt)
            if receipt.get("expected"):
                return receipt_message(await self._verify(scope, receipt))
            return receipt_message(receipt)
        if len(cards) > 1:
            return "发现多个相同操作编号的卡片，请手动处理重复卡片；未自动继续写入。"
        return (
            receipt_message(receipt)
            + f"\n暂未找到。搜索可能漏掉刚创建、已移动或被修改的卡片；请手动检查。确认确实没有卡片后才可执行：/trello 清除未决 {receipt['operation'][-8:]}"
        )
