import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.support import client_mock, draft_json, load, target

models, storage, submission, trello = (
    load(name) for name in ["models", "storage", "submission", "trello"]
)


class SubmissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = storage.ReceiptStore(Path(self.temp.name))
        self.client = client_mock()
        self.service = submission.Submitter(self.client, self.store)
        self.draft = models.Draft(**draft_json())

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def commit(self, **kwargs):
        return await self.service.commit(
            "alice",
            self.draft,
            kwargs.get("pictures", []),
            target(),
            "operation12345678",
            "ABCDEF-1",
            "2026-10-24T12:00:00+08:00",
        )

    async def test_success_and_duplicate_confirmation(self):
        result = await self.commit()
        self.assertEqual(result["phase"], "complete")
        self.client.add_checklist.assert_awaited_once()
        with self.assertRaises(trello.TrelloError):
            await self.commit()
        self.client.create_card.assert_awaited_once()

    async def test_writes_journal_before_post(self):
        async def create(*args):
            self.assertEqual(self.store.get("alice")["phase"], "creating")
            return {"id": "d" * 24, "shortUrl": "https://trello.com/c/abc"}

        self.client.create_card.side_effect = create
        await self.commit()

    async def test_uncertain_timeout_never_reposts_even_after_restart(self):
        self.client.create_card.side_effect = trello.TrelloError("timeout", uncertain=True)
        with self.assertRaises(trello.TrelloError):
            await self.commit()
        self.assertEqual(self.store.get("alice")["phase"], "unknown")
        self.service = submission.Submitter(self.client, storage.ReceiptStore(Path(self.temp.name)))
        with self.assertRaises(trello.TrelloError):
            await self.commit()
        self.client.create_card.assert_awaited_once()

    async def test_definite_error_can_be_reconfirmed(self):
        self.client.create_card.side_effect = trello.TrelloError("403")
        with self.assertRaises(trello.TrelloError):
            await self.commit()
        self.assertEqual(self.store.get("alice")["phase"], "failed")
        self.client.create_card.side_effect = None
        self.assertEqual((await self.commit())["phase"], "complete")

    async def test_list_failure_locks_when_uncertain(self):
        self.client.ensure_reminder.side_effect = trello.TrelloError("timeout", uncertain=True)
        with self.assertRaises(trello.TrelloError):
            await self.commit()
        self.assertEqual(self.store.get("alice")["phase"], "unknown")
        self.client.create_card.assert_not_awaited()

    async def test_partial_never_creates_again(self):
        self.client.add_checklist.side_effect = trello.TrelloError("429")
        result = await self.commit()
        self.assertEqual(result["phase"], "partial")
        self.assertIn("trello.com", result["url"])
        with self.assertRaises(trello.TrelloError):
            await self.commit()
        self.client.create_card.assert_awaited_once()

    async def test_only_selected_images_uploaded(self):
        pictures = [
            models.Picture(i, Path(f"img{i}.png"), "image/png", f"image-{i}.png") for i in [1, 2]
        ]
        self.draft.image_ids = [2]
        await self.commit(pictures=pictures)
        self.client.add_picture.assert_awaited_once_with(
            "d" * 24, Path("img2.png"), "image-2.png", "image/png"
        )

    async def test_configuration_change_fails_before_write(self):
        self.client.verify.return_value = {**target(), "board_id": "z" * 24}
        with self.assertRaises(trello.TrelloError):
            await self.commit()
        self.client.ensure_reminder.assert_not_awaited()
        self.client.create_card.assert_not_awaited()

    async def test_disk_failure_prevents_post(self):
        with (
            patch.object(self.store, "save", side_effect=OSError("disk full")),
            self.assertRaises(OSError),
        ):
            await self.commit()
        self.client.create_card.assert_not_awaited()

    async def test_post_success_journal_failure_still_locks(self):
        real_save = self.store.save

        def fail_created(scope, receipt):
            if receipt["phase"] == "created":
                raise OSError("disk full")
            real_save(scope, receipt)

        with patch.object(self.store, "save", side_effect=fail_created), self.assertRaises(OSError):
            await self.commit()
        self.assertEqual(self.store.get("alice")["phase"], "creating")
        with self.assertRaises(trello.TrelloError):
            await self.commit()
        self.client.create_card.assert_awaited_once()

    async def test_reconcile_finds_card_and_does_not_add_missing_content(self):
        self.store.save("alice", {"phase": "unknown", "operation": "op", "board_id": "b" * 24})
        self.client.find_operation.return_value = [
            {"id": "d" * 24, "shortUrl": "https://trello.com/c/abc"}
        ]
        response = await self.service.reconcile("alice")
        self.assertIn("已存在", response)
        self.assertEqual(self.store.get("alice")["phase"], "partial")
        self.client.create_card.assert_not_awaited()
        self.client.add_checklist.assert_not_awaited()

    async def test_reconcile_not_found_stays_locked(self):
        self.store.save(
            "alice", {"phase": "creating", "operation": "op12345678", "board_id": "b" * 24}
        )
        self.assertIn("清除未决 12345678", await self.service.reconcile("alice"))
        self.assertEqual(self.store.get("alice")["phase"], "creating")
