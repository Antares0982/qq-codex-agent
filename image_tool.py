import json
import socket
import sys


def send(path):
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(30)
        client.connect(path)
        client.sendall(b'{"action":"send_image"}\n')
        with client.makefile("r") as stream:
            result = json.loads(stream.readline())
    if result.get("ok"):
        return {"content": [{"type": "text", "text": "图片已发送到当前 QQ 会话。"}]}
    return {
        "content": [{"type": "text", "text": result.get("error", "图片发送失败。")}],
        "isError": True,
    }


def main():
    path = sys.argv[1]
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
                    "serverInfo": {"name": "qq-image", "version": "1"},
                }
            elif method == "tools/list":
                result = {
                    "tools": [
                        {
                            "name": "send_image",
                            "description": "将本轮生成的最新图片发送到当前 QQ 会话。仅在希望用户收到图片时调用。",
                            "inputSchema": {"type": "object", "properties": {}},
                        }
                    ]
                }
            elif method == "tools/call" and request["params"].get("name") == "send_image":
                result = send(path)
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
