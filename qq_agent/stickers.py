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
    return (
        "普通群聊任务的每条 assistant 消息（过程说明和最终回复）必须仅包含一个 JSON 对象，"
        '格式为 {"response":"文本","sticker":"表情名称"}，不要代码围栏或其他字段。'
        "response 必填，只能是文本或 null。有文本必须填写 sticker 配置键。"
        '不回复时使用 {"response":null}；仅发表情时 response 为 null，sticker 为图片键。'
        "键对应 null 时不发表情。表情先于文本发送。普通图片仍用 qq_image.send_image。"
        "不得输出原始 JSON 以外的 assistant 文字。应用发起的内部交接总结改用该轮指定的 summary schema，不发送到 QQ。\n表情配置（值表示是否发送图片）："
        + json.dumps(
            {
                key: "图片" if value is not None else None
                for key, value in stickers.items()
            },
            ensure_ascii=False,
        )
    )
