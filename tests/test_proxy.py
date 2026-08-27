"""The startup egress probe: one best-effort fetch through PROXY, one log line.

This is what replaced the tailscale container's ExitNode healthcheck when the
proxy became a plain HTTP proxy on a tailnet box — a wrong egress can no longer
hide behind a proxy that answers, but a mis-set PROXY could still be silent, so
boot names the IP (CLAUDE.md, "The egress proof is one log line at startup").
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))

from app import main  # noqa: E402


PROXY = "http://100.101.102.103:8888"


async def test_the_probe_names_the_ip(monkeypatch, caplog):
    async def fake_ip(proxy):
        assert proxy == PROXY
        return "203.0.113.7"

    monkeypatch.setattr(main, "_egress_ip", fake_ip)
    with caplog.at_level(logging.INFO, logger="xhsbot"):
        await main.probe_egress(PROXY)
    assert "203.0.113.7" in caplog.text
    assert [getattr(r, "fields", None) for r in caplog.records] == [
        {"event": "proxy_egress", "ip": "203.0.113.7"}
    ]


async def test_an_unreachable_proxy_warns_but_never_raises(monkeypatch, caplog):
    async def dead(proxy):
        # Connect errors quote the address they failed on.
        raise httpx.ConnectError(f"Cannot connect to proxy {PROXY}")

    monkeypatch.setattr(main, "_egress_ip", dead)
    with caplog.at_level(logging.WARNING, logger="xhsbot"):
        # A proxy URL can carry BasicAuth credentials; the probe must survive
        # both the failure and the temptation to log the message.
        await main.probe_egress("http://user:secret@100.101.102.103:8888")
    assert any(r.levelname == "WARNING" for r in caplog.records)
    assert "ConnectError" in caplog.text
    assert "100.101.102.103" not in caplog.text
    assert "secret" not in caplog.text


async def test_no_proxy_means_no_probe(monkeypatch):
    async def never(proxy):
        raise AssertionError("must not be called")

    monkeypatch.setattr(main, "_egress_ip", never)
    await main.probe_egress("")


def test_the_probe_url_stays_an_ip_service():
    # Anything that answers with a bare IP works; this is just pinning the one
    # in use so a change is deliberate rather than accidental.
    assert main.EGRESS_URL == "https://api.ipify.org"
