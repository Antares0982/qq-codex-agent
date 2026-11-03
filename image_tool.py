import json
import socket
import sys


GROUP_TOOLS = [
    {
        "name": "view_image",
        "description": "按当前群消息中的 image_id 或 sticker_id 查看图片或表情（都传入 image_id 参数），同时将原文件下载到当前工作区。返回图片预览及真实路径；动画预览首帧，原文件保留动画。过期图片可能不可用。",
        "inputSchema": {
            "type": "object",
            "properties": {"image_id": {"type": "string"}},
            "required": ["image_id"],
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
        image = result.pop("image", None)
        return {
            "content": [
                {"type": "text", "text": json.dumps(result, ensure_ascii=False)}
            ]
            + ([image] if image else []),
            "structuredContent": result,
        }
    return {
        "content": [{"type": "text", "text": result.get("error", "图片发送失败。")}],
        "isError": True,
    }


def main():
    path = sys.argv[1]
    session = sys.argv[2] if len(sys.argv) > 2 else None
    group = len(sys.argv) == 5 and sys.argv[3] == "--group"
    token = sys.argv[4] if group else None
    tools = TOOLS + (GROUP_TOOLS if group else [])
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
                        "name": "qq-image",
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
                    {"path", "resend"}
                    if name == "send_image"
                    else {"image_id"}
                    if name == "view_image"
                    else set()
                )
                if (
                    name not in {tool["name"] for tool in tools}
                    or not isinstance(arguments, dict)
                    or arguments.keys() - allowed
                    or (
                        name == "view_image"
                        and not isinstance(arguments.get("image_id"), str)
                    )
                ):
                    raise ValueError("Unknown tool or invalid arguments")
                try:
                    result = call(
                        path,
                        session,
                        name,
                        arguments,
                        **({"token": token} if name == "view_image" else {}),
                    )
                except (OSError, ValueError):
                    result = {
                        "content": [
                            {
                                "type": "text",
                                "text": "图片工具连接失败或响应超时；发送结果可能未知，不要自动重试或声称已发送。",
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
