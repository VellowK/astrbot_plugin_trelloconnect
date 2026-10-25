"""Validated drafts; model output cannot authorize any external side effect."""

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from . import VERSION


class DraftError(ValueError):
    pass


@dataclass(frozen=True)
class Picture:
    id: int
    path: Path
    mime: str
    name: str


@dataclass
class Draft:
    title: str
    description: str
    checklist: list[str]
    due: str | None
    due_note: str
    image_ids: list[int]
    questions: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def _string(value, label: str, maximum: int, required: bool = True) -> str:
    if not isinstance(value, str) or len(value) > maximum:
        raise DraftError(f"模型返回的{label}无效或过长，请重新整理任务。")
    value = value.replace("\x00", "").strip()
    if required and not value:
        raise DraftError(f"模型返回的{label}不能为空，请重新整理任务。")
    return value


def parse_draft(text: str, available_images: list[int]) -> Draft:
    if not isinstance(text, str) or len(text) > 24000:
        raise DraftError("模型输出为空或过长，请重试。")
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        raise DraftError("模型未返回有效的 JSON 草稿；尚未写入 Trello，请重试。") from None
    if not isinstance(data, dict):
        raise DraftError("模型返回的草稿不是对象。")
    title = _string(data.get("title"), "标题", 160)
    description = _string(data.get("description", ""), "任务描述", 6000, required=False)
    items = data.get("checklist", [])
    if not isinstance(items, list) or len(items) > 20:
        raise DraftError("任务清单须为数组，最多 20 项；单一活动可不设清单。")
    checklist = list(dict.fromkeys(_string(item, "清单项", 240) for item in items))
    due = data.get("due")
    note = _string(data.get("due_note", ""), "时间依据", 300, required=False)
    if due is not None:
        try:
            if not isinstance(due, str) or len(due) > 40:
                raise ValueError
            parsed = datetime.fromisoformat(due.replace("Z", "+00:00"))
            if parsed.tzinfo is None or parsed.utcoffset() is None or not note:
                raise ValueError
            due = parsed.isoformat()
        except ValueError:
            raise DraftError(
                "截止时间须为带时区的 ISO 时间，并附原文依据；尚未创建卡片。"
            ) from None
    images = data.get("image_ids", [])
    if not isinstance(images, list) or any(
        type(i) is not int or i not in available_images for i in images
    ):
        raise DraftError("模型选择了不存在的图片；请重试。")
    questions = data.get("questions", [])
    if not isinstance(questions, list) or len(questions) > 5:
        raise DraftError("模型返回的待确认问题格式不正确。")
    questions = [_string(item, "待确认问题", 300) for item in questions]
    return Draft(title, description, checklist, due, note, list(dict.fromkeys(images)), questions)


def render_draft(
    draft: Draft,
    code: str,
    created_at: str,
    destination: str,
    pictures: list[Picture],
    warnings: list[str],
) -> str:
    labels = {p.id: p.name for p in pictures}
    lines = [
        f"Trello 待确认草稿（{code} · v{VERSION}）",
        f"目标：{destination} / reminder",
        f"标题：{draft.title}",
        f"记录时间：{created_at}",
        f"截止时间：{draft.due or '未设置'}",
    ]
    if draft.due_note:
        lines.append(f"时间依据：{draft.due_note}")
    if draft.description:
        lines.extend(["", "描述：", draft.description])
    if draft.checklist:
        lines.extend(["", "任务清单："])
        lines.extend(f"{i}. {item}" for i, item in enumerate(draft.checklist, 1))
    else:
        lines.append("\n任务清单：无（不创建）")
    lines.append(
        "\n将上传的图片：" + ("、".join(f"第{i}张（{labels[i]}）" for i in draft.image_ids) or "无")
    )
    if pictures:
        lines.append("可选图片编号：" + "、".join(str(p.id) for p in pictures))
    if draft.questions:
        lines.append("\n请先补充：" + "；".join(draft.questions))
    lines.extend(f"提示：{warning}" for warning in warnings)
    lines.extend(
        [
            "",
            "可直接回复修改意见，例如：截止改到周五18点 / 不设置截止时间 / 附上第1张图。",
            "正式提交请单独回复：确认 / 可以 / 就这样（无需编号，提交当前草稿）。",
            "取消请回复：取消。确认前不会创建卡片或上传 Trello 附件。",
        ]
    )
    return "\n".join(lines)


def card_description(draft: Draft, created_at: str, operation: str) -> str:
    metadata = f"记录时间：{created_at}\n来源：AstrBot\n操作编号：astrbot-operation:{operation}"
    return f"{draft.description}\n\n---\n{metadata}" if draft.description else metadata
