from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=dict)

    def as_ollama_tool(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema or {"type": "object", "properties": {}},
            },
        }


class ToolProvider(ABC):
    @abstractmethod
    def list_tools(self) -> list[ToolDefinition]:
        raise NotImplementedError

    @abstractmethod
    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        raise NotImplementedError

    def ollama_tools(self) -> list[dict[str, Any]]:
        return [tool.as_ollama_tool() for tool in self.list_tools()]
