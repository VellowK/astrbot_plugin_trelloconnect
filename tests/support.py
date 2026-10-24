"""Minimal AstrBot SDK doubles; tests do not claim live platform coverage."""

import importlib
import importlib.util
import logging
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "_trelloconnect_test"


class Plain:
    def __init__(self, text):
        self.text = text


class Image:
    def __init__(self, file="", url="", path=""):
        self.file, self.url, self.path = file, url, path


class Reply:
    def __init__(self, chain=None, message_str=""):
        self.chain, self.message_str = chain or [], message_str


class Star:
    def __init__(self, context):
        self.context = context


class Filters:
    EventMessageType = types.SimpleNamespace(ALL="all")

    @staticmethod
    def event_message_type(*args, **kwargs):
        return lambda fn: fn


if PACKAGE not in sys.modules:
    modules = {
        "astrbot": {},
        "astrbot.api": {"AstrBotConfig": dict, "logger": logging.getLogger("test_trello")},
        "astrbot.api.event": {"AstrMessageEvent": object, "filter": Filters},
        "astrbot.api.message_components": {"Image": Image, "Plain": Plain, "Reply": Reply},
        "astrbot.api.star": {"Context": object, "Star": Star},
        "astrbot.core": {},
        "astrbot.core.utils": {},
        "astrbot.core.utils.astrbot_path": {"get_astrbot_data_path": lambda: "unused"},
    }
    for name, values in modules.items():
        module = types.ModuleType(name)
        module.__dict__.update(values)
        sys.modules[name] = module
    spec = importlib.util.spec_from_file_location(
        PACKAGE, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE] = module
    spec.loader.exec_module(module)


def load(name):
    return importlib.import_module(PACKAGE + "." + name)


def target():
    return {"workspace_id": "a" * 24, "board_id": "b" * 24, "label": "团队 / 项目"}


def draft_json(**overrides):
    return {
        "title": "提交报告",
        "description": "整理数据，审核后提交报告。",
        "checklist": ["整理数据", "审核", "提交报告"],
        "due": None,
        "due_note": "",
        "image_ids": [],
        "questions": [],
        **overrides,
    }


def client_mock():
    return types.SimpleNamespace(
        verify=AsyncMock(return_value=target()),
        ensure_reminder=AsyncMock(return_value="c" * 24),
        create_card=AsyncMock(
            return_value={"id": "d" * 24, "shortUrl": "https://trello.com/c/abcd1234"}
        ),
        add_checklist=AsyncMock(),
        add_picture=AsyncMock(),
        find_operation=AsyncMock(return_value=[]),
        close=AsyncMock(),
    )


class Event:
    def __init__(
        self,
        text="",
        parts=None,
        sender="alice",
        umo="test:FriendMessage:alice",
        admin=True,
        private=True,
        wake=True,
    ):
        self.text, self.sender, self.unified_msg_origin = text, sender, umo
        self.parts = parts if parts is not None else [Plain(text)]
        self.admin, self.private, self.is_at_or_wake_command = admin, private, wake
        self.stopped, self.llm = False, True

    def get_messages(self):
        return self.parts

    def get_sender_id(self):
        return self.sender

    def get_self_id(self):
        return "bot"

    def get_platform_id(self):
        return "test"

    def is_admin(self):
        return self.admin

    def is_private_chat(self):
        return self.private

    def should_call_llm(self, value):
        self.llm = value

    def stop_event(self):
        self.stopped = True

    def plain_result(self, text):
        return text
