"""AstrBot entry point: per-user drafts, explicit revision-bound confirmation."""

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain, Reply
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .images import ImageError, ImageStager
from .intent import Intent, parse_intent
from .models import Draft, DraftError, Picture, render_draft
from .storage import UNRESOLVED, ReceiptStore, receipt_message
from .submission import Submitter
from .summarizer import Summarizer
from .trello import TrelloClient, TrelloError

HELP = """Trello Connect
• 帮我创建待办：明天下午3点前提交报告，先整理数据再审核（可附图片）
• 创建trello卡片（引用含任务/图片的消息，或下一条发送内容）
• 草稿期间直接说修改意见，或追加图片；不会直接创建。
• 核对草稿后，单独回复“确认创建 草稿编号”。每次修改编号都会变化。
• /trello 创建 正文；/trello 取消；/trello 状态；/trello 核对
• 管理员：/trello 工作区；/trello 看板；/trello 检查
在插件设置填写 API Key、Token、工作区 ID 和看板 ID。
默认仅允许 AstrBot 管理员私聊；普通用户需加入 allowed_users。
文字/图片会发送给当前会话模型；正式确认后，所选内容与图片上传 Trello。"""


def number(config, key: str, default: int, low: int, high: int) -> int:
    try:
        return min(high, max(low, int(config.get(key, default))))
    except (TypeError, ValueError):
        return default


@dataclass
class State:
    touched: float = field(default_factory=time.monotonic)
    waiting: bool = False
    busy: bool = False
    draft: Draft | None = None
    pictures: list[Picture] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    source: str = ""
    created_at: str = ""
    target: dict = field(default_factory=dict)
    operation: str = field(default_factory=lambda: uuid.uuid4().hex)
    revision: int = 0

    @property
    def code(self):
        return f"{self.operation[:6].upper()}-{self.revision}"


class TrelloConnectPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.ttl = number(config, "draft_timeout_minutes", 20, 5, 120) * 60
        self.max_images = number(config, "max_images", 5, 1, 10)
        self.max_states = number(config, "max_active_drafts", 20, 1, 100)
        self.timeout = number(config, "request_timeout", 40, 10, 120)
        self.client = TrelloClient(
            str(config.get("api_key", "")),
            str(config.get("token", "")),
            str(config.get("workspace_id", "")),
            str(config.get("board_id", "")),
            self.timeout,
        )
        root = Path(get_astrbot_data_path()) / "plugin_data" / "astrbot_plugin_trelloconnect"
        self.stager = ImageStager(
            root / "images",
            number(config, "max_image_mb", 5, 1, 10) * 1024 * 1024,
            self.timeout,
            bool(config.get("allow_private_images", False)),
        )
        self.store = ReceiptStore(root / "receipts")
        self.submitter = Submitter(self.client, self.store)
        self.summarizer = Summarizer(
            context,
            number(config, "model_timeout", 90, 15, 180),
            bool(config.get("vision_enabled", True)),
        )
        self.states: dict[str, State] = {}
        self.slots = asyncio.Semaphore(3)
        self.cleaner: asyncio.Task | None = None
        self.last_model: dict[str, float] = {}
        try:
            self.zone = ZoneInfo(str(config.get("timezone", "Asia/Shanghai")))
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(
                "timezone 配置无效；Windows 请确保已安装 requirements.txt 中的 tzdata。"
            ) from None

    def _drop(self, scope: str):
        state = self.states.pop(scope, None)
        if state:
            self.stager.remove(state.pictures)

    def _prune(self):
        now = time.monotonic()
        for scope, state in list(self.states.items()):
            if not state.busy and now - state.touched > self.ttl:
                self._drop(scope)
        for scope, touched in list(self.last_model.items()):
            if now - touched > self.ttl:
                self.last_model.pop(scope, None)
        # Orphans from process restarts are removed after the same retention TTL.
        # While any operation is active, new staged paths might not be in a state yet.
        if not any(s.busy for s in self.states.values()):
            active = {p.path for s in self.states.values() for p in s.pictures}
            for path in self.stager.root.glob("image_*"):
                if (
                    path.is_file()
                    and not path.is_symlink()
                    and path not in active
                    and time.time() - path.stat().st_mtime > self.ttl
                ):
                    path.unlink(missing_ok=True)

    async def _maintenance(self):
        while True:
            try:
                self._prune()
            except OSError:
                logger.warning("Trello 图片缓存清理失败；请检查插件数据目录权限。")
            await asyncio.sleep(60)

    async def initialize(self):
        if not self.cleaner:
            self.cleaner = asyncio.create_task(self._maintenance())

    @staticmethod
    def _quoted(parts: list) -> tuple[str, list]:
        texts, pictures = [], []
        for part in parts:
            if isinstance(part, Reply):
                chain = part.chain or []
                text = "".join(item.text for item in chain if isinstance(item, Plain))
                texts.append(text or part.message_str or "")
                pictures.extend(item for item in chain if isinstance(item, Image))
        return "\n".join(texts).strip(), pictures

    def _allowed(self, event) -> bool:
        if event.is_admin():
            return True
        allowed = {str(value) for value in self.config.get("allowed_users", [])}
        return (
            event.get_sender_id() in allowed
            or f"{event.get_platform_id()}:{event.get_sender_id()}" in allowed
        )

    def _preview(self, state: State) -> str:
        return render_draft(
            state.draft,
            state.code,
            state.created_at,
            state.target["label"],
            state.pictures,
            state.warnings,
        )

    async def _draft(self, scope: str, state: State, event, text: str, components: list) -> str:
        if not text.strip() and not components:
            state.waiting = True
            return "请在下一条消息发送任务要求和相关图片，或引用任务消息后再次创建。"
        if len(text) > 12000:
            return "任务或修改意见超过 12000 字，请缩短后重试。"
        if len(components) + len(state.pictures) > self.max_images:
            return f"一个草稿最多 {self.max_images} 张图片，请减少图片后重试。"
        if state.revision >= 30:
            return "本草稿已修改 30 次，请取消后重新创建。"
        cooldown = number(self.config, "model_cooldown_seconds", 2, 0, 60)
        if time.monotonic() - self.last_model.get(scope, float("-inf")) < cooldown:
            return "修改过于频繁，请稍等片刻再发。"
        self.last_model[scope] = time.monotonic()
        new_pictures = []
        try:
            # Validate target before sending task data to a model. This is read-only.
            target = state.target or await self.client.verify()
            for index, component in enumerate(components, len(state.pictures) + 1):
                new_pictures.append(await self.stager.stage(component, index))
            pictures = state.pictures + new_pictures
            created = state.created_at or datetime.now(self.zone).isoformat(timespec="seconds")
            source = state.source if state.draft else text.strip()
            draft, warnings = await self.summarizer.generate(
                event.unified_msg_origin,
                source,
                text.strip() if state.draft else "",
                state.draft,
                pictures,
                created,
                datetime.now(self.zone),
            )
        except BaseException:
            self.stager.remove(new_pictures)
            raise
        state.source, state.created_at, state.target = source, created, target
        state.draft, state.pictures = draft, pictures
        state.warnings = warnings
        state.waiting = False
        state.revision += 1
        return self._preview(state)

    async def _handle(
        self, scope: str, state: State, event, intent: Intent, text: str, images: list
    ) -> str:
        action = intent.action
        if action == "help":
            return HELP
        if action == "cancel":
            self._drop(scope)
            return "已取消本地草稿，未创建新卡片。之前已提交的卡片不会删除。"
        if action in {"check", "workspaces", "boards"}:
            if not event.is_admin():
                return "此配置辅助命令仅限 AstrBot 管理员。"
            if action == "check":
                target = await self.client.verify()
                return f"授权可读取目标：{target['label']}\n工作区/看板匹配。reminder 将在首次正式确认时自动创建或复用；写权限以实际提交结果为准。"
            values = await (
                self.client.workspaces() if action == "workspaces" else self.client.boards()
            )
            title = (
                "工作区（把 ID 填入插件设置）"
                if action == "workspaces"
                else "看板（把 ID 填入插件设置）"
            )
            rows = [
                f"{item.get('displayName') or item.get('name', '')} | {item['id']}"
                for item in values[:50]
            ]
            return (
                title
                + "\n"
                + ("\n".join(rows) or "没有可用项目。")
                + ("\n仅显示前 50 项。" if len(values) > 50 else "")
            )
        if action == "status":
            return self._preview(state) if state.draft else receipt_message(self.store.get(scope))
        if action == "reconcile":
            return await self.submitter.reconcile(scope)
        if action == "clear":
            receipt = self.store.get(scope)
            if not receipt or receipt.get("phase") not in UNRESOLVED:
                return receipt_message(receipt)
            if receipt.get("url"):
                return receipt_message(receipt)
            if intent.text.strip() != receipt["operation"][-8:]:
                return "请先 /trello 核对，并手动检查未创建卡片后，使用返回的完整清除指令。清除不会删除任何远端内容。"
            receipt["phase"] = "cleared"
            self.store.save(scope, receipt)
            self._drop(scope)
            return "已清除未决锁定。远端内容未删除；请确认没有旧卡片后再新建，以免重复。"
        if action in {"confirm", "confirmation_hint"}:
            if not state.draft:
                return receipt_message(self.store.get(scope))
            if images:
                return "确认消息附带了新图片。请先单独发送图片修改草稿，再确认新版编号。"
            if action != "confirm" or intent.text.upper() != state.code:
                return f"为避免误提交，请核对最新草稿后单独回复：确认创建 {state.code}"
            if state.draft.questions:
                return "草稿还有需要澄清的问题，请先回复：" + "；".join(state.draft.questions)
            # All selected local images must still exist before any Trello write.
            if any(
                not picture.path.is_file()
                for picture in state.pictures
                if picture.id in state.draft.image_ids
            ):
                return "草稿图片已不可用，请取消后重新发送图片创建。"
            result = await self.submitter.commit(
                scope,
                state.draft,
                state.pictures,
                state.target,
                state.operation,
                state.code,
                state.created_at,
            )
            self._drop(scope)
            return (
                "已创建 Trello 卡片。\n"
                if result["phase"] == "complete"
                else "卡片已创建，但清单/图片未完整写入。\n"
            ) + receipt_message(result)
        if action == "create":
            receipt = self.store.get(scope)
            if receipt and receipt.get("phase") in UNRESOLVED:
                return receipt_message(receipt)
            if state.draft:
                return (
                    "你已有待确认草稿。请直接发送修改意见；若要另建，请先确认或取消当前草稿。\n"
                    + self._preview(state)
                )
            return await self._draft(scope, state, event, intent.text, images)
        return await self._draft(scope, state, event, text, images)

    @filter.event_message_type(filter.EventMessageType.ALL, priority=90)
    async def on_message(self, event: AstrMessageEvent):
        if event.get_sender_id() == event.get_self_id() or not event.get_sender_id():
            return
        self._prune()
        scope = json.dumps([event.unified_msg_origin, event.get_sender_id()], ensure_ascii=False)
        state = self.states.get(scope)
        parts = event.get_messages()
        text = "".join(part.text for part in parts if isinstance(part, Plain)).strip()
        intent = parse_intent(text)
        if not state and (
            not intent or (not intent.explicit and intent.action in {"cancel", "confirmation_hint"})
        ):
            return
        if (
            intent
            and not intent.explicit
            and not self.config.get("natural_language", True)
            and not state
        ):
            return
        if not event.is_private_chat() and not self.config.get("enable_group", False):
            return
        if (
            not event.is_private_chat()
            and not state
            and not (intent and intent.explicit)
            and self.config.get("group_require_at", True)
            and not event.is_at_or_wake_command
        ):
            return
        if not self._allowed(event):
            if intent and intent.explicit:
                event.should_call_llm(False)
                yield event.plain_result(
                    "没有使用此 Trello 账号的权限，请管理员配置 allowed_users。"
                )
                event.stop_event()
            return
        if intent is None:
            if not state or not (state.waiting or state.draft):
                return
            # Do not hijack other plugins' slash commands while awaiting edits.
            if text.startswith("/"):
                return
            intent = Intent("edit", text)
        event.should_call_llm(False)
        if state and state.busy:
            yield event.plain_result("上一个 Trello 请求仍在处理中，请勿重复提交。")
            event.stop_event()
            return
        if not state:
            if len(self.states) >= self.max_states:
                yield event.plain_result("当前草稿数量达到上限，请稍后再试。")
                event.stop_event()
                return
            state = State()
            self.states[scope] = state
        state.busy = True
        try:
            images = [part for part in parts if isinstance(part, Image)]
            if intent.action == "create" or state.waiting or intent.action == "edit":
                quote, quoted_images = self._quoted(parts)
                images = quoted_images + images
                if quote:
                    body = intent.text if intent.action == "create" else text
                    body = (body + "\n\n引用的任务资料：\n" + quote).strip()
                    if intent.action == "create":
                        intent = Intent("create", body, intent.explicit)
                    else:
                        text = body
            async with self.slots:
                response = await self._handle(scope, state, event, intent, text, images)
            yield event.plain_result(response)
        except (TrelloError, DraftError, ImageError) as exc:
            receipt = self.store.get(scope)
            message = str(exc)
            if receipt and receipt.get("phase") in UNRESOLVED:
                message += "\n" + receipt_message(receipt)
                self._drop(scope)
            yield event.plain_result(message)
        except OSError:
            logger.warning("Trello 本地文件/提交回执操作失败；未自动重试。")
            yield event.plain_result(
                "本地文件或提交回执保存失败。请 /trello 状态，必要时 /trello 核对；勿重复提交。"
            )
        except Exception as exc:
            logger.warning(f"Trello 请求异常：{type(exc).__name__}（不记录任务或凭据）")
            yield event.plain_result(
                "处理异常，未自动重试。若发生在提交时请先 /trello 状态 和 /trello 核对。"
            )
        finally:
            state.busy = False
            state.touched = time.monotonic()
            # Help/config/status requests need no long-lived in-memory state.
            if self.states.get(scope) is state and not state.draft and not state.waiting:
                self._drop(scope)
            event.stop_event()

    async def terminate(self):
        if self.cleaner:
            self.cleaner.cancel()
            try:
                await self.cleaner
            except asyncio.CancelledError:
                pass
        for scope in list(self.states):
            self._drop(scope)
        await self.stager.close()
        await self.client.close()
