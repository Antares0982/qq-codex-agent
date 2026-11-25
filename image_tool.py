import json
import socket
import sys

GENERATION_TIMEOUT = 600
MODELS = ["gpt-image-2.6-flare", "gpt-image-2.6-sunburst"]
FORMATS = ["png", "jpeg", "webp", "gif"]
QUALITIES = ["auto", "low", "medium", "high", "xhigh", "max"]
COMPLEXITIES = ["low", "medium", "high"]
GENERATION_TOOL = {
    "name": "generate_image",
    "description": "用付费 OpenAI GPT-Image 2.6 生成或编辑一张图。普通静态图优先 Codex 内置生图；本工具可直接生成循环 GIF（描述动作和循环方式）、用透明 PNG 标注引导编辑，或分别控制构图规划 complexity 和渲染质量 quality。用户点名 2.6 时也用本工具。Flare 默认，精细编辑可选 Sunburst。参考图用当前工作区真实路径；群图先用 view_image。官方支持 SVG，但本工具不开放。返回原图路径，不会发到 QQ；请调用 send_image。失败或结果未知不要自动重试。",
    "inputSchema": {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "minLength": 1},
            "model": {"type": "string", "enum": MODELS, "default": MODELS[0]},
            "images": {
                "type": "array",
                "maxItems": 5,
                "items": {"type": "string"},
                "description": "按顺序提供 PNG/JPEG/WebP 静态参考图；不传则从文本生成。",
            },
            "markup": {
                "type": "string",
                "description": "与首张参考图显示尺寸一致、含透明像素的 PNG 标注层路径。",
            },
            "output_format": {"type": "string", "enum": FORMATS, "default": "png"},
            "size": {
                "type": "string",
                "description": "auto 或 WIDTHxHEIGHT，宽高均为 16 的倍数。",
            },
            "quality": {"type": "string", "enum": QUALITIES},
            "complexity": {"type": "string", "enum": COMPLEXITIES},
            "frame_count": {
                "type": "integer",
                "minimum": 2,
                "maximum": 16,
                "description": "仅 GIF，默认 9 帧。",
            },
            "frame_duration_ms": {
                "type": "integer",
                "minimum": 20,
                "maximum": 2000,
                "description": "仅 GIF，默认每帧 200 毫秒。",
            },
        },
        "required": ["prompt"],
        "additionalProperties": False,
    },
}

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
        client.settimeout(GENERATION_TIMEOUT + 10 if action == "generate_image" else 40)
        client.connect(path)
        client.sendall(
            (
                json.dumps(
                    {
                        "action": action,
                        "session": session,
                        **arguments,
                        **({"token": token} if token is not None else {}),
                    },
                    ensure_ascii=False,
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
    flags = sys.argv[3:]
    generation = "--generate" in flags
    group = "--group" in flags
    token = flags[-1] if flags else None
    tools = (
        TOOLS
        + (GROUP_TOOLS if group and token != "--group" else [])
        + ([GENERATION_TOOL] if generation else [])
    )
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
                tool = next((tool for tool in tools if tool["name"] == name), None)
                if (
                    tool is None
                    or not isinstance(arguments, dict)
                    or arguments.keys() - tool["inputSchema"]["properties"].keys()
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
                        **(
                            {"token": token}
                            if name in ("view_image", "generate_image")
                            else {}
                        ),
                    )
                except (OSError, ValueError):
                    result = {
                        "content": [
                            {
                                "type": "text",
                                "text": "图片工具连接失败或响应超时；生成或发送结果未知，不要自动重试或声称已发送。",
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
