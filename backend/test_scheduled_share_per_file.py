import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from app.api.scheduled_share import ScheduledShareTaskCreate, ScheduledShareTaskUpdate
from app.services import scheduler
from app.services.scheduler import _sunflower_name_parts
from app.services.tg_bot import TGService


def test_scheduled_share_api_accepts_per_file_mode():
    create_payload = ScheduledShareTaskCreate(
        name="逐条分享",
        account_id=1,
        dir_path="/影视库",
        cron_expression="0 3 * * *",
        share_mode="per_file",
        target_channels=["-1001"],
    )
    update_payload = ScheduledShareTaskUpdate(share_mode="per_file")

    assert create_payload.share_mode == "per_file"
    assert update_payload.share_mode == "per_file"


def test_per_file_mode_uses_tmdb_media_name_for_file_copy():
    assert _sunflower_name_parts("十万个冷笑话 (2014) [tmdbid=323182]") == (
        "shiwangelengxiaohua (2014) [tmdbid=323182]",
        "323182",
        "十万个冷笑话",
    )


def test_per_file_discovery_skips_desensitization_without_tmdb_id():
    class FakeService:
        async def _get_dir_items(self, cid, *, strict=False):
            assert strict
            assert cid == 11
            return [{"id": "21", "name": "普通电影.mkv", "is_dir": False}]

    async def no_pause(*_args, **_kwargs):
        return None

    with patch.object(scheduler, "_per_file_pause", no_pause):
        candidates = asyncio.run(
            scheduler._collect_per_file_candidates(
                FakeService(),
                10,
                [{"id": "11", "name": "普通电影", "is_dir": True}],
                "影视库",
            )
        )

    assert len(candidates) == 1
    assert candidates[0]["media_context"] is None


def test_per_file_broadcast_includes_original_name_and_single_link_for_all_channels():
    class FakeBot:
        def __init__(self):
            self.messages = []

        async def send_message(self, *args, **kwargs):
            self.messages.append((args, kwargs))

    service = TGService.__new__(TGService)
    service.bot = FakeBot()
    metadata = {
        "full_text": "📄 定时逐条分享：未脱敏文件名.mkv\n🔗 链接：https://115.com/s/test",
        "entities": [],
        "scheduled_share_per_file": True,
    }

    for channel_config in (
        {"id": "-1001", "concise": True},
        {"id": "-1002", "concise": False},
    ):
        asyncio.run(
            service._post_to_single_channel_batch(
                channel_config,
                {"https://115.com/s/test": "https://115.com/s/test"},
                metadata,
                raise_on_error=True,
            )
        )

    assert len(service.bot.messages) == 2
    for (args, kwargs), channel_id in zip(service.bot.messages, ("-1001", "-1002")):
        assert args == (channel_id,)
        assert kwargs["text"] == metadata["full_text"]
        assert kwargs["text"].count("https://115.com/s/test") == 1


def test_per_file_mode_copies_sanitizes_shares_and_deletes_after_delivery():
    class FakeService:
        def __init__(self):
            self.client = SimpleNamespace(
                fs_makedirs_app=object(),
                fs_copy_app=object(),
                fs_rename_app=object(),
                fs_delete_app=object(),
            )
            self.source_files = {
                11: [{
                    "id": "21",
                    "name": "十万个冷笑话.2014.2160p.HEVC.AAC.mkv",
                    "is_dir": False,
                }]
            }
            self.copied_files = {}
            self.rename_payloads = []
            self.deleted_ids = []
            self.events = []

        async def _get_dir_items(self, cid, *, strict=False):
            assert strict
            return list(self.copied_files.get(cid, self.source_files.get(cid, [])))

        async def _fs_category_get_with_fallback(self, _cid, *, timeout=30):
            return {"state": True, "size": 100, "count": 1, "folder_count": 0}

        def _is_margin_response(self, _response):
            return False

        async def _api_call_with_timeout(self, func, *args, **kwargs):
            if func is self.client.fs_makedirs_app:
                return {"state": True, "data": {"cid": 90}}
            if func is self.client.fs_copy_app:
                self.copied_files[90] = [{
                    "id": "201",
                    "name": "十万个冷笑话.2014.2160p.HEVC.AAC.mkv",
                    "is_dir": False,
                }]
                return {"state": True}
            if func is self.client.fs_rename_app:
                self.rename_payloads.append(args[0])
                self.copied_files[90][0]["name"] = args[0][1]
                return {"state": True}
            if func is self.client.fs_delete_app:
                self.deleted_ids.extend(args[0])
                self.events.append(("delete", list(args[0])))
                for cid in args[0]:
                    self.source_files.pop(cid, None)
                    self.copied_files.pop(cid, None)
                for cid, files in self.source_files.items():
                    self.source_files[cid] = [
                        item for item in files if item["id"] not in args[0]
                    ]
                return {"state": True}
            raise AssertionError(f"Unexpected P115 operation: {func}")

        def _get_ios_ua_kwargs(self):
            return {}

        async def _share_fids_direct(self, fids):
            assert fids == ["201"]
            self.events.append("share")
            return "https://115.com/s/per-file-test"

    class FakeTelegram:
        def __init__(self):
            self.broadcasts = []

        async def broadcast_to_channels(self, links, metadata, **kwargs):
            self.broadcasts.append((links, metadata, kwargs))
            if kwargs.get("validate_only"):
                return
            if len(self.broadcasts) == 2:
                svc.events.append("broadcast_failed")
                raise RuntimeError("temporary Telegram failure")
            else:
                svc.events.append("broadcast")

    svc = FakeService()
    tg = FakeTelegram()
    task = SimpleNamespace(target_channels=["-1001"])
    root_items = [{
        "id": "11",
        "name": "十万个冷笑话 (2014) [tmdbid=323182]",
        "is_dir": True,
    }]

    async def no_pause(_operation, minimum=5, maximum=10):
        if minimum >= 180:
            svc.events.append("long_wait")

    retry_delays = []

    async def no_sleep(delay):
        retry_delays.append(delay)

    with (
        patch.object(scheduler, "_per_file_pause", no_pause),
        patch.object(scheduler.asyncio, "sleep", no_sleep),
        patch("app.services.tg_bot.tg_service", tg),
    ):
        asyncio.run(
            scheduler._run_per_file_share_mode(
                svc,
                task,
                5,
                10,
                root_items,
                "影视库",
            )
        )

    sanitized_name = (
        "shiwangelengxiaohua (2014) [tmdbid=323182].2014.2160p.HEVC.AAC.mkv"
    )
    assert svc.rename_payloads == [("201", sanitized_name)]
    assert svc.deleted_ids == [90, "21", 11]
    assert len(tg.broadcasts) == 3
    assert tg.broadcasts[0][2]["validate_only"] is True
    assert tg.broadcasts[1][0] == {
        "https://115.com/s/per-file-test": "https://115.com/s/per-file-test"
    }
    assert tg.broadcasts[1][1]["full_text"] == (
        "📄 定时逐条分享：十万个冷笑话.2014.2160p.HEVC.AAC.mkv\n"
        "🔗 链接：https://115.com/s/per-file-test"
    )
    assert tg.broadcasts[1][1]["scheduled_share_per_file"] is True
    assert tg.broadcasts[1][2]["channel_ids"] == ["-1001"]
    assert tg.broadcasts[1][2]["raise_on_error"] is True
    assert tg.broadcasts[2][2]["channel_ids"] == ["-1001"]
    assert len(retry_delays) == 1
    assert 120 <= retry_delays[0] <= 240
    assert svc.events == [
        "share",
        ("delete", [90, "21"]),
        "broadcast_failed",
        "broadcast",
        ("delete", [11]),
    ]


def main():
    test_scheduled_share_api_accepts_per_file_mode()
    test_per_file_mode_uses_tmdb_media_name_for_file_copy()
    test_per_file_discovery_skips_desensitization_without_tmdb_id()
    test_per_file_broadcast_includes_original_name_and_single_link_for_all_channels()
    test_per_file_mode_copies_sanitizes_shares_and_deletes_after_delivery()
    print("All per-file scheduled share tests passed")


if __name__ == "__main__":
    main()
