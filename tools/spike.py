#!/usr/bin/env python3
"""Answers the two open questions in PLAN §9/§10 against a real share link.

Nothing but the standard library, so it runs on the host with any Python 3.8+:

    python3 tools/spike.py "https://xhslink.com/a/xxxxx"                # §9.1
    python3 tools/spike.py "<link>" --token <bot token> --chat <chat id>  # §9.2

§9.1  Does a cookieless fetch work for a freshly-shared link?
§9.2  Do XHS CDN URLs survive Telegram's server-side fetch?

Requires the sidecar to be reachable; with the compose default that means
uncommenting the `ports:` block for xhs-downloader, or passing --downloader.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
OK, BAD, INFO = "  ✓", "  ✗", "  ·"


def post_json(url: str, payload: dict, timeout: float = 120.0) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode())


def head_media(url: str, referer: bool) -> str:
    headers = {"User-Agent": UA}
    if referer:
        headers["Referer"] = "https://www.xiaohongshu.com/"
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read(65536)
            length = response.headers.get("content-length") or "?"
            return (
                f"HTTP {response.status} {response.headers.get('content-type')} "
                f"len={length} first-bytes={len(body)}"
            )
    except urllib.error.HTTPError as exc:
        return f"HTTP {exc.code} {exc.reason}"
    except Exception as exc:  # noqa: BLE001 - diagnostics only
        return f"{type(exc).__name__}: {exc}"


def resolve(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.geturl()
    except urllib.error.HTTPError as exc:
        return exc.url or url
    except Exception as exc:  # noqa: BLE001
        return f"<unresolved: {type(exc).__name__}: {exc}>"


def fetch(downloader: str, url: str, cookie: str | None) -> tuple[bool, dict]:
    payload = {"url": url, "download": False, "skip": False}
    if cookie:
        payload["cookie"] = cookie
    try:
        body = post_json(f"{downloader.rstrip('/')}/xhs/detail", payload)
    except Exception as exc:  # noqa: BLE001
        print(f"{BAD} downloader call failed: {type(exc).__name__}: {exc}")
        return False, {}
    data = body.get("data") or {}
    print(f"{INFO} message: {body.get('message')}")
    if not data:
        return False, {}
    urls = data.get("下载地址") or []
    if isinstance(urls, str):
        urls = urls.split()
    print(
        f"{INFO} type={data.get('作品类型')} author={data.get('作者昵称')} "
        f"title={(data.get('作品标题') or '')[:40]!r} media={len(urls)}"
    )
    return True, data


def media_urls(data: dict) -> list:
    urls = data.get("下载地址") or []
    return urls.split() if isinstance(urls, str) else list(urls)


def telegram_probe(token: str, chat: str, urls: list, kind: str) -> None:
    api = f"https://api.telegram.org/bot{token}"
    if kind == "video" or len(urls) == 1:
        method = "sendVideo" if kind == "video" else "sendPhoto"
        field = "video" if kind == "video" else "photo"
        payload = {"chat_id": chat, field: urls[0], "caption": "spike: CDN URL passthrough"}
    else:
        method = "sendMediaGroup"
        payload = {
            "chat_id": chat,
            "media": json.dumps(
                [
                    {"type": "photo", "media": u, **({"caption": "spike: CDN URL passthrough"} if i == 0 else {})}
                    for i, u in enumerate(urls[:10])
                ],
                ensure_ascii=False,
            ),
        }
    try:
        body = post_json(f"{api}/{method}", payload, timeout=180)
        print(f"{OK} {method} accepted the CDN URL(s) — passthrough works, no bytes through us")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        print(f"{BAD} {method} rejected the CDN URL(s): {detail[:300]}")
        print(f"{INFO} → keep MEDIA_MODE=auto; the bot will stream through instead")
    except Exception as exc:  # noqa: BLE001
        print(f"{BAD} {method} failed: {type(exc).__name__}: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("url", help="an xhslink.com share link, freshly copied from the app")
    parser.add_argument("--downloader", default="http://127.0.0.1:5556")
    parser.add_argument("--cookie", default=None, help="optional: compare against an authenticated fetch")
    parser.add_argument("--token", default=None, help="bot token, to run the §9.2 Telegram probe")
    parser.add_argument("--chat", default=None, help="chat id to send the probe to")
    args = parser.parse_args()

    print("\n§9.1  cookieless fetch")
    print(f"{INFO} share link resolves to: {resolve(args.url)}")
    cookieless_ok, data = fetch(args.downloader, args.url, None)
    print(f"{OK if cookieless_ok else BAD} cookieless fetch {'works' if cookieless_ok else 'FAILED'}")
    if cookieless_ok:
        print(f"{INFO} → the cookie subsystem stays optional; leave the cookie unset")
    elif args.cookie:
        authed_ok, data = fetch(args.downloader, args.url, args.cookie)
        print(f"{OK if authed_ok else BAD} authenticated fetch {'works' if authed_ok else 'FAILED too'}")
        if authed_ok:
            print(f"{INFO} → set the cookie via /cookie in the bot")
    else:
        print(f"{INFO} → re-run with --cookie to see whether authentication is what's missing")

    if not data:
        print("\nNo media URLs to test; stopping before §9.2.")
        return 1

    urls = media_urls(data)
    print("\n§9.2  CDN reachability")
    print(f"{INFO} first media URL: {urls[0][:110]}")
    print(f"{INFO} with referer:    {head_media(urls[0], referer=True)}")
    print(f"{INFO} without referer: {head_media(urls[0], referer=False)}")
    print(f"{INFO} (Telegram's fetcher sends no referer — the line above is what it sees)")

    if args.token and args.chat:
        kind = "video" if str(data.get("作品类型")).lower() in {"视频", "video"} else "image"
        telegram_probe(args.token, args.chat, urls, kind)
    else:
        print(f"{INFO} pass --token and --chat to run the Telegram-side probe")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
