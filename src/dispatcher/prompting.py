from __future__ import annotations

import json
from importlib import resources
from typing import Any


def load_prompt(group: str, name: str) -> str:
    """从 dispatcher.prompts 包中读取指定分组下的 prompt 模板。"""
    return resources.files("dispatcher.prompts").joinpath(group).joinpath(name).read_text(encoding="utf-8")


def render_prompt(template: str, replacements: dict[str, str]) -> str:
    """把模板中的 {key} 占位符替换为 replacements 中对应的文本。"""
    text = template
    for key, value in replacements.items():
        # 这里使用简单字符串替换，要求模板占位符格式固定为 {key}。
        text = text.replace("{" + key + "}", value)
    return text


def format_fact_ids(fact_ids: list[str]) -> str:
    """把事实 ID 列表格式化为适合放进 prompt 的 JSON 文本。"""
    return format_json_block(fact_ids)


def format_open_intents(intents: list[dict[str, Any]]) -> str:
    """把未关闭的意图列表格式化为适合放进 prompt 的 JSON 文本。"""
    return format_json_block(intents)


def format_hints(hints: list[dict[str, Any]]) -> str:
    """把提示信息列表格式化为适合放进 prompt 的 JSON 文本。"""
    return format_json_block(hints)


def format_json_block(value: Any) -> str:
    """把任意 Python 值转换为缩进后的 JSON 字符串，并保留中文字符。"""
    return json.dumps(value, ensure_ascii=False, indent=2)
