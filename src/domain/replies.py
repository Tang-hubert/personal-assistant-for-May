"""Fixed BotResponses the router sends without any LLM call. May never sees an error detail."""

from __future__ import annotations

from src.domain.budget import MAX_AUDIO_S
from src.domain.models import ActionButton, BotResponse

FINISH_CALLBACK = "s:finish"
MIN_AUDIO_S = 1
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


def voice_unclear() -> BotResponse:
    return BotResponse(
        title="我沒聽清楚",
        bullets=("請靠近手機再講一次", "或是直接打字給我也可以"),
    )


def voice_rejection(duration_s: float) -> BotResponse | None:
    """The one voice-length rule, applied by the adapter before download and by the router."""
    if duration_s > MAX_AUDIO_S:
        return voice_too_long()
    if duration_s < MIN_AUDIO_S:
        return voice_unclear()
    return None


def file_too_large() -> BotResponse:
    return BotResponse(
        title="檔案太大了",
        bullets=("這個檔案太大 我收不下", "照片請直接用相機拍了傳給我", "語音請分成短一點的幾段"),
    )


def photo_not_received() -> BotResponse:
    return BotResponse(
        title="有照片沒收到",
        bullets=("有幾張照片沒有傳過來", "請把照片再傳一次給我"),
    )


def help_menu() -> BotResponse:
    return BotResponse(
        title="我可以幫您做這些事",
        bullets=(
            "傳照片給我 可以整理講座 寫珠寶文案 記下東西放哪",
            "問我「翡翠放哪了」 我幫您找",
            "手機不會用 直接問我 我教您三個步驟",
        ),
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
