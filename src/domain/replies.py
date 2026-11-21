"""Fixed BotResponses the router sends without any LLM call. May never sees an error detail."""

from __future__ import annotations

from src.domain.models import ActionButton, BotResponse

FINISH_CALLBACK = "s:finish"
TRUNCATED_NOTICE = "您的訊息有點長 我先看了前面 850 個字喔"


def finish_button() -> ActionButton:
    """Handlers attach this to Layer-2 replies so May can end the topic herself."""
    return ActionButton(label="✅ 這個話題結束", callback_data=FINISH_CALLBACK)


def rest_message() -> BotResponse:
    return BotResponse(
        title="小幫手今天先休息囉",
        bullets=("今天的額度用完了", "明天早上再來找我聊天吧"),
    )


def busy_message() -> BotResponse:
    return BotResponse(
        title="小幫手暫時忙線中",
        bullets=("請等一下再試試看", "您的訊息我沒有弄丟喔"),
    )


def new_topic_prompt() -> BotResponse:
    return BotResponse(
        title="我們開新話題吧",
        bullets=("這個話題聊得很久了", "請把想問的事情再說一次", "我會重新幫您看"),
    )


def voice_too_long() -> BotResponse:
    return BotResponse(
        title="語音有點長喔",
        bullets=("請在兩分鐘內講完", "可以分成幾段", "一段一段傳給我"),
    )


def unsupported_message() -> BotResponse:
    return BotResponse(
        title="這個我還看不懂",
        bullets=("請傳文字、語音或照片給我",),
    )


def topic_finished() -> BotResponse:
    return BotResponse(title="話題結束囉", bullets=("有新的事情隨時告訴我",))


def with_truncation_notice(response: BotResponse) -> BotResponse:
    """Add the notice as a bullet; with three bullets already, append it to the last one."""
    bullets = response.bullets
    if len(bullets) < 3:
        bullets = (*bullets, TRUNCATED_NOTICE)
    else:
        bullets = (*bullets[:-1], f"{bullets[-1]}\n{TRUNCATED_NOTICE}")
    return response.model_copy(update={"bullets": bullets})
