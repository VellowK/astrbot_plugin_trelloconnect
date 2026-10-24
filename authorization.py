"""Browser consent guidance; never receive or display a user's token in chat."""

from urllib.parse import urlencode

KEY_URL = "https://trello.com/apps/admin"
DOCS_URL = "https://developer.atlassian.com/cloud/trello/guides/rest-api/authorization/"


def authorization_help(api_key: str) -> str:
    lines = [
        "Trello 授权（仅链接引导，不会自动回填 Token）",
        "API Key 申请 / 管理地址：",
        KEY_URL,
        "登录后创建或选择一个 Power-Up，在 Trello Auth / API Key 页面生成 Key。无需 Application Secret。",
    ]
    api_key = api_key.strip()
    if not api_key:
        lines.append(
            "先把 Key 填入 AstrBot 插件设置的 api_key，保存并重载，然后私聊 /trello 授权。"
        )
    else:
        query = urlencode(
            {
                "expiration": "30days",
                "name": "AstrBotTrelloConnect",
                "scope": "read,write",
                "response_type": "token",
                "key": api_key,
            }
        )
        lines.extend(
            [
                "点击以下链接，在 Trello 核对账号并允许授权（读写权限，有效期 30 天）：",
                "https://trello.com/1/authorize?" + query,
                "将授权页面显示的 Token 复制到 AstrBot 插件设置的 token，保存并重载。到期后重新授权。",
            ]
        )
    lines.extend(
        [
            "不要把 Token 发给 Bot、其他人或放在公开截图中；本命令不读取或输出已配置的 Token。",
            "完成后私聊 /trello 工作区、/trello 看板，并用 /trello 检查 核对配置。",
            "官方说明：" + DOCS_URL,
        ]
    )
    return "\n".join(lines)
