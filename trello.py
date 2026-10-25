"""Trello API boundary: fixed origin, redacted errors, no write retries."""

import asyncio
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp


class TrelloError(Exception):
    def __init__(self, message: str, uncertain: bool = False):
        super().__init__(message)
        self.uncertain = uncertain


def resource_id(value: str, board: bool = False) -> str:
    value = value.strip()
    if board and value.startswith("https://trello.com/b/"):
        value = urlsplit(value).path.split("/")[2]
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value):
        raise TrelloError("请配置有效的工作区 ID/短名称和看板 ID/短链接。")
    return value


def card_link(card: dict) -> str:
    for field in ("shortUrl", "url"):
        value = card.get(field, "")
        if isinstance(value, str) and re.match(
            r"^https://trello\.com/c/[A-Za-z0-9]+(?:/|$)", value
        ):
            return value
    return "https://trello.com/c/" + str(card.get("id", ""))


def content_fingerprints(description: str, items: list[str]) -> dict:
    """Keep only comparison digests in receipts, never the task text itself."""
    normalized = description.replace("\r\n", "\n").replace("\r", "\n").strip()
    return {
        "description": hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
        "checklist": hashlib.sha256(
            json.dumps(sorted(items), ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        if items
        else None,
    }


def content_verified(result: dict) -> bool:
    return result.get("description") == "verified" and result.get("checklist") in {
        "verified",
        "not_requested",
    }


class TrelloClient:
    BASE = "https://api.trello.com/1"

    def __init__(self, key: str, token: str, workspace: str, board: str, timeout: int = 40):
        self.key, self.token = key.strip(), token.strip()
        self.workspace, self.board = workspace.strip(), board.strip()
        self.timeout = timeout
        self.session: aiohttp.ClientSession | None = None
        self.list_lock = asyncio.Lock()

    async def close(self):
        if self.session:
            await self.session.close()
            self.session = None

    async def request(
        self, method: str, path: str, *, params: dict | None = None, data=None, json_data=None
    ):
        if not self.key or not self.token:
            raise TrelloError(
                "请先在插件设置填写 Trello API Key 和 Token；管理员可私聊 /trello 授权 获取申请地址与授权链接。"
            )
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout), trust_env=False
            )
        query = {**(params or {}), "key": self.key, "token": self.token}
        try:
            async with self.session.request(
                method,
                self.BASE + path,
                params=query,
                data=data,
                allow_redirects=False,
                **({"json": json_data} if json_data is not None else {}),
            ) as response:
                if response.status < 200 or response.status >= 300:
                    hints = {
                        401: "授权失效，请检查 API Key/Token；管理员可私聊 /trello 授权 重新获取授权链接。",
                        403: "没有工作区或看板的读写权限。",
                        404: "目标不存在或当前账号无权访问。",
                        429: "请求过于频繁，请稍后再试。",
                    }
                    raise TrelloError(
                        hints.get(response.status, f"Trello 请求失败（HTTP {response.status}）。"),
                        uncertain=method != "GET"
                        and (response.status >= 500 or response.status == 408),
                    )
                raw = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    raw.extend(chunk)
                    if len(raw) > 2 * 1024 * 1024:
                        raise TrelloError(
                            "Trello 响应过大，请到看板手动核对。", uncertain=method != "GET"
                        )
                try:
                    return json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    raise TrelloError(
                        "Trello 返回了异常响应，请核对看板。", uncertain=method != "GET"
                    ) from None
        except TrelloError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            raise TrelloError("无法连接 Trello 或请求超时。", uncertain=method != "GET") from None

    @staticmethod
    def object_id(data, *, uncertain=False) -> str:
        if not isinstance(data, dict) or not re.fullmatch(
            r"[a-fA-F0-9]{24}", str(data.get("id", ""))
        ):
            raise TrelloError("Trello 返回了无效的资源 ID。", uncertain=uncertain)
        return data["id"]

    async def workspaces(self) -> list:
        return await self.request(
            "GET", "/members/me/organizations", params={"fields": "id,name,displayName"}
        )

    async def boards(self) -> list:
        org = resource_id(self.workspace)
        return await self.request(
            "GET",
            f"/organizations/{org}/boards",
            params={"filter": "open", "fields": "id,name,url,idOrganization"},
        )

    async def verify(self) -> dict:
        org = await self.request(
            "GET",
            f"/organizations/{resource_id(self.workspace)}",
            params={"fields": "id,name,displayName"},
        )
        board = await self.request(
            "GET",
            f"/boards/{resource_id(self.board, board=True)}",
            params={"fields": "id,name,idOrganization,closed"},
        )
        self.object_id(org)
        self.object_id(board)
        if board.get("closed"):
            raise TrelloError("配置的看板已归档，请选择开放的看板。")
        if board.get("idOrganization") != org["id"]:
            raise TrelloError("配置的看板不属于指定工作区；已阻止写入。")
        return {
            "board_id": board["id"],
            "workspace_id": org["id"],
            "label": f"{org.get('displayName', org.get('name', org['id']))} / {board.get('name', board['id'])}",
        }

    async def ensure_reminder(self, board_id: str) -> str:
        async with self.list_lock:
            lists = await self.request(
                "GET",
                f"/boards/{resource_id(board_id)}/lists",
                params={"filter": "open", "fields": "id,name,closed"},
            )
            matches = [
                item
                for item in lists
                if not item.get("closed")
                and str(item.get("name", "")).strip().casefold() == "reminder"
            ]
            if len(matches) > 1:
                raise TrelloError("看板存在多个 reminder 列表，请先合并或重命名多余列表。")
            if matches:
                return self.object_id(matches[0])
            result = await self.request(
                "POST", "/lists", params={"name": "reminder", "idBoard": board_id, "pos": "bottom"}
            )
            return self.object_id(result, uncertain=True)

    async def create_card(
        self, list_id: str, title: str, description: str, due: str | None
    ) -> dict:
        result = await self.request(
            "POST",
            "/cards",
            json_data={
                "idList": list_id,
                "name": title,
                "desc": description,
                "pos": "bottom",
                **({"due": due} if due else {}),
            },
        )
        self.object_id(result, uncertain=True)
        return result

    async def add_checklist(self, card_id: str, items: list[str]):
        if not items:
            return
        checklist = await self.request(
            "POST",
            f"/cards/{resource_id(card_id)}/checklists",
            json_data={"name": "任务清单", "pos": "bottom"},
        )
        checklist_id = self.object_id(checklist, uncertain=True)
        for item in items:
            await self.request(
                "POST",
                f"/checklists/{checklist_id}/checkItems",
                json_data={"name": item, "pos": "bottom", "checked": False},
            )

    async def verify_content(self, card_id: str, expected: dict) -> dict:
        """Read back description and checklist; a 2xx write alone is not proof of content."""
        card_id = resource_id(card_id)
        result = {
            "description": "unverified",
            "checklist": "unverified" if expected.get("checklist") else "not_requested",
        }
        try:
            card = await self.request("GET", f"/cards/{card_id}", params={"fields": "desc"})
            if (
                isinstance(card, dict)
                and card.get("id") == card_id
                and isinstance(card.get("desc"), str)
            ):
                digest = content_fingerprints(card["desc"], [])["description"]
                result["description"] = (
                    "verified" if digest == expected.get("description") else "mismatch"
                )
        except TrelloError:
            pass
        if not expected.get("checklist"):
            return result
        try:
            checklists = await self.request(
                "GET", f"/cards/{card_id}/checklists", params={"checkItems": "all"}
            )
            if not isinstance(checklists, list):
                return result
            valid = True
            for checklist in checklists:
                if not isinstance(checklist, dict):
                    valid = False
                    continue
                if checklist.get("name") != "任务清单":
                    continue
                items = checklist.get("checkItems")
                if not isinstance(items, list) or any(
                    not isinstance(item, dict) or not isinstance(item.get("name"), str)
                    for item in items
                ):
                    valid = False
                    continue
                digest = content_fingerprints("", [item["name"] for item in items])["checklist"]
                if digest == expected["checklist"]:
                    result["checklist"] = "verified"
                    return result
            if valid:
                result["checklist"] = "mismatch"
        except TrelloError:
            pass
        return result

    async def add_picture(self, card_id: str, path: Path, name: str, mime: str):
        with path.open("rb") as stream:
            form = aiohttp.FormData()
            form.add_field("file", stream, filename=name, content_type=mime)
            form.add_field("name", name)
            await self.request("POST", f"/cards/{resource_id(card_id)}/attachments", data=form)

    async def find_operation(self, board_id: str, operation: str) -> list:
        # Page all cards, including archived/moved-within-board cards. Never assume
        # a truncated first page proves that a timed-out POST did not succeed.
        found, before, seen = [], None, set()
        for _ in range(100):
            params = {"filter": "all", "fields": "id,desc,shortUrl,url", "limit": 1000}
            if before:
                params["before"] = before
            cards = await self.request(
                "GET", f"/boards/{resource_id(board_id)}/cards", params=params
            )
            if not isinstance(cards, list):
                raise TrelloError("无法核对看板卡片；请手动检查。")
            for card in cards:
                if f"astrbot-operation:{operation}" in card.get("desc", ""):
                    found.append(card)
            if len(cards) < 1000:
                return found
            before = cards[-1]["id"]
            if before in seen:
                raise TrelloError("Trello 分页未前进，请手动核对。")
            seen.add(before)
        raise TrelloError("看板过大，无法完整核对；请手动检查操作编号。")
