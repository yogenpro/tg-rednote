# QR cookie capture

Enable the optional helper with `docker compose --profile login up -d`.
In a checkout, add `--build` to build both local images. The owner sends `/login`
in the bot's private chat, scans the QR with the Xiaohongshu app and approves
sign-in. `/login_cancel` cancels; a new `/login` after expiry requests a fresh QR.
Manual `/cookie` import remains available. No password is requested.

The helper uses a fresh, temporary browser session for each attempt. It captures
only the QR element, waits up to three minutes and confirms the account UI and
user state on a fresh server page load, plus a nonempty `web_session` cookie.
Anonymous device cookies cannot complete login. The bot updates its existing
protected cookie state only on confirmed success. Failure, cancellation, timeout
and restart preserve the previous cookie. It does not automatically replay the
retry queue or publish notes. The state schema and backup writer are unchanged.

Only the owner receives the QR. Telegram's `protect_content` flag disables normal
forwarding and saving, and the bot deletes the QR on completion, expiry or
cancellation. Deletion failures are reported. A QR and login approval still pass
through Telegram; the captured cookie passes directly over the private socket
and never appears in chat. Browser cookies, errors, screenshots and page contents
are not logged. Sessions and results expire even if the bot disconnects.

This is browser automation of the current Xiaohongshu website, not an official
stable login API. CAPTCHA, SMS verification, account restrictions, web UI changes
and QR expiry can prevent completion. The bot does not solve challenges or
collect verification codes; use a normal logged-in browser and `/cookie` if a
challenge prevents the QR flow. Xiaohongshu can redirect to regional
`rednote.com`; the helper opens that site's dedicated login page and captures
cookies from the authenticated site. A valid regional website session does not
guarantee the mainland downloader API accepts it; note-page fallback may still
be needed. End-to-end fetch behavior needs verification after the owner's scan.

## Isolation and deployment

Keep the browser in its separate `xhs-login` container. It receives only the
proxy setting; **never attach the bot's `env_file`, token, state directory,
Docker socket or host network**. The bot image contains no browser or Playwright.
The helper image contains no bot application. Their only shared mount is a
dedicated Unix-socket volume owned by uid 10001, directory mode 0700 and socket
mode 0600. There is no TCP API, CDP port or host port. Each login also has an
unguessable capability; the authenticated cookie is delivered once, then erased.

The helper runs as uid 10001 with a read-only root filesystem, temporary `/tmp`,
bounded shared memory, 768 MiB memory, 256 processes, all capabilities dropped,
`no-new-privileges` and Docker's default seccomp profile. Chromium's inner
namespace sandbox is disabled because default Docker seccomp disallows its user
namespaces. The separate restricted container is the browser's security boundary;
do not run `worker.py` directly on a host or inside the credential-holding bot.
No privileged mode, `SYS_ADMIN`, host IPC or Docker seccomp relaxation is required.

Browser requests allow only Xiaohongshu and its CDN domains. The helper uses the
same `PROXY` (or legacy `XHS_PROXY`) as the bot; failed proxy requests never retry
directly. Chromium's browser process gets a minimal environment. Both HTTP and
WebRTC bypass routes are disabled; WebSockets and service workers are blocked.
Cookies are exported only for the authenticated site's domain
(`xiaohongshu.com` or `rednote.com`) and screened for header injection. No
existing cookie is injected into the login browser.

The optional helper publishes as `ghcr.io/yogenpro/tg-rednote-login`, built from
`login/` after the same offline test gate as the bot. Pin both image digests in
managed deployments. An existing managed deployment needs explicit helper/socket
wiring and its normal backup and rollout procedure before `/login` is available.
Its availability does not imply an end-to-end account login has been validated;
that acceptance requires the owner's scan and approval.

The DOM approach follows the current [upstream login implementation](https://github.com/xpzouying/xiaohongshu-mcp/blob/main/xiaohongshu/login.go).
The dedicated regional login page is also described in
[upstream's regional compatibility report](https://github.com/xpzouying/xiaohongshu-mcp/issues/812).
Container sandbox behavior is described in the [Playwright Docker guide](https://playwright.dev/python/docs/docker).
