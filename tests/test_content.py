"""Optional checklists and independent readback must not hide a partially written card."""

import json
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

from tests.support import client_mock, draft_json, load, target

models, trello, submission, storage, summarizer = (
    load(name) for name in ["models", "trello", "submission", "storage", "summarizer"]
)

CARD_ID = "d" * 24
LOCATION = "地点：牛津布鲁克斯学院6C205"


class OptionalChecklistTests(unittest.TestCase):
    def test_single_activity_accepts_location_and_no_checklist(self):
        draft = models.parse_draft(
            json.dumps(draft_json(title="参加学代会彩排", description=LOCATION, checklist=[])), []
        )
        self.assertEqual(draft.description, LOCATION)
        self.assertEqual(draft.checklist, [])
        preview = models.render_draft(draft, "ABCDEF-1", "time", "团队 / 项目", [], [])
        self.assertIn("任务清单：无（不创建）", preview)
        self.assertIn("v0.1.4", preview)
        self.assertNotIn("规划路线", preview)
        self.assertFalse(any(line.startswith("1. ") for line in preview.splitlines()))
        self.assertTrue(models.card_description(draft, "time", "op").startswith(LOCATION))

    def test_description_and_checklist_may_both_be_omitted(self):
        data = draft_json()
        data.pop("description")
        data.pop("checklist")
        draft = models.parse_draft(json.dumps(data), [])
        self.assertEqual(draft.description, "")
        self.assertEqual(draft.checklist, [])
        description = models.card_description(draft, "time", "op")
        self.assertTrue(description.startswith("记录时间：time"))
        self.assertIn("astrbot-operation:op", description)

    def test_invalid_optional_fields_still_rejected(self):
        for overrides in [
            {"description": None},
            {"description": 1},
            {"checklist": None},
            {"checklist": "不需要"},
            {"checklist": [""]},
        ]:
            with self.subTest(overrides=overrides), self.assertRaises(models.DraftError):
                models.parse_draft(json.dumps(draft_json(**overrides)), [])

    def test_fingerprints_normalize_newlines_not_missing_content(self):
        left = trello.content_fingerprints("地点：教室\r\n时间\r\n", ["甲", "乙"])
        right = trello.content_fingerprints("地点：教室\n时间", ["乙", "甲"])
        self.assertEqual(left, right)
        self.assertNotIn("教室", json.dumps(left, ensure_ascii=False))
        self.assertNotEqual(left, trello.content_fingerprints("", ["甲", "乙"]))
        self.assertNotEqual(left, trello.content_fingerprints("地点：教室\n时间", ["甲"]))
        self.assertIsNone(trello.content_fingerprints("", [])["checklist"])


class ActivityPromptTests(unittest.IsolatedAsyncioTestCase):
    async def test_location_only_activity_is_accepted_without_forcing_steps(self):
        context = types.SimpleNamespace(
            get_current_chat_provider_id=AsyncMock(return_value="current"),
            llm_generate=AsyncMock(
                return_value=types.SimpleNamespace(
                    completion_text=json.dumps(
                        draft_json(title="参加学代会彩排", description=LOCATION, checklist=[])
                    )
                )
            ),
        )
        now = datetime(2026, 10, 25, 10, tzinfo=timezone.utc)
        draft, warnings = await summarizer.Summarizer(context).generate(
            "umo", "明天参加彩排，地点6C205", "", None, [], now.isoformat(), now
        )
        self.assertEqual(draft.description, LOCATION)
        self.assertEqual(draft.checklist, [])
        self.assertEqual(warnings, [])
        instructions = context.llm_generate.call_args.kwargs["system_prompt"]
        self.assertIn("默认空数组", instructions)
        self.assertIn("只有原文确实有多个", instructions)
        self.assertIn("具体的硬性要求/交付物/限制应简短保留", instructions)
        self.assertNotIn("checklist（1~20", instructions)


class ContentReadbackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = trello.TrelloClient("fixture-key", "fixture-token", "team", "board")
        self.expected = trello.content_fingerprints(LOCATION, ["整理数据", "审核"])
        self.remote_card = {"id": CARD_ID, "desc": LOCATION}
        self.remote_list = {
            "name": "任务清单",
            "checkItems": [{"name": "整理数据"}, {"name": "审核"}],
        }

    async def test_reads_description_and_chinese_check_items(self):
        self.client.request = AsyncMock(side_effect=[self.remote_card, [self.remote_list]])
        result = await self.client.verify_content(CARD_ID, self.expected)
        self.assertEqual(result, {"description": "verified", "checklist": "verified"})
        self.assertTrue(trello.content_verified(result))
        calls = self.client.request.call_args_list
        self.assertTrue(all(call.args[0] == "GET" for call in calls))
        self.assertEqual(calls[0].kwargs["params"], {"fields": "desc"})
        self.assertEqual(calls[1].kwargs["params"], {"checkItems": "all"})

    async def test_missing_description_and_checklist_not_success(self):
        self.client.request = AsyncMock(side_effect=[{"id": CARD_ID, "desc": ""}, []])
        result = await self.client.verify_content(CARD_ID, self.expected)
        self.assertEqual(result, {"description": "mismatch", "checklist": "mismatch"})
        self.assertFalse(trello.content_verified(result))

    async def test_incomplete_or_extra_check_items_fail_comparison(self):
        for names in [["整理数据"], ["整理数据", "审核", "自行添加"]]:
            with self.subTest(names=names):
                self.client.request = AsyncMock(
                    side_effect=[
                        self.remote_card,
                        [{"name": "任务清单", "checkItems": [{"name": name} for name in names]}],
                    ]
                )
                result = await self.client.verify_content(CARD_ID, self.expected)
                self.assertEqual(result["description"], "verified")
                self.assertEqual(result["checklist"], "mismatch")

    async def test_http_read_failure_is_not_claimed_as_missing(self):
        self.client.request = AsyncMock(side_effect=trello.TrelloError("HTTP 429"))
        result = await self.client.verify_content(CARD_ID, self.expected)
        self.assertEqual(result, {"description": "unverified", "checklist": "unverified"})
        self.assertFalse(trello.content_verified(result))

    async def test_malformed_readback_is_unverified(self):
        for lists in [None, {}, [{"name": "任务清单", "checkItems": None}]]:
            with self.subTest(lists=lists):
                self.client.request = AsyncMock(side_effect=[{"id": CARD_ID}, lists])
                result = await self.client.verify_content(CARD_ID, self.expected)
                self.assertEqual(result, {"description": "unverified", "checklist": "unverified"})

    async def test_wrong_card_is_not_verified(self):
        self.client.request = AsyncMock(return_value={"id": "f" * 24, "desc": LOCATION})
        result = await self.client.verify_content(
            CARD_ID, trello.content_fingerprints(LOCATION, [])
        )
        self.assertEqual(result["description"], "unverified")

    async def test_empty_checklist_skips_write_and_read(self):
        self.client.request = AsyncMock(return_value=self.remote_card)
        await self.client.add_checklist(CARD_ID, [])
        self.client.request.assert_not_awaited()
        result = await self.client.verify_content(
            CARD_ID, trello.content_fingerprints(LOCATION, [])
        )
        self.assertEqual(result, {"description": "verified", "checklist": "not_requested"})
        self.client.request.assert_awaited_once()


class SubmissionReadbackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = storage.ReceiptStore(Path(self.temp.name))
        self.client = client_mock()
        self.service = submission.Submitter(self.client, self.store)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def commit(self, items=None):
        draft = models.Draft(**draft_json(description=LOCATION, checklist=items or []))
        return await self.service.commit(
            "scope", draft, [], target(), "op12345678", "ABCDEF-1", "time"
        )

    async def test_no_checklist_does_not_create_empty_container(self):
        result = await self.commit()
        self.assertEqual(result["phase"], "complete")
        self.client.add_checklist.assert_not_awaited()
        self.assertIn(LOCATION, self.client.create_card.call_args.args[2])
        self.client.verify_content.assert_awaited_once()
        self.assertNotIn(LOCATION, json.dumps(self.store.get("scope"), ensure_ascii=False))

    async def test_readback_mismatch_locks_then_can_be_readonly_rechecked(self):
        self.client.verify_content.side_effect = None
        self.client.verify_content.return_value = {
            "description": "mismatch",
            "checklist": "not_requested",
        }
        result = await self.commit()
        self.assertEqual(result["phase"], "partial")
        message = storage.receipt_message(result)
        self.assertIn("描述：缺失或与草稿不一致", message)
        self.assertIn("任务清单：不需要", message)
        with self.assertRaises(trello.TrelloError):
            await self.commit()
        self.client.verify_content.return_value["description"] = "verified"
        self.assertIn("描述：已回读核对", await self.service.reconcile("scope"))
        self.assertEqual(self.store.get("scope")["phase"], "complete")
        self.client.create_card.assert_awaited_once()
        self.client.add_checklist.assert_not_awaited()

    async def test_readback_unavailable_keeps_link_and_no_auto_retry(self):
        self.client.verify_content.side_effect = None
        self.client.verify_content.return_value = {
            "description": "unverified",
            "checklist": "unverified",
        }
        result = await self.commit(["审核"])
        self.assertEqual(result["phase"], "partial")
        self.assertIn("无法核验", storage.receipt_message(result))
        self.assertIn("trello.com/c/", storage.receipt_message(result))
        self.client.create_card.assert_awaited_once()

    async def test_write_failure_never_masked_by_successful_content_read(self):
        self.client.add_checklist.side_effect = trello.TrelloError("HTTP 403")
        result = await self.commit(["审核"])
        self.assertEqual(result["phase"], "partial")
        self.assertEqual(result["failed_step"], "checklist")
        self.assertIn("清单写入请求未完成", storage.receipt_message(result))
        await self.service.reconcile("scope")
        self.assertEqual(self.store.get("scope")["phase"], "partial")
        self.client.add_checklist.assert_awaited_once()

    async def test_reconcile_after_restart_uses_digests_without_task_text(self):
        await self.commit(["审核"])
        self.client.create_card.reset_mock()
        self.client.add_checklist.reset_mock()
        service = submission.Submitter(self.client, storage.ReceiptStore(Path(self.temp.name)))
        self.assertIn("描述：已回读核对", await service.reconcile("scope"))
        self.client.create_card.assert_not_awaited()
        self.client.add_checklist.assert_not_awaited()
