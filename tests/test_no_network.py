"""默认测试不得访问真实模型服务。"""
import os

import pytest

from skills import llm, vision


def test_api_key_is_not_exposed_to_tests():
    assert os.getenv("DEEPSEEK_API_KEY") is None


def test_vision_network_boundary_is_blocked():
    with pytest.raises(RuntimeError, match="测试禁网"):
        vision._chat_sync([], 1)


def test_llm_network_boundary_is_blocked():
    with pytest.raises(RuntimeError, match="测试禁网"):
        llm._chat_sync([], 1)


async def test_llm_uncertain_path_does_not_need_network():
    result = await llm.chat_json("返回一个 JSON 对象")
    assert result["uncertain"] is True
    assert "测试禁网" in result["error"]
