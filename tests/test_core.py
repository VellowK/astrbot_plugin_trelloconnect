import json
import tempfile
import unittest
from pathlib import Path

from tests.support import draft_json, load

intent = load("intent")
models = load("models")
storage = load("storage")


class IntentTests(unittest.TestCase):
    def test_natural_creation(self):
        for text in [
            "创建代办：买牛奶",
            "帮我创建待办：整理数据",
            "请帮我创建trello卡片：周五交付",
            "新建一条待办",
            "麻烦添加一个Trello卡片，交报告",
            "把这条消息放到trello",
            "帮我记个待办：买牛奶",
            "能帮我新建一个待办：交报告",
        ]:
            with self.subTest(text=text):
                self.assertEqual(intent.parse_intent(text).action, "create")

    def test_no_substring_triggers(self):
        for text in [
            "不要创建待办",
            "你知道怎么创建trello卡片吗",
            "他说创建待办很好用",
            "今天的日程很满",
            "确认创建 ABCDEF-1，但是日期不对",
        ]:
            with self.subTest(text=text):
                self.assertIsNone(intent.parse_intent(text))

    def test_confirmation_accepts_short_reply_and_optional_version(self):
        self.assertEqual(intent.parse_intent("确认创建 abcdef-2").text, "ABCDEF-2")
        for text in ["确认", "确认创建", "可以", "就这样", "好的！", "OK", "ok了", "没问题。"]:
            with self.subTest(text=text):
                self.assertEqual(intent.parse_intent(text).action, "confirm")
                self.assertEqual(intent.parse_intent(text).text, "")

    def test_mixed_or_question_replies_are_not_confirmation(self):
        for text in ["确认吗？", "可以，不过日期不对", "好的，改一下标题", "不要确认", "他说好的"]:
            with self.subTest(text=text):
                self.assertIsNone(intent.parse_intent(text))

    def test_semantic_candidates_are_bounded_and_task_related(self):
        for text in [
            "帮我把这个写进 Trello",
            "这事给我在trello里记一下",
            "麻烦整理成待办",
            "把学代会彩排保存到Trello",
            "Trello 里加个卡片：彩排",
            "不要把这个加到Trello",
            "帮我添加一个任务：学代会彩排",
        ]:
            with self.subTest(text=text):
                self.assertTrue(intent.is_creation_candidate(text))
        for text in ["今天天气如何", "确认", "/123share 添加到trello", "", "trello" + "加" * 12000]:
            with self.subTest(text=text[:40]):
                self.assertFalse(intent.is_creation_candidate(text))

    def test_commands(self):
        self.assertEqual(intent.parse_intent("/trello 创建 交付报告").text, "交付报告")
        self.assertEqual(intent.parse_intent("/trello 核对").action, "reconcile")
        self.assertEqual(intent.parse_intent("/trello 确认 ABCDEF-1").action, "confirm")
        self.assertIsNone(intent.parse_intent("/trellox 创建"))


class DraftTests(unittest.TestCase):
    def parse(self, **kwargs):
        return models.parse_draft(json.dumps(draft_json(**kwargs)), [1, 2])

    def test_valid(self):
        draft = self.parse(
            image_ids=[2, 2], due="2026-10-25T15:00:00+08:00", due_note="明天下午3点"
        )
        self.assertEqual(draft.image_ids, [2])
        self.assertTrue(draft.due.endswith("+08:00"))

    def test_code_fence(self):
        self.assertEqual(
            models.parse_draft("```json\n" + json.dumps(draft_json()) + "\n```", []).title,
            "提交报告",
        )

    def test_invalid_types_and_bounds(self):
        invalid = [
            {"title": ""},
            {"description": "a" * 6001},
            {"checklist": "步骤"},
            {"checklist": ["步骤"] * 21},
            {"checklist": [True]},
            {"image_ids": [3]},
            {"image_ids": [True]},
            {"image_ids": "1"},
            {"due": "2026-10-25"},
            {"due": "2026-10-25T15:00:00", "due_note": "明天"},
            {"due": "2026-10-25T15:00:00+08:00"},
            {"questions": "问题"},
        ]
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(models.DraftError):
                self.parse(**values)

    def test_nul_only_required_fields_rejected(self):
        for values in [{"title": "\x00"}, {"checklist": ["\x00"]}]:
            with self.subTest(values=values), self.assertRaises(models.DraftError):
                self.parse(**values)

    def test_model_cannot_authorize_or_select_paths(self):
        draft = self.parse(confirmed=True, authorized=True, url="http://127.0.0.1/private")
        self.assertFalse(hasattr(draft, "confirmed"))
        with self.assertRaises(models.DraftError):
            models.parse_draft("not json", [])

    def test_preview_and_description(self):
        draft = self.parse()
        preview = models.render_draft(
            draft, "ABCDEF-1", "2026-10-24T12:00:00+08:00", "团队 / 项目", [], []
        )
        self.assertIn("确认 / 可以 / 就这样（无需编号", preview)
        self.assertIn("截止时间：未设置", preview)
        self.assertIn("astrbot-operation:op1", models.card_description(draft, "time", "op1"))


class StoreTests(unittest.TestCase):
    def test_durable_scoped_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = storage.ReceiptStore(root)
            self.assertIsNone(store.get("alice"))
            store.save("alice", {"operation": "a", "phase": "creating", "board_id": "b" * 24})
            self.assertEqual(storage.ReceiptStore(root).get("alice")["phase"], "creating")
            self.assertIsNone(store.get("bob"))
            self.assertFalse(any("alice" in p.name for p in root.iterdir()))

    def test_structurally_corrupt_receipt_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            store = storage.ReceiptStore(Path(directory))
            for value in [
                {"phase": "unknown"},
                {"phase": "typo", "operation": "op", "board_id": "b" * 24},
                {"phase": ["creating"]},
                {"phase": 1},
            ]:
                store._path("alice").write_text(json.dumps(value))
                with self.assertRaises(OSError):
                    store.get("alice")

    def test_corrupt_receipt_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            store = storage.ReceiptStore(Path(directory))
            store._path("alice").write_text("bad json")
            with self.assertRaises(OSError):
                store.get("alice")
