# Session Notes — 2026-05-27

## What this project does

`geelark_multimodal_bot.py` is an app-agnostic automation framework for GeeLark
cloud phones, primarily used today to:

1. Open Bumble on each cloud phone in a GeeLark group
2. Scan the Chats list for unread / "your move" customer messages
3. Open each actionable chat, extract conversation + matched profile context
4. Send the context (text + screenshot) to an AI endpoint
5. Type the AI-generated reply via ADBKeyboard (Unicode support) and tap Send
6. Verify the reply landed, loop to the next chat / phone

CLI subcommands (`python3 geelark_multimodal_bot.py <cmd>`):

- `test-profile` — single-phone diagnostic
- `bumble-right-swipe` — likes/right-swipes
- `bumble-capture-chat` — single-phone chat-list + chat capture + optional reply
- `bumble-group-right-swipe` — right-swipe loop across a group
- `bumble-group-reply-chats` — chat capture + reply loop across a group (with
  `--monitor-loops N` to repeat the scan; `0` = until interrupted)

## Bugs fixed this session

All three were responsible for the symptoms "抓不到客户消息 / 回不了消息 /
导航乱". Locations are inside `geelark_multimodal_bot.py`.

### 1. Click point regression — taps were landing on the avatar, not the row

`_find_chat_list_candidates` was overwriting `click_center` with the row's
ring-view (avatar) x-coordinate (~140 px) whenever a `connectionItem_ringView`
sibling was present. Tapping the avatar in Bumble only shows a profile preview
overlay; it does not open the chat thread.

Fix: when the candidate is a `connectionsItem_personName` or
`connectionsItem_message`, keep the node's own center (text middle, x ≈ 540).
The ring-view fallback now applies only to top-of-list match carousels.

### 2. Segmented swipe was being interpreted as a tap (api-shell mode)

When `--api-shell` is used, `_prefer_motion_events` returns `False`, so
`human_bezier_swipe` went straight into the per-segment fallback, issuing
~16 short `input swipe` commands of 8-50 ms each. Android (and Bumble) treats
that chain as repeated taps, so trying to scroll the Chats list could open
whichever row happened to sit under the swipe's start point.

Fix: when `use_motion_events` is `False`, skip the segmented fallback and
issue one continuous `input swipe` for the full path.

### 3. Pre-chat popup scan was skipped in api-shell + economy mode

Bumble's "You have a new match / Lucky them!" modal can blanket the entire
Chats list. The bot was unconditionally skipping the popup-dismiss step
whenever `economy_mode and isinstance(device, GeelarkOpenAPIShellDevice)`.

Fix: always run a fast `_click_visible_text_fast(["Close", "Not now",
"No thanks", "Maybe later", "Got it"])` pass; do it twice in case a second
popup is queued behind the first. Single XML grep, cheap.

### 4. Recovery if a stray scroll lands in a chat thread (defensive)

Added to the "scroll list to find more chats" loop: after each scroll, if
`_snapshot_looks_like_bumble_chat_list` returns False, send BACK twice and
reopen the Chats tab before continuing. Belt-and-suspenders alongside fix #2.

## Validation runs

| Run | Mode | Result |
|---|---|---|
| 22:00 v1 (sequential) | bumble-capture-chat on 5429 + 5379 | Confirmed click-center fix; also surfaced bug #2 (scroll into TeRRy) |
| 22:23 v2 | After fix #2 | Scroll no longer accidentally entered chat threads |
| 22:42 v4/v5 | Popup retest | Popup did not reappear, fix #3 not exercised in the wild but code path is straightforward |
| 22:47 – 01:47 long parallel run | 27 phones × parallel workers × 3 hours | 1166 iterations, 0 sent. ZERO actionable chats appeared anywhere in the 3-hour window; not a bug, just no inbound messages |

## Operational notes

- All 27 phones in group `601002110892376134` were stopped after the parallel
  run — no idle billing.
- One phone (`5559_mankitipat`) does not have ADBKeyboard installed; Chinese
  Unicode replies on that phone will fail until the IME is installed.
- One phone (`5621_osakanaomi98`) had a slow GeeLark start (>240 s) during the
  parallel run — single transient failure, not a code bug.

## How to run again (real-time monitor mode)

```bash
python3 geelark_multimodal_bot.py bumble-group-reply-chats \
  --env-file .env \
  --group-id 601002110892376134 \
  --monitor-loops 0 \
  --max-chats-per-phone 3 \
  --prepare-adb \
  --api-shell
```

For the parallel-per-phone setup used in tonight's 3-hour run, see
`/tmp/parallel_launcher.sh` (local, not committed) — one bash worker per
profile id, each looping `bumble-capture-chat --keep-phone-running` so phones
do not need to be restarted between iterations.
