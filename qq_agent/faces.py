import json
import re
from importlib.resources import files

from .prompts import FACE_ERROR, FACE_LABEL, UNKNOWN_FACE

FACES: dict[str, str] = json.loads(
    files("qq_agent").joinpath("faces.json").read_text(encoding="utf-8")
)


def face_label(identifier):
    return FACE_LABEL.format(
        identifier=identifier, name=FACES.get(identifier, UNKNOWN_FACE)
    )


def parse_faces(text):
    parts = []
    for value in re.split(r"(\[qqface:[^\]]*\])", text):
        if not value:
            continue
        if "[qqface:" not in value:
            parts.append(("text", value))
            continue
        match = re.fullmatch(r"\[qqface:([0-9]{1,10}):([^\[\]\r\n:]+)\]", value)
        if not match or FACES.get(match[1]) != match[2]:
            raise ValueError(FACE_ERROR)
        parts.append(("face", match[1]))
    return parts


def text_chunks(text, faces=False):
    parts = parse_faces(text) if faces else [("text", text)]
    chunks, chunk, size = [], [], 0
    for kind, value in parts:
        while value:
            if size == 1500:
                chunks.append(chunk)
                chunk, size = [], 0
            length = 1 if kind == "face" else min(len(value), 1500 - size)
            chunk.append((kind, value if kind == "face" else value[:length]))
            size += length
            value = "" if kind == "face" else value[length:]
    if chunk:
        chunks.append(chunk)
    return chunks
