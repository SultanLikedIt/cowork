"""Reusable test doubles for streaming ChatAgent and usage recording."""

from __future__ import annotations

from typing import Any


class FakeMsg:
    def __init__(self, content: str) -> None:
        self.content = content


class FakePartial:
    def __init__(self, content: str) -> None:
        self.msg = FakeMsg(content)


class FakeFinal:
    info: dict[str, Any] = {
        "usage": {
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "total_tokens": 2,
        }
    }


class FakeStream:
    """Mimics CAMEL's AsyncStreamingChatAgentResponse: async-iterable and awaitable."""

    def __init__(self, chunks: list[str]) -> None:
        self._chunks = chunks

    def __aiter__(self):
        async def generate():
            for chunk in self._chunks:
                yield FakePartial(chunk)

        return generate()

    def __await__(self):
        async def final():
            return FakeFinal()

        return final().__await__()
