# Заметки для продолжения работы

Собрано в проекте 10.10.2026 до остановки. Находки по API ещё **не перепроверены**: перед исправлением каждую нужно подтвердить по коду и документации. После использования файл можно удалить.

## Проверка кода со стороны Claude API (не перепроверено)

### api-1: Base64 images/PDFs stay in history and are re-sent on every turn; past 32 MB the whole conversation fails with 413 and the bot says "try again" (medium)
`bot/main.py:319`

**Проблема:** build_user_content (main.py:207-233) inlines every photo, image document and PDF as base64. store.append (main.py:356) saves that base64 in SQLite, and on_message (main.py:318-319) re-sends the full history on every request. The Messages API rejects any request body over 32 MB with 413 request_too_large (SKILL.md, Document & File Input: "Limits: 32 MB request"). Base64 adds about 33%, so a single Telegram file at the 20 MB download cap is already about 26.7 MB on the wire. The SDK raises RequestTooLargeError, a subclass of APIStatusError. main.py:341-343 catches it generically and replies "Claude API вернул ошибку 413. Попробуйте ещё раз." Retrying cannot help, because every later message in that conversation carries the same oversized history.

**Сценарий:** The girlfriend sends a 14 MB lecture PDF, asks questions, then sends a second 12 MB PDF in the same dialog. The history now holds about 34.7 MB of base64, so this request and every later one gets 413. The bot keeps telling her to try again until she happens to press /new. The same thing happens gradually after about 50-80 ordinary Telegram photos (0.3-0.6 MB each) in one long conversation.

**Исправление:** (1) Before calling claude.ask, compute len(json.dumps(messages)). If it is over about 30 MB, do not send. Tell the user that the dialog is too large because of files and to start a new one with /new. (2) Add an explicit `except anthropic.RequestTooLargeError` before the APIStatusError handler, with the same /new hint. (3) Better: upload PDFs and images once with the Files API (`await client.files.upload(file=(name, data, mime))`, with no expires_in_seconds so replays keep working) and store `{"type":"document","source":{"type":"file","file_id":...}}` or `{"type":"image","source":{"type":"file","file_id":...}}` in history instead of base64. A file reference keeps the body small and is the pattern the docs recommend for content referenced across turns. Apply this only to new turns, because rewriting already-sent turns is a history edit.

### api-2: No context-size guard: huge text files and PDFs enter history permanently, and every later message re-bills them (cache writes after 5 idle minutes, Haiku's 5x tier above 100K) (medium)
`bot/main.py:234`

**Проблема:** Text documents up to 20 MB are decoded and inlined in full with no size cap (main.py:234-239). PDFs and images are inlined too, and everything stays in the conversation forever. Nothing checks how large the prompt has become. All four models have a 1M-token window, so a 2-4 MB text file (about 0.5-1M tokens) is accepted, and every later message carries it. Caching (claude.py:82) uses the default 5-minute TTL. After any pause longer than 5 minutes, the whole history is written to cache again at 1.25x the input price ($5/MTok on Opus 5.5, $12.50/MTok on Fable 5.1). On Haiku 5.5, a prompt over 100K tokens moves the whole request to the $0.50/$2.50 rate card. Separately, when the input overflows mid-generation the API ends with stop_reason "model_context_window_exceeded". main.py treats that as a normal reply and stores it. A 20 MB text file gives a 400 "prompt is too long", and the hint at main.py:336-337 wrongly blames the dialog length.

**Сценарий:** A user sends a 3 MB server log (about 800K tokens) with the model set to Fable 5.1. The first message costs about $8-10 in input. After that, each reply that follows a coffee break re-writes 800K tokens to cache, about $10 per message. Each quick follow-up still reads 800K cached tokens. /usage shows the spend only after the fact, and nothing warns the user.

**Исправление:** (a) Cap inlined text files, for example at 300 KB, and reject larger ones with a message. Alternatively, call `await client.messages.count_tokens(model=..., system=..., messages=...)` before sending and ask for confirmation above a threshold (for example 150K tokens). (b) After each reply, compute `usage.input_tokens + usage.cache_read_input_tokens + usage.cache_creation_input_tokens`. Above a threshold (for example 200K, or 100K on Haiku), append a note suggesting /new, or start a new conversation automatically. (c) Handle `reply.stop_reason == "model_context_window_exceeded"`: do not store the turn, and tell the user to run /new. (d) Set a monthly spend limit for the workspace in the Anthropic Console as a hard backstop.

### api-3: Usage accounting breaks when a server-side fallback runs: the cost shows as $0 and the declined attempt is not counted (medium)
`bot/main.py:347`

**Проблема:** store.add_usage(uid, reply.served_by, reply.usage) records only the top-level usage, under final.model. With `fallbacks: "default"`, final.model is the fallback model (claude-opus-4-8, claude-opus-5 or claude-sonnet-5). The SDK accumulator sets it from the fallback block's `to.model`, and I confirmed this with a mocked SSE stream. None of these IDs are in cfg.PRICES, so main.py:151 `PRICES.get(r["model"], (0, 0))` prices them at $0. The docs also say top-level usage covers only the attempt that produced the returned message, and `usage.iterations` is the per-attempt record. In my simulation the declined Opus 5.5 attempt had 4000 output tokens in iterations and top-level usage showed 50. That declined mid-stream attempt is billed at normal rates but never recorded. Sticky routing then serves the conversation from the fallback model for about an hour, so every one of those requests also shows $0.

**Сценарий:** A cyber-adjacent question on Opus 5.5 at effort max is declined after 4000 output tokens and finished by claude-opus-4-8. /usage shows "claude-opus-4-8: 1 запр., ≈$0.00". The real bill includes about 4000 Opus 5.5 output tokens ($0.08) plus the Opus 4.8 attempt. The same $0 line repeats for every message in the following hour of sticky routing.

**Исправление:** In ClaudeClient.ask, return the iterations along with the reply. In add_usage, when `usage.iterations` is not None, insert one row per entry using `it.model or requested_model`, `it.input_tokens`, `it.output_tokens`, `it.cache_read_input_tokens` and `it.cache_creation_input_tokens`. Fall back to the top-level usage only when iterations is None. Add prices for the fallback targets to cfg.PRICES: "claude-opus-5": (5.0, 25.0), "claude-opus-4-8": (5.0, 25.0), "claude-sonnet-5": (2.0, 10.0). For any model still missing from PRICES, show "price unknown" instead of a silent $0.

### api-4: Images sent as files are inlined without a size check; above about 3.75 MB the API's 5 MB per-image base64 limit causes a 400 (medium)
`bot/main.py:229`

**Проблема:** For documents with an image/* mime type, main.py:229-233 base64-encodes up to 20 MB and sends the result. The API enforces a per-image limit on the encoded form. The installed SDK pins it at `DEFAULT_MAX_IMAGE_BASE64_BYTES = 5 * 1024 * 1024` (anthropic/lib/tools/agent_toolset.py:107), with a comment saying an oversized block is rejected at request time. Any image file over about 3.75 MB raw is therefore rejected with a 400. The bot also trusts Telegram's sender-supplied mime_type, and the API rejects base64 data that does not match its declared media_type.

**Сценарий:** A user sends a 6 MB iPhone photo or a 4K PNG screenshot "as a file" to keep full quality. The request fails with BadRequestError, and the bot shows "Ошибка запроса к Claude." followed by the raw API message. The same photo works when sent as a normal compressed Telegram photo, which the user has no way of knowing.

**Исправление:** Before building the image block, check `len(b64) <= 5 * 1024 * 1024`. If it is larger, downscale with Pillow: open the image, `thumbnail((2576, 2576))`, re-encode as JPEG at quality 85, and set media_type to "image/jpeg". If Pillow is not available, reply "Изображение больше ~3.7 МБ — пришлите его как обычное фото, не файлом". Detect the real type from magic bytes (b"\x89PNG", b"\xff\xd8", b"GIF8", b"RIFF....WEBP") instead of trusting doc.mime_type.

### api-5: MAX_TOKENS=32000 is too small for xhigh/max effort: thinking uses up the budget, the user gets an empty or truncated answer, and a turn with no text is stored (medium)
`bot/config.py:75`

**Проблема:** Thinking counts toward max_tokens, and on all four models it cannot be turned off. The migration guide says Opus 5.5 "tends to think more per turn ..., especially at xhigh and max" and recommends starting at 64K for those levels. It also documents that Haiku 5.5 at xhigh sometimes puts the whole answer in its thinking and ends the turn with no visible text. When that happens here, claude.py:97 keeps the content (often only a thinking block, since _history_content strips the empty text), main.py:355-357 stores it, and the user sees "(пустой ответ)" or only the "(ответ обрезан…)" note. The user pays for up to 32K output tokens ($0.64 on Opus 5.5, $1.60 on Fable 5.1) and gets no answer. The useless turn then stays in the history that later requests replay.

**Сценарий:** The user picks Fable 5.1 with effort max and asks a hard math question. Thinking uses all 32000 tokens, so stop_reason is max_tokens and there is no text block. The bot shows only "(ответ обрезан по лимиту длины)", charges about $1.60, and saves a turn containing only a thinking block in the dialog.

**Исправление:** Raise the default to MAX_TOKENS=64000 in config.py and .env.example. The request streams, so there is no HTTP-timeout concern, and all four models allow up to 128K. In ClaudeClient.ask/on_message: if the reply has no non-empty text block (whether stop_reason is max_tokens or end_turn), do not store either the user turn or the assistant turn. Tell the user that the model spent the whole budget thinking and suggest a lower effort or a retry. Optionally hide xhigh/max for Haiku 5.5.

### api-6: Mid-stream API errors (overloaded_error SSE) appear as "ошибка 200"; 413/403/404 get the same generic "try again" text (low)
`bot/main.py:341`

**Проблема:** When the API sends an `error` SSE event after the stream has started (for example overloaded_error), the SDK raises a plain APIStatusError with status_code 200, because it uses the HTTP status of the original response. I confirmed this with a mocked stream: `APIStatusError 200 {'type': 'error', 'error': {'type': 'overloaded_error', ...}}`. main.py:341-343 then shows "Claude API вернул ошибку 200". RequestTooLargeError (413), PermissionDeniedError (403) and NotFoundError (404, for example a model not enabled for the org) also land in this generic branch with an advice to retry. In every error path, usage is not recorded, even though the partial output streamed before the error may be billed.

**Сценарий:** During peak load, Opus 5.5 streams half an answer and then sends overloaded_error. The partial text disappears and is replaced with "Claude API вернул ошибку 200. Попробуйте ещё раз." The message looks like a bug, and the owner cannot tell an overload from a size or permission problem.

**Исправление:** In the except chain, read the error type from the body: `etype = (e.body or {}).get("error", {}).get("type") if isinstance(e.body, dict) else None`. Map overloaded_error and 529 to "Claude перегружен, повторите через минуту", RequestTooLargeError to the /new hint, and NotFoundError/PermissionDeniedError to "модель недоступна для вашего аккаунта — выберите другую". Log `e.request_id` for every error.

### api-7: Changing the model or effort mid-dialog re-bills the whole cached history at 1.25x; a per-message effort mechanism exists for this (low)
`bot/main.py:189`

**Проблема:** on_setting changes the model or effort for the current conversation, and the next request sends a different top-level `model` or `output_config.effort`. Caches are scoped per model, and the prompt-caching guide says a top-level effort change invalidates the messages cache. So the next turn writes the whole history to cache again at 1.25x the input price. On Fable 5.1 the docs add that a top-level effort change "steers the model less reliably". The bot does not warn about this, even though the buttons invite frequent switching.

**Сценарий:** A Fable 5.1 conversation has 300K tokens of history (a PDF plus discussion). The user moves effort from medium to high for one question. That single turn costs about 300K x $12.50/MTok = $3.75 in cache writes, where a cache read would have cost about $0.08. Toggling back costs the same again.

**Исправление:** Store the effort a conversation started with in the conversations table and keep the top-level `output_config.effort` fixed per conversation. When the user changes effort mid-dialog, append (append-only, and store it in history) a message `{"role": "system", "content": [], "output_config": {"effort": new}}` and add the beta `mid-conversation-output-config-2026-07-01`. It works on Fable 5.1, Opus 5.5, Sonnet 5.5 and Haiku 5.5 with adaptive thinking and keeps the cache. For a model change, either start a new conversation automatically or warn that the whole history will be billed again.

### api-8: /usage cost formula uses one cache-read multiplier (0.1x) for every model and ignores Haiku 5.5's >100K rate card (low)
`bot/main.py:153`

**Проблема:** The estimate charges cache reads at `pin * 0.1` for all models. The current prices are Opus 5.5 at $0.20/MTok (0.05x), Fable 5.1 at $0.25/MTok (0.025x), and Sonnet 5.5 and Haiku 5.5 at 0.1x. In long chats cache reads dominate, so /usage overstates Opus spend about 2x and Fable spend about 4x on that part. Haiku 5.5 bills a request whose prompt is over 100K tokens at $0.50/$2.50 instead of $0.10/$0.50, which the per-model SUM in usage_since cannot represent, so Haiku spend on long chats is understated about 5x.

**Сценарий:** After a week of long Fable chats, /usage reports ≈$40 while the Console shows about $15, and a heavy Haiku PDF chat shows ≈$0.30 while it actually cost about $1.50. The owner cannot trust the number.

**Исправление:** Store a per-model cache-read multiplier in config, for example `CACHE_READ = {"claude-opus-5-5": 0.05, "claude-fable-5-1": 0.025, "claude-sonnet-5-5": 0.1, "claude-haiku-5-5": 0.1}`, and use it in the formula. For Haiku, compute the cost per request at insert time: the prompt size is input + cache_read + cache_creation, and above 100K the $0.50/$2.50 rates apply. Store a `cost_usd` column in the usage table and SUM that, instead of re-deriving cost from aggregated tokens.

### api-9: _history_content removes only trailing empty text blocks; an empty text block in the middle (e.g. before a mid-stream fallback) is stored and replayed (low)
`bot/claude.py:46`

**Проблема:** Only trailing empty or whitespace-only text blocks are dropped. If a text block is opened and the model declines before any delta arrives, the stored content is `[{'type':'text','text':''}, thinking, text]`. I confirmed this by calling _history_content directly. The Messages API rejects empty text content blocks in request messages, so replaying that assistant turn can return a 400 on every later message in the dialog. Removing whitespace-only text blocks is safe for the preserved-thinking check, which per shared/preserved-thinking-migration.md ignores "whitespace-only text blocks" and leading or trailing whitespace.

**Сценарий:** A Sonnet 5.5 answer is declined just after its text block opens, and the fallback to Sonnet 5 completes the answer. The stored turn contains an empty text block. The user's next message gets "Ошибка запроса к Claude" with a text-block validation error, and the dialog stays broken until /new.

**Исправление:** In _history_content, filter every text block whose `text.strip()` is empty, wherever it appears: `out = [b for b in out if not (b.get('type') == 'text' and not b.get('text','').strip())]`. Keep the rstrip of the final text block. If the filtered list contains no text block at all, return [] so the turn is not stored (see api-5).

## Факты для инструкции по развёртыванию (Waicore, API, Telegram, Docker)

# Research report: deploying a Python Telegram bot (Docker, long polling) on a Waicore VDS

## 1. Waicore (waicore.com)

**What I could not check:** Waicore's own docs at wiki.waicore.net could not be fetched (server errors), and waicore.com returned only meta tags. Everything below comes from third-party review and benchmark sites. Treat it as likely, not confirmed.

| Item | Finding | Source |
|---|---|---|
| Company | Waicore LTD, UK company registered 22 Apr 2024 (formerly H.S.N. LTD), AS213887. Another review lists a contact address in Batumi, Georgia, and says it has operated since 2021. | [S1], [S2] |
| Locations | Mainly Germany (Frankfurt), with some mention of Finland. The provider announced **Moscow** VPS in June 2025. A YABS benchmark of a plan named "RM-1" shows a Moscow server whose IP geolocates to Frankfurt. Reviews often say the IP geolocation does not match the advertised location. | [S2], [S3], [S4], [S5] |
| Virtualization | **KVM**. Two independent YABS benchmarks report `vm: KVM`, and review sites list Waicore as KVM. Docker runs fine on KVM (unlike old OpenVZ/LXC). | [S5], [S6], [S7], [S8] |
| OS images | Benchmarked servers ran **Ubuntu 24.04 LTS** (kernel 6.8). I found no full list of OS templates. | [S5], [S6] |
| Root/SSH access | Not documented in any source I could reach. Review sites say KVM gives full root, and the provider's forum posts mention root on test servers. How credentials are delivered (email or client area) is unverified. | [S8], [S4] |
| Control panel | Client area / "VM manager" (one review says it did not work). ISPmanager, HestiaCP and FastPanel are offered as optional hosting panels. | [S3], [S7], [S8] |
| Docker allowed? | No explicit policy found. The ToS and FAQ could not be fetched. | — |
| Cheapest plans | About €4/mo for 1 vCPU / 2 GB / 30 GB NVMe (EPYC). About €6/mo for 1 vCPU / 2 GB / 60 GB (Ryzen 5950X). A promo "RX-VPN" plan was €1 for 1 vCPU / 1 GB / 10 GB. One RU listing shows RX1 at 717 RUB for 1 core / 2 GB / 30 GB. Benchmarked boxes had 1.9 GiB RAM and a 30.9 GB disk. | [S4], [S7], [S8], [S5] |
| Reputation | Mixed to poor. Ratings are 3.2/5 on hosting101 and 1.5 on hostings.info. Complaints: IP geolocation mismatch, slow activation, refund problems, and IPs blocked in Russia. | [S3], [S7] |

To check the server from the shell: `systemd-detect-virt` should print `kvm`, `cat /etc/os-release` shows the OS, and `free -h` / `df -h` show RAM and disk. This is standard Linux tooling. I could not fetch the systemd man page to cite it.

## 2. Anthropic API: regions, billing, keys

**Supported regions**
- The supported-regions list has 175 entries. **Russia and Belarus are not on it.** Germany, Finland, Netherlands, Kazakhstan, Armenia, Georgia, Turkey and UAE are on it. [S9]
- Anthropic's Supported Regions Policy says: "Any country or region not listed is unsupported." [S10]
- The policy covers the API and the developer platform, and it is not only about the server's IP. It also excludes:
  - people **physically located** in an unsupported region;
  - entities incorporated or headquartered there;
  - entities majority-owned or controlled from there. [S10]
- **Flag for the orchestrator:** the user appears to be in Russia and uses this VDS as a VPN. Under the official policy, Russia is unsupported regardless of where the server is. I am only reporting the rule, with no advice on getting around it. Also, if the VDS is Waicore's Moscow location, the requests themselves come from Russia.

**What an unsupported region gets back**
- The official errors page documents no region-specific error. [S11]
- One user report (GitHub issue, not official docs) shows HTTP **400**: "Access to Anthropic models is not allowed from unsupported countries, regions, or territories. Please refer to https://www.anthropic.com/supported-countries…" [S12]

**Billing (Console, new account)** [S13], [S14]
- Usage is paid from **prepaid credits**, bought under Settings > Billing. When credits run out, API calls stop.
- Failed requests are not billed.
- Credits **expire after 1 year** and are **non-refundable**.
- **Auto-reload** is optional: you set a balance threshold and a reload amount.
- **Minimum purchase amount: I could not verify it.** It is not stated in official docs.
- Monthly caps by tier: Start $500, Build $1,000, Scale $200k. New organizations may start in a lower "Evaluation" tier.
- You can set your own lower spend limit in Settings > Billing > Spend limits. Hitting it returns HTTP 400 `invalid_request_error`. Hitting the tier cap returns HTTP 429 `rate_limit_error` with `enforced_spend_limit_reached`.

**API key** [S15]
- Create it at platform.claude.com/settings/keys with **Create key**.
- The key starts with `sk-ant-` and is **shown only once**.
- The SDK reads the `ANTHROPIC_API_KEY` environment variable. Raw HTTP requests send it in the `x-api-key` header.

**Other API errors** [S11]: 401 bad key, 402 billing, 403 permission, 429 rate limit, 500 internal error, 529 overloaded.

## 3. Telegram Bot API basics

- **Token:** in @BotFather, send `/newbot` and give a name and a username. The username is 5–32 characters, Latin letters, digits and underscores, must end in "bot", and cannot be changed later. [S16]
  - Anyone with the token "can be used by anyone to control your bot", so keep it secret. [S16]
  - `/token` in BotFather generates a new token (revoking the old one). [S16]
  - Format example: `4839574812:AAFD39kk…`. Store it in environment variables, not in code. [S17]
- **Polling vs webhook:**
  - "There are two mutually exclusive ways of receiving updates": `getUpdates` (polling) or webhooks. [S18]
  - `getUpdates` "will not work if an outgoing webhook is set up". `deleteWebhook` switches back to polling. Pending updates are kept for up to 24 hours. [S18]
  - Webhooks need a public IPv4 address, HTTPS with TLS 1.2 or later, and one of ports 443, 80, 88 or 8443. [S19]
  - Long polling only makes outgoing HTTPS calls to `api.telegram.org`, so no inbound port has to be opened. This follows from how the request model works rather than from an explicit statement in the docs. [S18]
- **One poller per token:** a second `getUpdates` consumer makes Telegram return HTTP **409 Conflict: "terminated by other getUpdates request; make sure that only one bot instance is running"**. A typical cause is a local copy still running, or two containers overlapping during a deploy. [S20]

## 4. Installing Docker Engine and Compose on Ubuntu 22.04 / 24.04

- **Supported versions:** Ubuntu 26.04, 24.04 and 22.04 LTS (64-bit). [S21]
- **Remove conflicting packages first:** `docker.io`, `docker-compose`, `docker-compose-v2`, `docker-doc`, `docker-buildx`, `podman-docker`, and any old `containerd`/`runc`. [S21]
- **Recommended way, the apt repository:** [S21]
  ```
  sudo apt update && sudo apt install ca-certificates curl
  sudo install -m 0755 -d /etc/apt/keyrings
  sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  sudo chmod a+r /etc/apt/keyrings/docker.asc
  # add /etc/apt/sources.list.d/docker.sources (download.docker.com/linux/ubuntu, stable)
  sudo apt update
  sudo apt install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  sudo docker run hello-world
  ```
- **Convenience script:** `curl -fsSL https://get.docker.com -o get-docker.sh && sh get-docker.sh`. Docker says it is "Only recommended for testing and development environments" and "not recommended … for deployment to production systems". [S21], [S22]
- **Post-install:** the `docker` group "grants root-level privileges to the user". [S23]
- **Restart policy in compose:** `no` (default), `always`, `on-failure[:N]`, `unless-stopped`. [S24]

**Running the bot alongside VPN panels**

- **Ports:** a long-polling bot publishes no ports, so it cannot clash with the ports used by the 3x-ui panel, Xray inbounds, WireGuard (UDP) or Outline. Docker-published ports would bypass ufw [S25], but that does not apply when nothing is published.
- **Outline:**
  - Outline's installer **already installs Docker** (via get.docker.com) if it is missing. Run `docker --version` before installing again. [S26]
  - Outline's own container (shadowbox) runs with `--net host`. [S26]
  - Its Watchtower updater runs with `--label-enable --scope=outline`, so it only updates Outline's labelled container and will not touch the bot. [S26]
- **WireGuard:** this is the one real risk.
  - If Docker turns on `net.ipv4.ip_forward` itself, it also sets the iptables `FORWARD` policy to **DROP**. [S25], [S27]
  - That can break routing for VPN clients whose setup relies on the default ACCEPT policy. There are user reports of exactly this. [S28]
  - Fixes: add an accept rule in the `DOCKER-USER` chain (e.g. `iptables -I DOCKER-USER -i eth0 -o wg0 -j ACCEPT` [S28]), or set `"ip-forward-no-drop": true` in `/etc/docker/daemon.json`. [S25]
  - My inference, not documented: if WireGuard already enabled ip_forward through sysctl, Docker leaves the policy alone, and wg-quick setups with explicit `PostUp` FORWARD ACCEPT rules are usually unaffected.
- **3x-ui / Xray:**
  - 3x-ui installs as a systemd service (`x-ui`) via its script, or runs in Docker. [S29]
  - My inference, not verified: Xray is a user-space proxy and does not depend on kernel packet forwarding, so Docker's FORWARD DROP policy should not affect it.
- **Resources:** a 1 vCPU / 2 GB plan is enough for a small Python bot next to a VPN. This is an estimate, not sourced.

## 5. Docker log rotation (json-file)

- By default there is no rotation: `max-size` is -1 (unlimited) and `max-file` defaults to 1, and only takes effect when `max-size` is set. Options are `max-size` (e.g. `10m`), `max-file` (e.g. `3`) and `compress` (default false). [S30]
- Docker recommends the `local` driver for most cases, because it rotates by default. [S31]
- Per-service setting in compose (structure from the compose spec [S24], options from [S30]):
  ```yaml
  services:
    bot:
      restart: unless-stopped
      logging:
        driver: json-file
        options:
          max-size: "10m"
          max-file: "3"
  ```
- Daemon-wide setting in `/etc/docker/daemon.json`: `{"log-driver":"json-file","log-opts":{"max-size":"10m","max-file":"3"}}`. Values must be strings. It applies only to **newly created** containers after a Docker restart. [S30]

## Unverified or flagged
- Waicore: official docs and ToS were unreachable. Root/SSH delivery, Docker policy and the full OS list are unconfirmed. The Moscow location exists, and IP geolocation complaints are common.
- Anthropic: the minimum credit purchase is not stated in official docs. The 400 "unsupported countries" error text comes from a user report, not official docs.
- The Docker/WireGuard interaction depends on how WireGuard was set up. The Xray-is-unaffected point is my inference.

## Sources
- [S1] https://geomyip.com/hosters/10070
- [S2] https://ru.hostings.info/waicore-com.html
- [S3] https://hosting101.ru/waicore
- [S4] https://ragemp.pro/threads/waicore-germanija-5-25gbps-25-na-zapusk.14246/
- [S5] https://www.vpsbenchmarks.com/yabs/waicore-1c-2gb-20250628-808154
- [S6] https://www.vpsbenchmarks.com/yabs/waicore-1c-2gb-20250917-964d2d
- [S7] https://ru.hostings.info/waicore-com.html (plans table)
- [S8] https://vpsup.ru/reviews/waicore.com.html
- [S9] https://platform.claude.com/docs/en/api/supported-regions
- [S10] https://www.anthropic.com/supported-countries
- [S11] https://platform.claude.com/docs/en/api/errors
- [S12] https://github.com/anthropics/claude-code/issues/2656
- [S13] https://support.claude.com/en/articles/8977456
- [S14] https://platform.claude.com/docs/en/api/rate-limits
- [S15] https://platform.claude.com/docs/en/get-api-key
- [S16] https://core.telegram.org/bots/features#botfather
- [S17] https://core.telegram.org/bots/tutorial
- [S18] https://core.telegram.org/bots/api#getupdates
- [S19] https://core.telegram.org/bots/webhooks
- [S20] https://community.render.com/t/it-is-impossible-to-deploy-the-bot-make-sure-that-only-one-bot-instance-is-running/6990
- [S21] https://docs.docker.com/engine/install/ubuntu/
- [S22] https://github.com/docker/docker-install
- [S23] https://docs.docker.com/engine/install/linux-postinstall/
- [S24] https://raw.githubusercontent.com/compose-spec/compose-spec/main/05-services.md
- [S25] https://docs.docker.com/engine/network/packet-filtering-firewalls/
- [S26] https://raw.githubusercontent.com/OutlineFoundation/outline-server/master/src/server_manager/install_scripts/install_server.sh
- [S27] https://docs.docker.com/engine/network/firewall-iptables/
- [S28] https://dietpi.com/forum/t/docker-breaking-wireguard-external-connectivity/4460
- [S29] https://github.com/MHSanaei/3x-ui
- [S30] https://docs.docker.com/engine/logging/drivers/json-file/
- [S31] https://docs.docker.com/engine/logging/configure/