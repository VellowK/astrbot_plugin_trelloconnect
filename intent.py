"""Fast request recognition, bounded semantic candidates and whole-message confirmations."""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Intent:
    action: str
    text: str = ""
    explicit: bool = False


PREFIX = r"(?:(?:请|麻烦|劳烦|顺便|能不能|能|可以|帮我|帮忙|给我|替我)\s*)*"
CREATE = re.compile(
    r"^"
    + PREFIX
    + r"(?:创建|新建|添加|新增|建|记)(?:\s*(?:一个|一条|个|条))?\s*(?:trello\s*(?:卡片|待办|代办)?|待办|代办|任务卡片)(?:\s*(?:一下|吧))?",
    re.I,
)
REVERSE = re.compile(
    r"^"
    + PREFIX
    + r"(?:把|将)\s*(?:这条消息|这段话|这些内容|这个消息|这个任务)\s*(?:添加到|加入|记到|放到)\s*trello",
    re.I,
)


CONFIRMATIONS = {
    "确认",
    "确定",
    "确认创建",
    "确认添加",
    "确认提交",
    "正式确认",
    "没问题",
    "没问题了",
    "可以",
    "可以了",
    "可以的",
    "好",
    "好的",
    "行",
    "行了",
    "行吧",
    "就这样",
    "就这样吧",
    "就按这个",
    "确认吧",
    "创建吧",
    "添加吧",
    "提交吧",
    "ok",
    "ok了",
    "okay",
    "yes",
}


def is_creation_candidate(text: str) -> bool:
    """Only task/Trello-related messages may use the semantic fallback, never slash commands."""
    if not text.strip() or len(text) > 12000 or text.lstrip().startswith("/"):
        return False
    return bool(
        re.search(r"trello|待办|代办|任务", text, re.I)
        and re.search(r"创|建|添|加|记|录|写|存|放|列|安排|整理|转|做|create|add|save", text, re.I)
    )


def parse_intent(text: str) -> Intent | None:
    text = text.strip()
    command = re.match(r"^/?trello(?:\s+|$)(.*)$", text, re.I | re.S)
    if command:
        rest = command[1].strip()
        parts = rest.split(maxsplit=1)
        action = parts[0].lower() if parts else "帮助"
        payload = parts[1] if len(parts) > 1 else ""
        actions = {
            "创建": "create",
            "新建": "create",
            "添加": "create",
            "新增": "create",
            "add": "create",
            "create": "create",
            "帮助": "help",
            "help": "help",
            "取消": "cancel",
            "cancel": "cancel",
            "确认": "confirm",
            "状态": "status",
            "核对": "reconcile",
            "检查": "check",
            "授权": "authorize",
            "登录": "authorize",
            "auth": "authorize",
            "login": "authorize",
            "工作区": "workspaces",
            "看板": "boards",
            "清除未决": "clear",
        }
        if action in actions or text.startswith("/"):
            return Intent(actions.get(action, "help"), payload, True)
    if text in {"取消", "取消创建", "取消待办", "取消代办", "取消trello"}:
        return Intent("cancel")
    confirmed = re.fullmatch(r"确认(?:创建|添加)?\s+([A-Za-z0-9]+-[0-9]+)[。！!]?", text)
    if confirmed:
        return Intent("confirm", confirmed[1].upper())
    if text.rstrip("。.!！～~").strip().lower() in CONFIRMATIONS:
        return Intent("confirm")
    match = CREATE.match(text) or REVERSE.match(text)
    if match:
        tail = text[match.end() :].lstrip(" \t：:，,\n")
        if tail in {"这条消息", "这个消息", "这段话", "这些内容", "这个任务"}:
            tail = ""
        return Intent("create", tail)
    return None
