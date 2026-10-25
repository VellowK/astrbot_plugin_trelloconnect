"""SDK/API boundary contracts use doubles; no external services or credentials."""

import asyncio
import base64
import json
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import aiohttp

from tests.support import Image, draft_json, load

images, summarizer, trello = (load(name) for name in ["images", "summarizer", "trello"])
models = load("models")
PNG = b"\x89PNG\r\n\x1a\n" + b"fixture"
CARD = {"id": "d" * 24, "shortUrl": "https://trello.com/c/abc123"}


class Body:
    def __init__(self, chunks):
        self.chunks = chunks

    async def iter_chunked(self, size):
        for chunk in self.chunks:
            yield chunk


class Response:
    def __init__(self, status=200, chunks=None, headers=None, length=None):
        self.status = status
        self.content = Body(chunks or [])
        self.headers = headers or {}
        self.content_length = length

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class TrelloTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = trello.TrelloClient("secret-key", "secret-token", "team", "board")
        self.session = types.SimpleNamespace(closed=False, request=MagicMock())
        self.client.session = self.session

    async def test_streaming_json_and_auth(self):
        self.session.request.return_value = Response(chunks=[b'{"id":', b'"abc"}'])
        self.assertEqual(await self.client.request("GET", "/test"), {"id": "abc"})
        args, kwargs = self.session.request.call_args
        self.assertEqual(args, ("GET", "https://api.trello.com/1/test"))
        self.assertEqual(kwargs["params"]["token"], "secret-token")
        self.assertFalse(kwargs["allow_redirects"])

    async def test_http_errors_are_redacted_and_writes_classified(self):
        for status in [301, 401, 403, 404, 408, 429, 500, 503]:
            for method in ["GET", "POST"]:
                with self.subTest(status=status, method=method):
                    self.session.request.return_value = Response(
                        status, [b"secret-token secret-key"]
                    )
                    with self.assertRaises(trello.TrelloError) as caught:
                        await self.client.request(method, "/cards")
                    self.assertNotIn("secret-", str(caught.exception))
                    self.assertEqual(
                        caught.exception.uncertain,
                        method == "POST" and (status >= 500 or status == 408),
                    )

    async def test_network_errors_are_not_retried(self):
        self.session.request.side_effect = aiohttp.ClientConnectionError("secret-token")
        with self.assertRaises(trello.TrelloError) as caught:
            await self.client.request("POST", "/cards")
        self.assertTrue(caught.exception.uncertain)
        self.assertNotIn("secret-", str(caught.exception))
        self.session.request.assert_called_once()

    async def test_malformed_success_is_uncertain(self):
        self.session.request.return_value = Response(chunks=[b"not json"])
        with self.assertRaises(trello.TrelloError) as caught:
            await self.client.request("POST", "/cards")
        self.assertTrue(caught.exception.uncertain)

    async def test_response_bound(self):
        self.session.request.return_value = Response(chunks=[b"x" * 1024 * 1024] * 3)
        with self.assertRaises(trello.TrelloError) as caught:
            await self.client.request("POST", "/cards")
        self.assertTrue(caught.exception.uncertain)

    async def test_invalid_id_after_post_is_uncertain(self):
        self.client.request = AsyncMock(return_value={})
        with self.assertRaises(trello.TrelloError) as caught:
            await self.client.create_card("c" * 24, "title", "desc", None)
        self.assertTrue(caught.exception.uncertain)

    async def test_reminder_reused(self):
        self.client.request = AsyncMock(return_value=[{"id": "c" * 24, "name": "Reminder"}])
        self.assertEqual(await self.client.ensure_reminder("b" * 24), "c" * 24)
        self.client.request.assert_awaited_once()

    async def test_reminder_created_only_once_under_concurrency(self):
        values = []

        async def api(method, path, **kwargs):
            await asyncio.sleep(0)
            if method == "GET":
                return list(values)
            values.append({"id": "c" * 24, "name": "reminder"})
            return values[0]

        self.client.request = AsyncMock(side_effect=api)
        result = await asyncio.gather(
            self.client.ensure_reminder("b" * 24), self.client.ensure_reminder("b" * 24)
        )
        self.assertEqual(result, ["c" * 24] * 2)
        self.assertEqual(
            sum(call.args[0] == "POST" for call in self.client.request.call_args_list), 1
        )

    async def test_duplicate_reminder_blocks(self):
        self.client.request = AsyncMock(return_value=[{"name": "reminder"}, {"name": "REMINDER"}])
        with self.assertRaises(trello.TrelloError):
            await self.client.ensure_reminder("b" * 24)

    async def test_board_workspace_mismatch_blocks(self):
        self.client.request = AsyncMock(
            side_effect=[{"id": "a" * 24}, {"id": "b" * 24, "idOrganization": "z" * 24}]
        )
        with self.assertRaises(trello.TrelloError):
            await self.client.verify()

    async def test_board_url_and_resource_path_safety(self):
        self.assertEqual(
            trello.resource_id("https://trello.com/b/abc123/title", board=True), "abc123"
        )
        for value in ["", "../members/me", "board?token=x", "https://evil.invalid/b/abc"]:
            with self.subTest(value=value), self.assertRaises(trello.TrelloError):
                trello.resource_id(value, board=True)

    async def test_checklist_contract(self):
        self.client.request = AsyncMock(return_value={"id": "e" * 24})
        await self.client.add_checklist("d" * 24, ["first", "second"])
        calls = self.client.request.call_args_list
        self.assertEqual(calls[0].args, ("POST", "/cards/" + "d" * 24 + "/checklists"))
        self.assertEqual([call.kwargs["data"]["name"] for call in calls[1:]], ["first", "second"])

    async def test_attachment_is_multipart_file_not_remote_url(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "image.png"
            path.write_bytes(PNG)

            async def upload(method, route, *, data):
                self.assertEqual(method, "POST")
                self.assertTrue(route.endswith("/attachments"))
                self.assertIsInstance(data, aiohttp.FormData)
                return {}

            self.client.request = AsyncMock(side_effect=upload)
            await self.client.add_picture("d" * 24, path, "image-1.png", "image/png")

    async def test_reconcile_reads_archived_cards_and_pages(self):
        first = [{"id": str(index), "desc": ""} for index in range(1000)]
        self.client.request = AsyncMock(
            side_effect=[first, [{**CARD, "desc": "astrbot-operation:op"}]]
        )
        self.assertEqual(len(await self.client.find_operation("b" * 24, "op")), 1)
        self.assertEqual(self.client.request.call_args.kwargs["params"]["before"], "999")
        self.assertEqual(self.client.request.call_args.kwargs["params"]["filter"], "all")

    async def test_repeated_page_never_proves_absence(self):
        page = [{"id": str(index), "desc": ""} for index in range(1000)]
        self.client.request = AsyncMock(return_value=page)
        with self.assertRaises(trello.TrelloError):
            await self.client.find_operation("b" * 24, "op")


class ImageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.stager = images.ImageStager(Path(self.temp.name) / "cache", 100)

    async def asyncTearDown(self):
        await self.stager.close()
        self.temp.cleanup()

    async def test_base64_stage_and_cleanup(self):
        picture = await self.stager.stage(
            Image(file="base64://" + base64.b64encode(PNG).decode()), 2
        )
        self.assertEqual(picture.id, 2)
        self.assertEqual(picture.path.read_bytes(), PNG)
        self.assertEqual(picture.mime, "image/png")
        self.stager.remove([picture])
        self.assertFalse(picture.path.exists())

    async def test_local_and_file_uri_sources(self):
        path = Path(self.temp.name) / "source%20 image.png"
        path.write_bytes(PNG)
        for source in [str(path), path.as_uri()]:
            picture = await self.stager.stage(Image(file=source), 1)
            self.assertNotEqual(picture.path, path)
            self.assertEqual(picture.path.read_bytes(), PNG)
            self.stager.remove([picture])
        self.assertTrue(path.exists())

    async def test_magic_not_filename(self):
        with self.assertRaises(images.ImageError):
            await self.stager.stage(Image(file="base64://" + base64.b64encode(b"html").decode()), 1)
        for data, mime in [
            (b"\xff\xd8\xfftest", "image/jpeg"),
            (b"GIF89atest", "image/gif"),
            (b"RIFF1234WEBPtest", "image/webp"),
        ]:
            self.assertEqual(images.image_kind(data)[0], mime)

    async def test_size_empty_and_encoding_limits(self):
        for payload in ["", "!!invalid!!", base64.b64encode(PNG * 100).decode()]:
            with self.subTest(payload=payload[:10]), self.assertRaises(images.ImageError):
                await self.stager.stage(Image(file="base64://" + payload), 1)
        self.assertFalse(list(self.stager.root.iterdir()))

    async def test_network_error_is_redacted(self):
        self.stager._download = AsyncMock(
            side_effect=aiohttp.ClientConnectionError("private image signature")
        )
        with self.assertRaises(images.ImageError) as caught:
            await self.stager.stage(Image(url="https://example.com/image"), 1)
        self.assertNotIn("signature", str(caught.exception))

    async def test_ssrf_addresses(self):
        for url in [
            "http://localhost/x",
            "http://127.0.0.1/x",
            "http://10.0.0.1/x",
            "http://169.254.169.254/x",
            "http://[::1]/x",
            "http://[::ffff:127.0.0.1]/x",
            "file:///tmp/x",
            "https://user:pass@example.com/x",
        ]:
            with self.subTest(url=url), self.assertRaises(images.ImageError):
                images.validate_url(url, False)
        images.validate_url("https://example.com/image", False)
        images.validate_url("http://127.0.0.1/image", True)

    async def test_dns_private_and_mixed_results_rejected(self):
        resolver = images.PublicResolver()
        real = resolver.inner
        try:
            for addresses in [
                [{"host": "127.0.0.1"}],
                [{"host": "1.1.1.1"}, {"host": "192.168.1.1"}],
                [],
            ]:
                resolver.inner = types.SimpleNamespace(resolve=AsyncMock(return_value=addresses))
                with self.assertRaises(OSError):
                    await resolver.resolve("example.com")
            resolver.inner = types.SimpleNamespace(
                resolve=AsyncMock(return_value=[{"host": "1.1.1.1"}])
            )
            self.assertEqual((await resolver.resolve("example.com"))[0]["host"], "1.1.1.1")
        finally:
            await real.close()

    async def test_redirect_to_private_host_rejected(self):
        self.stager.session = types.SimpleNamespace(
            closed=False,
            get=MagicMock(return_value=Response(302, headers={"Location": "https://127.0.0.1/x"})),
            close=AsyncMock(),
        )
        with self.assertRaises(images.ImageError):
            await self.stager._download("https://example.com/x")
        self.stager.session.get.assert_called_once()

    async def test_https_downgrade_rejected(self):
        self.stager.session = types.SimpleNamespace(
            closed=False,
            get=MagicMock(return_value=Response(302, headers={"Location": "http://example.com/x"})),
            close=AsyncMock(),
        )
        with self.assertRaises(images.ImageError):
            await self.stager._download("https://example.com/x")

    async def test_download_bounded_without_content_length(self):
        self.stager.session = types.SimpleNamespace(
            closed=False,
            get=MagicMock(return_value=Response(chunks=[b"a" * 60, b"b" * 60])),
            close=AsyncMock(),
        )
        with self.assertRaises(images.ImageError):
            await self.stager._download("https://example.com/x")

    async def test_unc_file_paths_rejected(self):
        for value in [
            "//server/share/x.png",
            "\\\\server\\share\\x.png",
            "file://server/share/x.png",
        ]:
            with self.subTest(value=value), self.assertRaises(images.ImageError):
                await self.stager.stage(Image(file=value), 1)


class SummarizerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.context = types.SimpleNamespace(
            get_current_chat_provider_id=AsyncMock(return_value="current-provider"),
            llm_generate=AsyncMock(
                return_value=types.SimpleNamespace(completion_text=json.dumps(draft_json()))
            ),
        )
        self.service = summarizer.Summarizer(self.context)
        self.now = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)

    async def generate(self, pictures=None, previous=None, feedback=""):
        return await self.service.generate(
            "platform:FriendMessage:alice",
            "任务资料",
            feedback,
            previous,
            pictures or [],
            self.now.isoformat(),
            self.now,
        )

    async def test_intent_uses_current_provider_without_tools_or_history(self):
        self.context.llm_generate.return_value = types.SimpleNamespace(
            completion_text='{"action":"create"}'
        )
        self.assertTrue(await self.service.is_create_request("current:umo", "帮我写进Trello"))
        self.context.get_current_chat_provider_id.assert_awaited_once_with("current:umo")
        params = self.context.llm_generate.call_args.kwargs
        self.assertEqual(params["chat_provider_id"], "current-provider")
        self.assertIsNone(params["tools"])
        self.assertEqual(params["image_urls"], [])
        self.assertNotIn("contexts", params)
        self.assertEqual(json.loads(params["prompt"]), {"text": "帮我写进Trello"})

    async def test_intent_only_accepts_bounded_creation_decision(self):
        for output in [
            '{"action":"none"}',
            "[]",
            "null",
            '{"action":"confirm"}',
            '{"action":"create","confirmed":true}',
            "not json",
            "x" * 257,
            123,
        ]:
            with self.subTest(output=str(output)[:40]):
                self.context.llm_generate.return_value = types.SimpleNamespace(
                    completion_text=output
                )
                self.assertFalse(await self.service.is_create_request("umo", "Trello里加一个"))
        self.context.llm_generate.return_value = types.SimpleNamespace(
            completion_text='```json\n{"action":"create"}\n```'
        )
        self.assertTrue(await self.service.is_create_request("umo", "Trello里加一个"))

    async def test_intent_failure_is_redacted(self):
        self.context.llm_generate.side_effect = RuntimeError("secret-token")
        with self.assertRaises(models.DraftError) as caught:
            await self.service.is_create_request("umo", "添加到Trello")
        self.assertNotIn("secret-token", str(caught.exception))

    async def test_intent_timeout_is_bounded(self):
        self.service.timeout = 0.001

        async def slow(**kwargs):
            await asyncio.sleep(10)

        self.context.llm_generate.side_effect = slow
        with self.assertRaises(models.DraftError):
            await self.service.is_create_request("umo", "添加到Trello")

    async def test_current_provider_no_tools_and_no_shared_history(self):
        draft, warnings = await self.generate()
        self.assertEqual(draft.title, "提交报告")
        self.assertEqual(warnings, [])
        self.context.get_current_chat_provider_id.assert_awaited_once_with(
            "platform:FriendMessage:alice"
        )
        params = self.context.llm_generate.call_args.kwargs
        self.assertEqual(params["chat_provider_id"], "current-provider")
        self.assertIsNone(params["tools"])
        self.assertNotIn("contexts", params)
        self.assertFalse(json.loads(params["prompt"])["images_visible"])

    async def test_vision_failure_falls_back_with_warning(self):
        self.context.llm_generate.side_effect = [
            RuntimeError("secret"),
            types.SimpleNamespace(completion_text=json.dumps(draft_json())),
        ]
        pictures = [models.Picture(1, Path("image.png"), "image/png", "image-1.png")]
        _, warnings = await self.generate(pictures=pictures)
        self.assertTrue(warnings)
        calls = self.context.llm_generate.call_args_list
        self.assertEqual(calls[0].kwargs["image_urls"], ["image.png"])
        self.assertEqual(calls[1].kwargs["image_urls"], [])
        self.assertFalse(json.loads(calls[1].kwargs["prompt"])["images_visible"])

    async def test_disabled_vision_warns_without_sending_image(self):
        self.service.vision = False
        _, warnings = await self.generate(
            [models.Picture(1, Path("image.png"), "image/png", "image-1.png")]
        )
        self.assertTrue(warnings)
        self.assertEqual(self.context.llm_generate.call_args.kwargs["image_urls"], [])

    async def test_feedback_and_previous_are_structured(self):
        previous = models.Draft(**draft_json())
        await self.generate(previous=previous, feedback="不设置截止时间")
        payload = json.loads(self.context.llm_generate.call_args.kwargs["prompt"])
        self.assertEqual(payload["previous"], previous.as_dict())
        self.assertEqual(payload["feedback"], "不设置截止时间")
        self.assertEqual(payload["source"], "任务资料")

    async def test_provider_failure_redacted(self):
        self.context.get_current_chat_provider_id.side_effect = RuntimeError("secret")
        with self.assertRaises(models.DraftError) as caught:
            await self.generate()
        self.assertNotIn("secret", str(caught.exception))
        self.context.llm_generate.assert_not_awaited()

    async def test_model_failure_does_not_leak(self):
        self.context.llm_generate.side_effect = RuntimeError("secret")
        with self.assertRaises(models.DraftError) as caught:
            await self.generate()
        self.assertNotIn("secret", str(caught.exception))

    async def test_malformed_draft_fails(self):
        self.context.llm_generate.return_value = types.SimpleNamespace(completion_text="no json")
        with self.assertRaises(models.DraftError):
            await self.generate()

    async def test_call_timeout(self):
        self.service.timeout = 0.001

        async def slow(**kwargs):
            await asyncio.sleep(10)

        self.context.llm_generate.side_effect = slow
        with self.assertRaises(models.DraftError):
            await self.generate()
