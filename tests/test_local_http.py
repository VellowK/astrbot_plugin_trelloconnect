"""Exercise aiohttp streaming/multipart against a loopback fixture, not Trello."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from aiohttp import web

from tests.support import Image, draft_json, load, target

images, trello, models, storage, submission = (
    load(name) for name in ["images", "trello", "models", "storage", "submission"]
)
PNG = b"\x89PNG\r\n\x1a\nfixture"


class LocalHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.received = None
        self.remote_card = None
        self.remote_checklists = []
        self.writes = []
        self.drop_description = False
        self.drop_checkitems = False
        self.fail_reads = False
        app = web.Application()
        app.router.add_post("/cards", self.create_card)
        app.router.add_get("/cards/{card}", self.read_card)
        app.router.add_post("/cards/{card}/checklists", self.create_checklist)
        app.router.add_get("/cards/{card}/checklists", self.read_checklists)
        app.router.add_post("/checklists/{checklist}/checkItems", self.create_checkitem)
        app.router.add_get("/stream", self.stream)
        app.router.add_get("/image", self.picture)
        app.router.add_post("/cards/{card}/attachments", self.attachment)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        self.temp = tempfile.TemporaryDirectory()
        self.client = trello.TrelloClient("fixture-key", "fixture-token", "team", "board")
        self.client.BASE = self.base
        self.stager = images.ImageStager(Path(self.temp.name), 1024, allow_private=True)

    async def asyncTearDown(self):
        await self.client.close()
        await self.stager.close()
        await self.runner.cleanup()
        self.temp.cleanup()

    async def stream(self, request):
        self.assertEqual(request.query["token"], "fixture-token")
        result = web.StreamResponse(headers={"Content-Type": "application/json"})
        await result.prepare(request)
        for chunk in [b'{"id":', b'"abc",', b'"ok":true}']:
            await result.write(chunk)
        await result.write_eof()
        return result

    async def picture(self, request):
        self.assertNotIn("token", request.query)
        return web.Response(body=PNG, content_type="image/png")

    async def attachment(self, request):
        form = await request.post()
        self.received = {
            "data": form["file"].file.read(),
            "name": form["file"].filename,
            "label": form["name"],
        }
        return web.json_response({"id": "e" * 24})

    async def json_body(self, request):
        self.assertEqual(request.content_type, "application/json")
        self.assertEqual(request.query["key"], "fixture-key")
        self.assertEqual(request.query["token"], "fixture-token")
        body = await request.json()
        self.assertNotIn("token", body)
        self.writes.append(request.path)
        return body

    async def create_card(self, request):
        body = await self.json_body(request)
        self.remote_card = {
            **body,
            "id": "d" * 24,
            "shortUrl": "https://trello.com/c/fixture123",
            "desc": "" if self.drop_description else body["desc"],
        }
        return web.json_response(self.remote_card)

    async def read_card(self, request):
        if self.fail_reads:
            return web.Response(status=503)
        return web.json_response(self.remote_card)

    async def create_checklist(self, request):
        body = await self.json_body(request)
        self.remote_checklists.append({**body, "id": "e" * 24, "checkItems": []})
        return web.json_response(self.remote_checklists[-1])

    async def create_checkitem(self, request):
        body = await self.json_body(request)
        self.assertIs(body["checked"], False)
        if not self.drop_checkitems:
            self.remote_checklists[-1]["checkItems"].append(body)
        return web.json_response({"id": "f" * 24})

    async def read_checklists(self, request):
        if self.fail_reads:
            return web.Response(status=503)
        self.assertEqual(request.query["checkItems"], "all")
        return web.json_response(self.remote_checklists)

    async def submit(self, items):
        self.client.verify = AsyncMock(return_value=target())
        self.client.ensure_reminder = AsyncMock(return_value="c" * 24)
        self.store = storage.ReceiptStore(Path(self.temp.name) / "receipts")
        self.submitter = submission.Submitter(self.client, self.store)
        draft = models.Draft(
            **draft_json(
                title="参加学代会彩排",
                description="地点：牛津布鲁克斯学院6C205",
                checklist=items,
                due="2026-10-26T12:40:00+08:00",
                due_note="2026年10月26日12:40",
            )
        )
        return await self.submitter.commit(
            "scope", draft, [], target(), "op12345678", "ABCDEF-1", "2026-10-25T18:30:27+08:00"
        )

    async def test_activity_description_is_json_unicode_without_checklist(self):
        result = await self.submit([])
        self.assertEqual(result["phase"], "complete")
        self.assertEqual(
            result["verification"], {"description": "verified", "checklist": "not_requested"}
        )
        self.assertTrue(self.remote_card["desc"].startswith("地点：牛津布鲁克斯学院6C205"))
        self.assertIn("记录时间：2026-10-25T18:30:27+08:00", self.remote_card["desc"])
        self.assertEqual(self.remote_card["due"], "2026-10-26T12:40:00+08:00")
        self.assertEqual(self.remote_checklists, [])
        self.assertEqual(self.writes, ["/cards"])

    async def test_chinese_checklist_items_survive_actual_json_transport(self):
        result = await self.submit(["整理数据", "审核", "发给负责人"])
        self.assertEqual(result["phase"], "complete")
        self.assertEqual(
            result["verification"], {"description": "verified", "checklist": "verified"}
        )
        self.assertEqual(self.remote_checklists[0]["name"], "任务清单")
        self.assertEqual(
            [item["name"] for item in self.remote_checklists[0]["checkItems"]],
            ["整理数据", "审核", "发给负责人"],
        )
        self.assertEqual(len(self.writes), 5)

    async def test_2xx_without_description_does_not_report_complete_or_repost(self):
        self.drop_description = True
        result = await self.submit([])
        self.assertEqual(result["phase"], "partial")
        self.assertEqual(result["verification"]["description"], "mismatch")
        self.assertIn("描述：缺失或与草稿不一致", storage.receipt_message(result))
        with self.assertRaises(trello.TrelloError):
            await self.submitter.commit(
                "scope",
                models.Draft(**draft_json()),
                [],
                target(),
                "op12345678",
                "ABCDEF-1",
                "time",
            )
        self.assertEqual(self.writes, ["/cards"])

    async def test_2xx_without_checkitems_does_not_report_complete(self):
        self.drop_checkitems = True
        result = await self.submit(["审核"])
        self.assertEqual(result["phase"], "partial")
        self.assertEqual(
            result["verification"], {"description": "verified", "checklist": "mismatch"}
        )
        self.assertIn("任务清单：缺失或与草稿不一致", storage.receipt_message(result))

    async def test_readback_failure_can_be_reconciled_without_another_write(self):
        self.fail_reads = True
        result = await self.submit([])
        self.assertEqual(result["phase"], "partial")
        self.assertEqual(result["verification"]["description"], "unverified")
        self.fail_reads = False
        self.assertIn("描述：已回读核对", await self.submitter.reconcile("scope"))
        self.assertEqual(self.store.get("scope")["phase"], "complete")
        self.assertEqual(self.writes, ["/cards"])

    async def test_stream_json(self):
        self.assertEqual(await self.client.request("GET", "/stream"), {"id": "abc", "ok": True})

    async def test_download_then_multipart_upload(self):
        picture = await self.stager.stage(Image(url=self.base + "/image"), 1)
        await self.client.add_picture("d" * 24, picture.path, picture.name, picture.mime)
        self.assertEqual(
            self.received, {"data": PNG, "name": "image-1.png", "label": "image-1.png"}
        )
        self.stager.remove([picture])
