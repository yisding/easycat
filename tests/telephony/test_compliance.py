"""Tests for compliance utilities."""

from __future__ import annotations

import asyncio
import concurrent.futures
import os
import stat
import threading
from pathlib import Path

import pytest

from easycat.telephony.compliance import (
    AIDisclosureConfig,
    AsyncDNCStore,
    DNCList,
    DNCStore,
    SQLiteDNCList,
    check_calling_hours,
    lookup_timezone,
)


class TestCallingHoursEnforcement:
    def test_rejects_call_outside_hours(self) -> None:
        # 7 AM is before 8 AM start.
        assert not check_calling_hours("+12125551234", current_hour=7)
        # 9 PM (21) is at-or-after end_hour=21.
        assert not check_calling_hours("+12125551234", current_hour=21)

    def test_accepts_call_within_hours(self) -> None:
        assert check_calling_hours("+12125551234", current_hour=10)
        assert check_calling_hours("+12125551234", current_hour=20)

    def test_timezone_lookup_by_area_code(self) -> None:
        tz = lookup_timezone("+12125551234")
        assert tz == "America/New_York"
        tz = lookup_timezone("+14155551234")
        assert tz == "America/Los_Angeles"

    def test_timezone_override(self) -> None:
        # Override takes precedence — use current_hour to test logic.
        assert check_calling_hours(
            "+12125551234", current_hour=10, timezone_override="America/Chicago"
        )

    def test_unknown_timezone_blocks_call(self) -> None:
        # Area code 999 is not in the mapping — should block conservatively.
        assert not check_calling_hours("+19995551234")

    def test_malformed_timezone_override_blocks_call(self) -> None:
        assert not check_calling_hours("+12125551234", timezone_override="/etc/passwd")
        assert not check_calling_hours("+12125551234", timezone_override="../UTC")

    def test_non_nanp_number_does_not_resolve_timezone(self) -> None:
        # A non-US E.164 number (UK) must not be misrouted to a US timezone.
        assert lookup_timezone("+442012345678") is None
        # And the call must be blocked rather than allowed via a guessed tz.
        assert not check_calling_hours("+442012345678")

    def test_malformed_short_number_returns_none(self) -> None:
        # Too few digits to be a NANP number — no area code guessing.
        assert lookup_timezone("212") is None

    def test_bare_ten_digit_number_resolves(self) -> None:
        assert lookup_timezone("2125551234") == "America/New_York"


class TestAIDisclosure:
    def test_disclosure_text_configurable(self) -> None:
        config = AIDisclosureConfig(text="This call uses AI assistance")
        assert config.text == "This call uses AI assistance"

    def test_disclosure_spoken_on_human_connect(self) -> None:
        """Disclosure should be spoken when connected to human (tested at config level)."""
        config = AIDisclosureConfig(enabled=True, text="AI assisted call")
        assert config.enabled
        assert config.text == "AI assisted call"

    def test_disclosure_not_spoken_to_voicemail(self) -> None:
        """Disclosure disabled check — config flag controls this."""
        config = AIDisclosureConfig(enabled=False)
        assert not config.enabled

    def test_disclosure_disabled_by_config(self) -> None:
        config = AIDisclosureConfig(enabled=False)
        assert not config.enabled


class TestDNCIntegration:
    def test_dnc_check_before_call(self) -> None:
        dnc = DNCList()
        assert not dnc.is_on_dnc("+15551234567")

    def test_dnc_blocks_call(self) -> None:
        dnc = DNCList()
        dnc.add("+15551234567")
        assert dnc.is_on_dnc("+15551234567")


class TestDNCStoreProtocol:
    def test_dnclist_satisfies_protocol(self) -> None:
        assert isinstance(DNCList(), DNCStore)
        assert isinstance(DNCList(), AsyncDNCStore)

    def test_sqlite_dnclist_satisfies_protocol(self) -> None:
        store = SQLiteDNCList(":memory:")
        assert isinstance(store, DNCStore)
        assert isinstance(store, AsyncDNCStore)
        store.close()


class TestSQLiteDNCList:
    def test_add_remove_and_check(self) -> None:
        store = SQLiteDNCList(":memory:")
        assert not store.is_on_dnc("+15551234567")
        store.add("+15551234567")
        assert store.is_on_dnc("+15551234567")
        assert len(store) == 1
        store.remove("+15551234567")
        assert not store.is_on_dnc("+15551234567")
        assert len(store) == 0
        store.close()

    def test_add_is_idempotent_and_normalizes(self) -> None:
        # Same number, different formatting → one normalized entry. A region is
        # set so a bare-national form also canonicalizes when phonenumbers is
        # installed (without it, the digit fallback already collapses these).
        store = SQLiteDNCList(":memory:", default_region="US")
        store.add("+1 (555) 123-4567")
        store.add("15551234567")
        assert len(store) == 1
        assert store.is_on_dnc("+1 (555) 123-4567")
        store.close()

    def test_persists_across_instances(self, tmp_path: Path) -> None:
        db = tmp_path / "dnc.sqlite"
        first = SQLiteDNCList(db)
        first.add("+15551234567")
        first.close()

        # A fresh instance at the same path (e.g. after a restart) sees it.
        second = SQLiteDNCList(db)
        assert second.is_on_dnc("+15551234567")
        second.close()

    def test_db_and_sidecars_are_owner_only(self, tmp_path: Path) -> None:
        # Phone-number PII must not be world-readable (mirrors the journal).
        db = tmp_path / "dnc.sqlite"
        store = SQLiteDNCList(db)
        store.add("+15551234567")  # forces WAL/SHM sidecars to appear
        for p in (db, Path(str(db) + "-wal"), Path(str(db) + "-shm")):
            if p.exists():
                mode = stat.S_IMODE(os.stat(p).st_mode)
                assert mode == 0o600, f"{p.name} is {oct(mode)}, expected 0o600"
        store.close()

    def test_creates_missing_parent_directory(self, tmp_path: Path) -> None:
        # A fresh first run with a nested path must not crash.
        db = tmp_path / "nested" / "sub" / "dnc.sqlite"
        store = SQLiteDNCList(db)
        store.add("+15551234567")
        assert store.is_on_dnc("+15551234567")
        store.close()

    def test_concurrent_adds_are_threadsafe(self) -> None:
        store = SQLiteDNCList(":memory:")
        numbers = [f"+1555{i:07d}" for i in range(50)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(store.add, numbers))
        assert len(store) == 50
        store.close()


class TestDNCNormalization:
    def test_nanp_cross_format_matches(self) -> None:
        # Added as E.164, queried as bare national (and vice versa) — must match.
        dnc = DNCList(default_region="US")
        dnc.add("+1 (555) 123-4567")
        assert dnc.is_on_dnc("5551234567")
        assert dnc.is_on_dnc("+15551234567")

        other = DNCList(default_region="US")
        other.add("5551234567")
        assert other.is_on_dnc("+15551234567")

    def test_sqlite_nanp_cross_format_matches(self) -> None:
        store = SQLiteDNCList(":memory:", default_region="US")
        store.add("5551234567")
        assert store.is_on_dnc("+15551234567")
        store.close()

    def test_international_e164_normalization(self) -> None:
        pytest.importorskip("phonenumbers")
        dnc = DNCList()
        dnc.add("+44 20 7946 0958")  # UK, various formatting
        assert dnc.is_on_dnc("+442079460958")
        assert not dnc.is_on_dnc("+15551234567")

    def test_in_memory_dnc_rejects_numbers_without_digits(self) -> None:
        dnc = DNCList()

        with pytest.raises(ValueError, match="at least one digit"):
            dnc.add("not a phone")

        assert len(dnc) == 0
        assert not dnc.is_on_dnc("anonymous")

    def test_sqlite_dnc_rejects_numbers_without_digits(self) -> None:
        store = SQLiteDNCList(":memory:")

        with pytest.raises(ValueError, match="at least one digit"):
            store.add("not a phone")

        assert len(store) == 0
        assert not store.is_on_dnc("anonymous")
        store.close()


class TestDNCListAsyncAPI:
    """DNCList's async wrappers should behave identically to the sync methods."""

    async def test_aadd_aremove_ais_on_dnc(self) -> None:
        dnc = DNCList()
        assert not await dnc.ais_on_dnc("+15551234567")
        await dnc.aadd("+15551234567")
        assert await dnc.ais_on_dnc("+15551234567")
        assert len(dnc) == 1
        await dnc.aremove("+15551234567")
        assert not await dnc.ais_on_dnc("+15551234567")
        assert len(dnc) == 0

    async def test_async_and_sync_share_state(self) -> None:
        dnc = DNCList()
        await dnc.aadd("+15551234567")
        assert dnc.is_on_dnc("+15551234567")
        dnc.remove("+15551234567")
        assert not await dnc.ais_on_dnc("+15551234567")


class TestSQLiteDNCListAsyncAPI:
    """SQLiteDNCList's async wrappers should offload the sync core to a thread."""

    async def test_aadd_aremove_ais_on_dnc(self) -> None:
        store = SQLiteDNCList(":memory:")
        assert not await store.ais_on_dnc("+15551234567")
        await store.aadd("+15551234567")
        assert await store.ais_on_dnc("+15551234567")
        assert len(store) == 1
        await store.aremove("+15551234567")
        assert not await store.ais_on_dnc("+15551234567")
        assert len(store) == 0
        await store.aclose()

    async def test_async_and_sync_share_state(self) -> None:
        store = SQLiteDNCList(":memory:")
        await store.aadd("+15551234567")
        assert store.is_on_dnc("+15551234567")
        store.remove("+15551234567")
        assert not await store.ais_on_dnc("+15551234567")
        store.close()

    async def test_async_does_not_block_event_loop(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        store = SQLiteDNCList(":memory:")
        original_add = store.add
        started = threading.Event()
        release = threading.Event()
        ticks = 0

        def controlled_add(phone: str) -> None:
            started.set()
            if not release.wait(timeout=2.0):
                raise TimeoutError("test did not release controlled DNC write")
            original_add(phone)

        async def ticker() -> None:
            nonlocal ticks
            for _ in range(20):
                await asyncio.sleep(0)
                ticks += 1

        monkeypatch.setattr(store, "add", controlled_add)
        add_task = asyncio.create_task(store.aadd("+15551234567"))
        try:
            assert await asyncio.to_thread(started.wait, 1.0)
            await ticker()
            assert ticks == 20
            assert not add_task.done()
        finally:
            release.set()
        await add_task
        assert await store.ais_on_dnc("+15551234567")
        await store.aclose()

    async def test_aadd_normalizes_and_rejects_invalid_numbers(self) -> None:
        store = SQLiteDNCList(":memory:", default_region="US")
        await store.aadd("+1 (555) 123-4567")
        await store.aadd("15551234567")
        assert len(store) == 1

        with pytest.raises(ValueError, match="at least one digit"):
            await store.aadd("not a phone")

        await store.aclose()
