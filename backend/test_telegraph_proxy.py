from unittest.mock import patch

from app.services import tg_bot


class FakeResponse:
    status = 200
    charset = "utf-8"
    headers = {}

    def __init__(self):
        self.content = self

    async def read(self, _limit):
        return b'<a href="https://115.com/s/example">share</a>'

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


class FakeClientSession:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.requests = []
        self.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def get(self, url, **kwargs):
        self.requests.append((url, kwargs))
        return FakeResponse()


async def test_telegraph_fetch_uses_http_proxy():
    FakeClientSession.instances.clear()
    with (
        patch.object(tg_bot.aiohttp, "ClientSession", FakeClientSession),
        patch.object(tg_bot.settings, "PROXY_ENABLED", True),
        patch.object(tg_bot.settings, "PROXY_TYPE", "HTTP"),
        patch.object(tg_bot.settings, "PROXY_HOST", "proxy.example"),
        patch.object(tg_bot.settings, "PROXY_PORT", "8080"),
        patch.object(tg_bot.settings, "PROXY_USER", ""),
        patch.object(tg_bot.settings, "PROXY_PASS", ""),
    ):
        share_links, ed2k_links = await tg_bot.TGService.__new__(
            tg_bot.TGService
        )._fetch_telegraph_links("https://telegra.ph/example")

    session = FakeClientSession.instances[-1]
    assert session.kwargs["connector"] is None
    assert session.requests[0][1]["proxy"] == "http://proxy.example:8080"
    assert share_links == ["https://115.com/s/example"]
    assert ed2k_links == []


async def test_telegraph_fetch_uses_socks5_connector():
    FakeClientSession.instances.clear()
    connector = object()
    with (
        patch.object(tg_bot.aiohttp, "ClientSession", FakeClientSession),
        patch.object(tg_bot.ProxyConnector, "from_url", return_value=connector) as from_url,
        patch.object(tg_bot.settings, "PROXY_ENABLED", True),
        patch.object(tg_bot.settings, "PROXY_TYPE", "SOCKS5"),
        patch.object(tg_bot.settings, "PROXY_HOST", "proxy.example"),
        patch.object(tg_bot.settings, "PROXY_PORT", "1080"),
        patch.object(tg_bot.settings, "PROXY_USER", ""),
        patch.object(tg_bot.settings, "PROXY_PASS", ""),
    ):
        await tg_bot.TGService.__new__(tg_bot.TGService)._fetch_telegraph_links(
            "https://telegra.ph/example"
        )

    session = FakeClientSession.instances[-1]
    from_url.assert_called_once_with("socks5://proxy.example:1080")
    assert session.kwargs["connector"] is connector
    assert session.requests[0][1]["proxy"] is None
