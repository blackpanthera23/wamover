"""Neutral, platform-independent chat model."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Media:
    path: str | None
    mime_type: str | None = None
    size: int | None = None
    name: str | None = None


@dataclass
class Message:
    id: int
    chat_id: int
    from_me: bool
    sender: str | None  # raw JID, None for own messages
    timestamp_ms: int
    text: str | None
    message_type: int
    media: Media | None = None


@dataclass
class Chat:
    id: int
    jid: str
    subject: str | None
    is_group: bool
    messages: list[Message] = field(default_factory=list)

    @property
    def title(self) -> str:
        return self.subject or self.jid


@dataclass
class Export:
    chats: list[Chat]
    warnings: list[str] = field(default_factory=list)
    names: dict[str, str] = field(default_factory=dict)  # jid -> display name

    @property
    def message_count(self) -> int:
        return sum(len(c.messages) for c in self.chats)

    @property
    def media_count(self) -> int:
        return sum(1 for c in self.chats for m in c.messages if m.media)
