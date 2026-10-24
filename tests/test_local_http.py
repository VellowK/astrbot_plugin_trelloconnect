"""Exercise aiohttp streaming/multipart against a loopback fixture, not Trello."""

import tempfile
import unittest
from pathlib import Path

from aiohttp import web

from tests.support import Image, load

images, trello = load("images"), load("trello")
PNG = b"\x89PNG\r\n\x1a\nfixture"


class LocalHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.received = None
        app = web.Application()
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

    async def test_stream_json(self):
        self.assertEqual(await self.client.request("GET", "/stream"), {"id": "abc", "ok": True})

    async def test_download_then_multipart_upload(self):
        picture = await self.stager.stage(Image(url=self.base + "/image"), 1)
        await self.client.add_picture("d" * 24, picture.path, picture.name, picture.mime)
        self.assertEqual(
            self.received, {"data": PNG, "name": "image-1.png", "label": "image-1.png"}
        )
        self.stager.remove([picture])
