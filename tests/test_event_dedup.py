from src.pipeline.event_dedup import RecentEventIds


def test_an_id_is_claimed_once() -> None:
    ids = RecentEventIds()
    assert ids.claim("tg:u:1")
    assert not ids.claim("tg:u:1")
    assert ids.claim("tg:u:2")


def test_the_oldest_id_is_forgotten_past_capacity() -> None:
    ids = RecentEventIds(capacity=2)
    for event_id in ("a", "b", "c"):
        assert ids.claim(event_id)
    assert ids.claim("a")
    assert not ids.claim("c")
