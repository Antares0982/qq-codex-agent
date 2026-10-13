import json
from importlib.resources import as_file, files
from pathlib import PurePosixPath

from image_assets import read_image


def unique_names(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"重复的表情名称：{key}")
        result[key] = value
    return result


def load_stickers():
    with as_file(files("stickers")) as root:
        root = root.resolve(strict=True)
        config = json.loads(
            root.joinpath("sticker_config.json").read_text(encoding="utf-8"),
            object_pairs_hook=unique_names,
        )
        if not isinstance(config, dict):
            raise ValueError("表情配置必须是名称到文件的 JSON 对象。")
        stickers = {}
        for name, filename in config.items():
            if not isinstance(name, str) or not name.strip():
                raise ValueError("表情名称不能为空。")
            if (
                not isinstance(filename, str)
                or not filename
                or PurePosixPath(filename).is_absolute()
            ):
                raise ValueError(f"表情 {name} 必须使用包内相对路径。")
            try:
                _, stickers[name] = read_image(root, filename)
            except ValueError as error:
                raise ValueError(f"表情 {name} 的资源无效：{error}") from error
        return stickers


def sticker_instructions(stickers):
    return (
        "当有合适场景的表情时，尽可能调用表情工具发送表情。"
        "使用 qq_image.send_sticker(name=表情名称)，每 turn 最多发送一次；"
        "同轮追加消息不重置次数，发送失败或结果未知也不能重试。"
        "只有工具返回成功才能声称已发送。普通图片使用 qq_image.send_image。\n"
        "全部可用表情名称：" + json.dumps(list(stickers), ensure_ascii=False)
    )
