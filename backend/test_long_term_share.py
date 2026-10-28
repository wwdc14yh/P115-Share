import asyncio

from app.services.p115 import P115Service, _is_long_term_share


def test_long_term_share_is_detected_from_duration():
    assert _is_long_term_share({"share_duration": -1})
    assert _is_long_term_share({"share_info": {"share_duration": "-1"}})


def test_long_term_share_is_detected_from_expiry_text():
    assert _is_long_term_share({"expire_time": "长期"})
    assert _is_long_term_share({"share_end_time": "永久有效"})
    assert _is_long_term_share({"shareinfo": {"expire_time": -1, "share_duration": "-1"}})


def test_non_long_term_expiry_is_not_skipped():
    assert not _is_long_term_share({"share_duration": 7})
    assert not _is_long_term_share({"share_end_time": 1793232000})
    assert not _is_long_term_share({"share_state": 1})


def test_share_status_exposes_long_term_expiry():
    class StubService:
        async def _share_snap_with_fallback(self, payload):
            assert payload["share_code"] == "abc"
            return {
                "data": {
                    "shareinfo": {
                        "expire_time": -1,
                        "share_duration": "-1",
                        "share_state": "1",
                        "have_vio_file": 0,
                    }
                }
            }

    status = asyncio.run(
        P115Service.get_share_status(StubService(), "https://115.com/s/abc")
    )

    assert status["is_long_term"]
    assert status["is_normal"]


def test_share_status_does_not_mark_auditing_link_as_normal():
    class StubService:
        async def _share_snap_with_fallback(self, payload):
            return {
                "data": {
                    "shareinfo": {
                        "expire_time": -1,
                        "share_duration": "-1",
                        "share_state": "0",
                        "have_vio_file": 0,
                    }
                }
            }

    status = asyncio.run(
        P115Service.get_share_status(StubService(), "https://115.com/s/abc")
    )

    assert status["is_pending"]
    assert status["is_long_term"]
    assert not status["is_normal"]
