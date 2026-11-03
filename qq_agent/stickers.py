import json
from importlib.resources import as_file, files
from pathlib import PurePosixPath

from image_assets import read_image

from .prompts import JSON_DUPLICATE, STICKER_INSTRUCTIONS


def unique_names(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(JSON_DUPLICATE.format(key=key))
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
            if filename is None:
                stickers[name] = None
                continue
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
    return STICKER_INSTRUCTIONS + json.dumps(list(stickers), ensure_ascii=False)
