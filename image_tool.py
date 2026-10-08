import json
import socket
import sys


MEMBER_FIELDS = ("称呼", "表达风格", "兴趣", "互动偏好", "不确定印象")
MEMBER_TOOLS = [
    {
        "name": "list_profiles",
        "description": "从数据库读取当前群全部成员最新的聊天偏好，可在群内核对。按用户 ID 区分成员，尽可能遵守其聊天偏好。",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "replace_profile",
        "description": "完整替换当前群指定成员的聊天偏好。按 user_id 区分成员，只记录本人明确表达或有依据的持续互动，不把第三方转述当成事实。保留有效字段，最多 500 字符；空对象删除。仅成功返回才表示已保存。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "user_id": {
                    "type": "string",
                    "description": "当前群成员的 QQ 号，不是昵称。",
                },
                "profile": {
                    "type": "object",
                    "properties": {name: {"type": "string"} for name in MEMBER_FIELDS},
                    "additionalProperties": False,
                },
            },
            "required": ["user_id", "profile"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_group_profile",
        "description": "读取当前群最新共同兴趣和互动习惯，作为聊天背景，不是手工群设定。",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "replace_group_profile",
        "description": "完整替换当前群共同偏好摘要，最多 1000 字符。保留有效信息，不把个人偏好推成全群共识，不保存身份信息或秘密；空字符串删除。仅成功返回才表示已保存。",
        "inputSchema": {
            "type": "object",
            "properties": {"profile": {"type": "string", "maxLength": 1000}},
            "required": ["profile"],
            "additionalProperties": False,
        },
    },
]


TOOLS = [
    {
        "name": "list_images",
        "description": "查询当前 QQ 会话已自动保存到工作区的生成图片，按生成顺序返回 ID、路径和大小（最近 100 张）。跨轮和重启后仍可查询。用真实路径加工中间帧或发送原图；无需自行解码 Base64。",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "send_image",
        "description": "将当前会话工作区中的 PNG/JPEG/GIF/WebP 文件上传到 QQ（最大 32 MiB）。用户无法访问本地路径，工具预览也不是 QQ 交付。path 可选：省略时发送最新生成原图；合成动图时必须指定最终 GIF 路径，不要发送中间帧。不需要 shell、Base64 或 SVG 包装。仅成功回执表示已发送；结果未知时不要自动重试。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "当前工作区的相对路径或绝对路径。",
                },
                "resend": {
                    "type": "boolean",
                    "default": False,
                    "description": "仅用户明确要求再次发送同一图片时设置。",
                },
            },
            "additionalProperties": False,
        },
    },
]


def call(path, session, action, arguments, token=None):
    with socket.socket(socket.AF_UNIX) as client:
        # The host waits up to 30 seconds for NapCat's acknowledgement.
        client.settimeout(40)
        client.connect(path)
        client.sendall(
            (
                json.dumps(
                    {
                        "action": action,
                        "session": session,
                        **arguments,
                        **({"token": token} if token is not None else {}),
                    }
                )
                + "\n"
            ).encode()
        )
        with client.makefile("r") as stream:
            result = json.loads(stream.readline())
    if result.get("ok"):
        return {
            "content": [
                {"type": "text", "text": json.dumps(result, ensure_ascii=False)}
            ],
            "structuredContent": result,
        }
    return {
        "content": [{"type": "text", "text": result.get("error", "图片发送失败。")}],
        "isError": True,
    }


def main():
    path = sys.argv[1]
    session = sys.argv[2] if len(sys.argv) > 2 else None
    members = len(sys.argv) == 5 and sys.argv[3] == "--members"
    token = sys.argv[4] if members else None
    tools = MEMBER_TOOLS if members else TOOLS
    for line in sys.stdin:
        request = {}
        try:
            request = json.loads(line)
            if "id" not in request:
                continue
            method = request.get("method")
            if method == "initialize":
                result = {
                    "protocolVersion": request["params"]["protocolVersion"],
                    "capabilities": {"tools": {}},
                    "serverInfo": {
                        "name": "qq-member" if members else "qq-image",
                        "version": "2",
                    },
                }
            elif method == "tools/list":
                result = {"tools": tools}
            elif method == "tools/call":
                params = request["params"]
                name = params.get("name")
                arguments = params.get("arguments", {})
                allowed = (
                    (
                        {"profile", "user_id"}
                        if name == "replace_profile"
                        else {"profile"}
                        if name == "replace_group_profile"
                        else set()
                    )
                    if members
                    else ({"path", "resend"} if name == "send_image" else set())
                )
                if (
                    name not in {tool["name"] for tool in tools}
                    or not isinstance(arguments, dict)
                    or arguments.keys() - allowed
                ):
                    raise ValueError("Unknown tool or invalid arguments")
                try:
                    result = call(
                        path,
                        session,
                        name,
                        arguments,
                        **({"token": token} if members else {}),
                    )
                except (OSError, ValueError):
                    result = {
                        "content": [
                            {
                                "type": "text",
                                "text": "偏好工具连接失败或响应超时；保存结果未知，不要声称已保存。"
                                if members
                                else "图片工具连接失败或响应超时；发送结果可能未知，不要自动重试或声称已发送。",
                            }
                        ],
                        "isError": True,
                    }
            else:
                raise ValueError("Unknown method")
            response = {"jsonrpc": "2.0", "id": request["id"], "result": result}
        except Exception as error:
            response = {
                "jsonrpc": "2.0",
                "id": request.get("id"),
                "error": {"code": -32603, "message": str(error)},
            }
        print(json.dumps(response), flush=True)


if __name__ == "__main__":
    main()
