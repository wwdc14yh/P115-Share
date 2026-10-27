import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from app.services import scheduler
from app.services.scheduler import (
    _extract_sunflower_tmdb_id,
    _sunflower_file_name,
    _sunflower_name_parts,
)


def test_extract_sunflower_tmdb_id_from_supported_tags():
    assert _extract_sunflower_tmdb_id("Title {tmdbid-336706}") == "336706"
    assert _extract_sunflower_tmdb_id("Title [tmdbid=336706]") == "336706"
    assert _extract_sunflower_tmdb_id("Title tmdb-336706") == "336706"
    assert _extract_sunflower_tmdb_id("Title without an identifier") is None


def test_sunflower_directory_name_uses_pinyin_year_and_canonical_tmdb_tag():
    assert _sunflower_name_parts("雷霆令 (2026) [tmdbid=336706]") == (
        "leitingling (2026) [tmdbid=336706]",
        "336706",
        "雷霆令",
    )


def test_directory_without_tmdb_id_is_not_desensitized():
    assert _sunflower_name_parts("雷霆令 (2026)") == (
        "雷霆令 (2026)",
        None,
        None,
    )


def test_sunflower_file_name_adds_directory_prefix_and_preserves_media_details():
    assert _sunflower_file_name(
        "雷霆令.2026.S01E03.2160p.HDR Vivid.WEB-DL.H265.AAC.mkv",
        source_dir_name="雷霆令 (2026) [tmdbid=336706]",
        source_title="雷霆令",
        standardized_dir_name="leitingling (2026) [tmdbid=336706]",
    ) == (
        "leitingling (2026) [tmdbid=336706].2026.S01E03."
        "2160p.HDR Vivid.WEB-DL.H265.AAC.mkv"
    )


def test_sunflower_file_name_transliterates_remaining_chinese_text():
    assert _sunflower_file_name(
        "雷霆令.特别篇.mkv",
        source_dir_name="雷霆令 [tmdbid=336706]",
        source_title="雷霆令",
        standardized_dir_name="leitingling [tmdbid=336706]",
    ) == "leitingling [tmdbid=336706].tebiepian.mkv"


async def test_sunflower_mode_skips_empty_dirs_and_deletes_only_after_delivery():
    class FakeService:
        def __init__(self):
            self.client = SimpleNamespace(
                fs_makedirs_app=object(),
                fs_copy_app=object(),
                fs_rename_app=object(),
                fs_delete_app=object(),
            )
            self.rename_payloads = []
            self.deleted_ids = []
            self.events = []

        async def _get_dir_items(self, cid, *, strict=False):
            assert strict
            if cid == 11:
                return [{
                    "id": "21",
                    "name": "雷霆令.2026.S01E03.2160p.mkv",
                    "is_dir": False,
                }]
            if cid == 12:
                return []
            if cid == 90:
                return [{
                    "id": "201",
                    "name": "雷霆令.2026.S01E03.2160p.mkv",
                    "is_dir": False,
                }]
            raise AssertionError(f"Unexpected directory listing: {cid}")

        async def _fs_category_get_with_fallback(self, _cid, *, timeout=30):
            return {"state": True, "size": 100, "count": 1, "folder_count": 0}

        def _is_margin_response(self, _response):
            return False

        async def _api_call_with_timeout(self, func, *args, **kwargs):
            if func is self.client.fs_makedirs_app:
                return {"state": True, "data": {"cid": 90}}
            if func is self.client.fs_copy_app:
                return {"state": True}
            if func is self.client.fs_rename_app:
                self.rename_payloads.append(args[0])
                return {"state": True}
            if func is self.client.fs_delete_app:
                self.deleted_ids = args[0]
                self.events.append("delete")
                return {"state": True}
            raise AssertionError(f"Unexpected P115 operation: {func}")

        def _get_ios_ua_kwargs(self):
            return {}

        async def _share_fids_direct(self, fids):
            assert fids == [90]
            self.events.append("share")
            return "https://115.com/s/test"

    class FakeTelegram:
        def __init__(self):
            self.bot = object()
            self.broadcasts = []

        async def broadcast_to_channels(self, links, metadata, **kwargs):
            self.broadcasts.append((links, metadata, kwargs))
            if not kwargs.get("validate_only"):
                svc.events.append("broadcast")

        async def enqueue_scheduled_share_review(self, *_args, **_kwargs):
            raise AssertionError("葵花宝典模式不应等待或登记审核")

    svc = FakeService()
    tg = FakeTelegram()
    task = SimpleNamespace(
        target_channels=["-1001"],
        account_id=7,
    )
    parent_items = [
        {"id": "11", "name": "雷霆令 (2026) [tmdbid=336706]", "is_dir": True},
        {"id": "12", "name": "空目录", "is_dir": True},
    ]

    async def no_pause(*_args, **_kwargs):
        if _kwargs.get("minimum", 0) >= 180:
            svc.events.append("post_broadcast_wait")
        return None

    async def copy_complete(*_args, **_kwargs):
        return {}

    async def create_share_link(service, fids):
        return await service._share_fids_direct(fids)

    with (
        patch.object(scheduler, "_sunflower_pause", no_pause),
        patch.object(scheduler, "_create_scheduled_serial_share_link", create_share_link),
        patch.object(scheduler, "_wait_transfer_complete", copy_complete),
        patch("app.services.tg_bot.tg_service", tg),
    ):
        await scheduler._run_sunflower_share_mode(
            svc, task, 5, 10, parent_items
        )

    assert svc.deleted_ids == [90, 11]
    assert svc.rename_payloads[0] == (90, "leitingling (2026) [tmdbid=336706]")
    assert svc.rename_payloads[1] == [
        ("201", "leitingling (2026) [tmdbid=336706].2026.S01E03.2160p.mkv")
    ]
    assert len(tg.broadcasts) == 2
    assert tg.broadcasts[0][2]["validate_only"] is True
    assert tg.broadcasts[1][2]["channel_ids"] == ["-1001"]
    assert tg.broadcasts[1][2]["raise_on_error"] is True
    assert svc.events == ["share", "broadcast", "post_broadcast_wait", "delete"]


def main():
    test_extract_sunflower_tmdb_id_from_supported_tags()
    test_sunflower_directory_name_uses_pinyin_year_and_canonical_tmdb_tag()
    test_directory_without_tmdb_id_is_not_desensitized()
    test_sunflower_file_name_adds_directory_prefix_and_preserves_media_details()
    test_sunflower_file_name_transliterates_remaining_chinese_text()
    asyncio.run(test_sunflower_mode_skips_empty_dirs_and_deletes_only_after_delivery())
    print("All sunflower naming tests passed")


if __name__ == "__main__":
    main()
