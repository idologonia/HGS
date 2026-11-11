"""Adapter Registry: maps adapter type names to adapter classes."""

from __future__ import annotations

from .base import ADAPTER_API_VERSION, BaseAdapter


class AdapterRegistry:
    def __init__(self) -> None:
        self._types: dict[str, type[BaseAdapter]] = {}

    def register(self, cls: type[BaseAdapter]) -> None:
        if cls.api_version != ADAPTER_API_VERSION:
            raise ValueError(
                f"adapter {cls.__name__} uses API v{cls.api_version}, core supports v{ADAPTER_API_VERSION}"
            )
        self._types[cls.adapter_type] = cls

    def get(self, adapter_type: str) -> type[BaseAdapter] | None:
        return self._types.get(adapter_type)

    def types(self) -> list[dict]:
        return [
            {"type": name, "api_version": cls.api_version, "description": cls.description}
            for name, cls in sorted(self._types.items())
        ]


def builtin_registry() -> AdapterRegistry:
    from .html.adapter import HtmlAdapter

    reg = AdapterRegistry()
    reg.register(HtmlAdapter)
    return reg
