from repositories.collector import InMemoryCollectorRepository


async def test_register_creates_collector_without_cursor() -> None:
    repo = InMemoryCollectorRepository()
    collector = await repo.register("feed", bucket_id=1)
    assert collector.name == "feed"
    assert collector.cursor is None


async def test_register_is_idempotent_and_keeps_cursor() -> None:
    repo = InMemoryCollectorRepository()
    first = await repo.register("feed", bucket_id=1)
    await repo.update_cursor(first.id, "2026-01-01T00:00:00Z")

    again = await repo.register("feed", bucket_id=1)

    assert again.id == first.id
    assert again.cursor == "2026-01-01T00:00:00Z"


async def test_register_with_new_bucket_resets_cursor() -> None:
    repo = InMemoryCollectorRepository()
    first = await repo.register("feed", bucket_id=1)
    await repo.update_cursor(first.id, "2026-01-01T00:00:00Z")

    moved = await repo.register("feed", bucket_id=2)

    assert moved.bucket_id == 2
    assert moved.cursor is None


async def test_update_cursor_unknown_collector_returns_none() -> None:
    repo = InMemoryCollectorRepository()
    assert await repo.update_cursor(42, "2026-01-01T00:00:00Z") is None
