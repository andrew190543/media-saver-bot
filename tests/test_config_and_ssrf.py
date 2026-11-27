import socket
from pathlib import Path
import pytest

from config import (
    Settings,
    URLValidationError,
    normalize_aiohttp_proxy,
    sanitize_secrets,
    validate_external_url,
)


def test_get_proxy_for_url_domain_matching():
    s = Settings(
        BOT_TOKEN="123:abc",
        PROXY_ENABLED=True,
        PROXY_URL="socks5://127.0.0.1:1080",
        PROXY_DOMAINS="youtube.com,x.com,pin.it",
    )
    assert s.get_proxy_for_url("https://dropbox.com/file.mp4") is None
    assert s.get_proxy_for_url("https://netflix.com/watch/1") is None
    assert s.get_proxy_for_url("https://yandex.com/?q=x.com") is None
    assert s.get_proxy_for_url("https://spin.it/abc") is None
    assert s.get_proxy_for_url("https://x.com/user/status/1") == "socks5://127.0.0.1:1080"
    assert s.get_proxy_for_url("https://mobile.x.com/user/status/1") == "socks5://127.0.0.1:1080"
    assert s.get_proxy_for_url("https://www.youtube.com/watch?v=1") == "socks5://127.0.0.1:1080"
    assert s.get_proxy_for_url("https://[bad") is None


def test_normalize_aiohttp_proxy():
    assert normalize_aiohttp_proxy(None) is None
    assert normalize_aiohttp_proxy("") is None
    assert normalize_aiohttp_proxy("socks5h://127.0.0.1:1080") == "socks5://127.0.0.1:1080"
    assert normalize_aiohttp_proxy("SOCKS4A://127.0.0.1:1080") == "socks4://127.0.0.1:1080"
    assert normalize_aiohttp_proxy("socks5://127.0.0.1:1443") == "socks5://127.0.0.1:1443"


def test_sanitize_secrets():
    raw = (
        "Failed via socks5://myuser:mypass@10.0.0.1:1080 "
        "https://api.telegram.org/bot123456:SECRET_XYZ/sendMessage"
    )
    cleaned = sanitize_secrets(raw, bot_token="123456:SECRET_XYZ")
    assert "myuser" not in cleaned
    assert "mypass" not in cleaned
    assert "SECRET_XYZ" not in cleaned
    assert "***:***@" in cleaned


def test_settings_effective_properties():
    cloud = Settings(BOT_TOKEN="123:abc", USE_LOCAL_BOT_API=False, MAX_FILESIZE_MB=48)
    assert cloud.api_scope == "cloud"
    assert cloud.effective_max_filesize_mb == 48
    assert cloud.effective_download_timeout == cloud.DOWNLOAD_TIMEOUT
    assert Path(cloud.TEMP_DIR).name == "media_saver_bot"

    local = Settings(
        BOT_TOKEN="123:abc",
        USE_LOCAL_BOT_API=True,
        LOCAL_MAX_FILESIZE_MB=1950,
        LOCAL_DOWNLOAD_TIMEOUT=1800,
    )
    assert local.api_scope == "local"
    assert local.effective_max_filesize_mb == 1950
    assert local.effective_download_timeout == 1800


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_url",
    [
        "http://127.0.0.1:8081/bot",
        "http://[::1]:8080/",
        "http://[::ffff:127.0.0.1]/",
        "http://[::ffff:169.254.169.254]/latest/meta-data",
        "http://169.254.169.254/latest/meta-data",
        "http://100.64.0.1/",
        "http://0177.0.0.1/",
        "http://0x7f.0.0.1/",
        "http://127.1/",
        "http://2130706433/",
        "http://localhost./test",
        "http://telegram-bot-api:8081/",
        "http://127.0.0.1.nip.io/x",
        "http://localtest.me/",
        "r:https://example.com/a.jpg",
        "file:///etc/passwd",
        "http://user:pass@youtube.com/watch?v=1",
        "https://[fe80::1%25eth0]/",
        "https://example.com\\test",
    ],
)
async def test_validate_external_url_blocks_ssrf(bad_url):
    with pytest.raises(URLValidationError):
        await validate_external_url(bad_url, uses_proxy=True)


@pytest.mark.asyncio
async def test_validate_external_url_dns_check(monkeypatch):
    # Proxied domain skips local DNS (RU poisoned ISP DNS case)
    assert (
        await validate_external_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ", uses_proxy=True)
        == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    )

    # Non-proxied domain resolves DNS and blocks private IP
    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.50", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(URLValidationError):
        await validate_external_url("https://evil-rebind.example.com/video.mp4", uses_proxy=False)

    # Public IP passes
    def fake_public_dns(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_public_dns)
    assert (
        await validate_external_url("https://example.com/video.mp4", uses_proxy=False)
        == "https://example.com/video.mp4"
    )
