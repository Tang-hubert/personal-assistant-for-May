from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import count
from typing import Any

from telegram import (
    CallbackQuery,
    Chat,
    Document,
    InlineKeyboardMarkup,
    Message,
    PhotoSize,
    Sticker,
    Update,
    User,
    Video,
    Voice,
)

from src.domain.models import ChatAction

MAY_ID = 7001
STRANGER_ID = 9999
SENT_AT = datetime(2026, 11, 21, 3, 0, tzinfo=UTC)
_ids = count(1)


@dataclass
class Sent:
    kind: str  # "message" or "photo"
    chat_id: int
    text: str
    markup: InlineKeyboardMarkup | None
    photo: bytes | None = None


@dataclass
class FakeBotApi:
    files: dict[str, bytes] = field(default_factory=dict)
    sent: list[Sent] = field(default_factory=list)
    downloads: list[str] = field(default_factory=list)
    chat_actions: list[tuple[int, ChatAction]] = field(default_factory=list)
    answered: list[str] = field(default_factory=list)
    fail_send: bool = False
    fail_photo: bool = False
    fail_download: bool = False

    async def get_file_bytes(self, file_id: str) -> bytes:
        self.downloads.append(file_id)
        if self.fail_download:
            raise RuntimeError("download failed")
        return self.files.get(file_id, b"file-" + file_id.encode())

    async def send_message(
        self, chat_id: int, text: str, markup: InlineKeyboardMarkup | None
    ) -> None:
        if self.fail_send:
            raise RuntimeError("send failed")
        self.sent.append(Sent("message", chat_id, text, markup))

    async def send_photo(
        self, chat_id: int, photo: bytes, caption: str, markup: InlineKeyboardMarkup | None
    ) -> None:
        if self.fail_photo:
            raise RuntimeError("photo rejected")
        self.sent.append(Sent("photo", chat_id, caption, markup, photo))

    async def send_chat_action(self, chat_id: int, action: ChatAction) -> None:
        self.chat_actions.append((chat_id, action))

    async def answer_callback_query(self, callback_query_id: str) -> None:
        self.answered.append(callback_query_id)


def message_update(
    *,
    user_id: int = MAY_ID,
    chat_type: str = Chat.PRIVATE,
    update_id: int | None = None,
    **content: Any,
) -> Update:
    chat_id = user_id if chat_type == Chat.PRIVATE else -100123
    message = Message(
        message_id=next(_ids),
        date=SENT_AT,
        chat=Chat(id=chat_id, type=chat_type),
        from_user=User(id=user_id, first_name="May", is_bot=False),
        **content,
    )
    return Update(update_id=update_id if update_id is not None else next(_ids), message=message)


def text_update(text: str = "翡翠放哪", **kw: Any) -> Update:
    return message_update(text=text, **kw)


def photo_update(file_size: int | None = 200_000, file_id: str = "large", **kw: Any) -> Update:
    sizes = (
        PhotoSize("small", "u-small", 90, 90, file_size=1_000),
        PhotoSize(file_id, f"u-{file_id}", 1280, 1280, file_size=file_size),
    )
    return message_update(photo=sizes, **kw)


def document_update(
    mime_type: str | None, file_name: str = "IMG_0001.HEIC", file_size: int | None = 300_000
) -> Update:
    return message_update(
        document=Document(
            "doc", "u-doc", file_name=file_name, mime_type=mime_type, file_size=file_size
        )
    )


def voice_update(duration: int = 30, file_size: int | None = 50_000) -> Update:
    return message_update(
        voice=Voice(
            "voice", "u-voice", duration=duration, mime_type="audio/ogg", file_size=file_size
        )
    )


def sticker_update() -> Update:
    return message_update(
        sticker=Sticker("st", "u-st", 512, 512, is_animated=False, is_video=False, type="regular")
    )


def video_update() -> Update:
    return message_update(video=Video("vid", "u-vid", 640, 480, duration=5))


def callback_update(
    data: str = "s:finish",
    *,
    with_message: bool = True,
    user_id: int = MAY_ID,
    update_id: int | None = None,
) -> Update:
    user = User(id=user_id, first_name="May", is_bot=False)
    message = (
        Message(
            message_id=next(_ids),
            date=SENT_AT,
            chat=Chat(id=user_id, type=Chat.PRIVATE),
            text="buttons",
        )
        if with_message
        else None
    )
    query = CallbackQuery(
        id=f"cq-{next(_ids)}", from_user=user, chat_instance="ci", message=message, data=data
    )
    return Update(
        update_id=update_id if update_id is not None else next(_ids), callback_query=query
    )
