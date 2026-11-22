import asyncio

from src.adapters.telegram.processor import PerUserUpdateProcessor
from tests.telegram_helpers import MAY_ID, STRANGER_ID, callback_update, text_update


class Recorder:
    def __init__(self) -> None:
        self.log: list[str] = []
        self.acked: list[int] = []

    async def acknowledge(self, update: object) -> None:
        self.acked.append(getattr(update, "update_id", -1))

    async def work(self, name: str, gate: asyncio.Event | None = None) -> None:
        self.log.append(f"start {name}")
        if gate is not None:
            await gate.wait()
        self.log.append(f"end {name}")


async def _run_all(processor: PerUserUpdateProcessor, *jobs: tuple[object, object]) -> list:
    # Like PTB: one task per update, created in Telegram order.
    return [
        asyncio.create_task(processor.process_update(update, coroutine))  # type: ignore[arg-type]
        for update, coroutine in jobs
    ]


async def test_one_users_updates_run_one_at_a_time_in_order() -> None:
    rec = Recorder()
    processor = PerUserUpdateProcessor(rec.acknowledge)
    gate = asyncio.Event()

    tasks = await _run_all(
        processor,
        (text_update(update_id=1), rec.work("voice", gate)),
        (text_update(update_id=2), rec.work("correction")),
    )
    await asyncio.sleep(0.01)
    assert rec.log == ["start voice"]  # the correction waits behind the slow voice note

    gate.set()
    await asyncio.gather(*tasks)
    assert rec.log == ["start voice", "end voice", "start correction", "end correction"]


async def test_a_callback_is_acknowledged_at_once_even_behind_a_slow_update() -> None:
    rec = Recorder()
    processor = PerUserUpdateProcessor(rec.acknowledge)
    gate = asyncio.Event()

    tasks = await _run_all(
        processor,
        (text_update(update_id=1), rec.work("gemini call", gate)),
        (callback_update(update_id=2), rec.work("tap")),
    )
    await asyncio.sleep(0.01)

    assert rec.acked == [2]  # the spinner stops now, not after the slow call
    assert rec.log == ["start gemini call"]
    gate.set()
    await asyncio.gather(*tasks)
    assert rec.log[-1] == "end tap"


async def test_another_user_is_not_held_up() -> None:
    rec = Recorder()
    processor = PerUserUpdateProcessor(rec.acknowledge)
    gate = asyncio.Event()

    tasks = await _run_all(
        processor,
        (text_update(update_id=1, user_id=MAY_ID), rec.work("may", gate)),
        (text_update(update_id=2, user_id=STRANGER_ID), rec.work("other")),
    )
    await asyncio.sleep(0.01)
    assert rec.log == ["start may", "start other", "end other"]
    gate.set()
    await asyncio.gather(*tasks)


async def test_a_failing_acknowledge_does_not_block_the_update() -> None:
    rec = Recorder()

    async def broken(update: object) -> None:
        raise RuntimeError("telegram down")

    processor = PerUserUpdateProcessor(broken)
    (task,) = await _run_all(processor, (callback_update(update_id=3), rec.work("tap")))
    await task
    await asyncio.sleep(0)
    assert rec.log == ["start tap", "end tap"]
