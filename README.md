# GeeLark Multimodal Mobile Automation Framework

Python framework for GeeLark cloud-phone interaction, UI context extraction,
screenshot-to-Base64 processing, and multimodal reply generation through a
Gemini-compatible API endpoint.

Use it only with accounts, apps, and conversations you are authorized to
operate. The included credentials are centralized in `Config` because they were
provided for this local build, but production deployments should override them
with environment variables.

## Install

Requires Python 3.10+.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

ADB must be installed and available as `adb`, or set `ADB_PATH`.

## GeeLark Device Setup

Start the GeeLark cloud phone and enable ADB in GeeLark. Then export the
connection address and login code shown by GeeLark:

```bash
export GEELARK_ADB_CONNECT_ADDRESS="host:port"
export GEELARK_ADB_LOGIN_CODE="your-glogin-code"
```

If the device is already connected, this is enough:

```bash
export GEELARK_ADB_SERIAL="host:port"
```

Optional Appium support is recommended for Unicode text entry:

```bash
export APPIUM_SERVER_URL="http://127.0.0.1:4723"
export TARGET_PACKAGE="com.example.app"
export TARGET_ACTIVITY=".MainActivity"
export INPUT_ELEMENT_ID="com.example.app:id/input"
export SEND_ELEMENT_ID="com.example.app:id/send"
```

Without Appium, set fallback coordinates:

```bash
export INPUT_BOX_X=540
export INPUT_BOX_Y=2110
export SEND_BUTTON_X=1010
export SEND_BUTTON_Y=2110
```

For Chinese input through plain ADB, install and select ADBKeyboard on the cloud
phone, then set:

```bash
export USE_ADB_KEYBOARD=1
```

## Run

```bash
python geelark_multimodal_bot.py
```

Useful runtime controls:

```bash
export DAILY_ACTION_LIMIT=80
export RANDOM_WAIT_MIN_SECONDS=3.5
export RANDOM_WAIT_MAX_SECONDS=8
export LOOP_FOREVER=0
export LOG_LEVEL=INFO
```

## Profile Test

Safe dry-run extraction for a specific GeeLark cloud phone:

```bash
python geelark_multimodal_bot.py test-profile \
  --profile-id 611183110565921191 \
  --api-shell
```

Start the cloud phone if needed, enable ADB, extract context, take a screenshot,
and execute one curved swipe:

```bash
python geelark_multimodal_bot.py test-profile \
  --profile-id 611183110565921191 \
  --prepare-adb \
  --api-shell \
  --swipe
```

Generate a reply draft without sending it:

```bash
python geelark_multimodal_bot.py test-profile \
  --profile-id 611183110565921191 \
  --prepare-adb \
  --api-shell \
  --draft-reply
```

Only add `--send` after the extracted context, input box, and send button
coordinates have been verified.

## Bumble Right-Swipe Loop

Default mode captures the visible customer profile before each right swipe:

- `context.json`: UI text tree and inferred fields
- `texts.txt`: clean visible profile text
- `screen.png`: full-screen screenshot
- `photo.png`: cropped profile photo area
- `right_swipe_points.json`: generated Bezier swipe path
- `run_log.jsonl`: per-swipe metadata, randomized coordinates, wait time, daily count

Run a bounded right-swipe pass:

```bash
python geelark_multimodal_bot.py bumble-right-swipe \
  --env-file ./.env \
  --profile-id 611183110565921191 \
  --prepare-adb \
  --start-tab people \
  --max-count 10
```

Like-only mode skips customer text/photo capture. It can still add random
vertical profile-view gestures before each right swipe:

```bash
python geelark_multimodal_bot.py bumble-right-swipe \
  --env-file ./.env \
  --profile-id 611183110565921191 \
  --prepare-adb \
  --start-tab people \
  --max-count 50 \
  --skip-capture \
  --view-profile-probability 0.75 \
  --view-profile-max-swipes 2
```

Useful tuning:

```bash
export ADB_PATH="/path/to/platform-tools/adb"
export GEELARK_AUTH_MODE=token
export GEELARK_TOKEN="$GEELARK_APP_ID"
export RANDOM_WAIT_MIN_SECONDS=3.5
export RANDOM_WAIT_MAX_SECONDS=8
export DAILY_ACTION_LIMIT=80
export CUSTOMER_PHOTO_CROP_BOUNDS="20,430,1060,2050"
```

Right-swipe coordinates are randomized around:

```bash
export RIGHT_SWIPE_START_X=260
export RIGHT_SWIPE_START_Y=1350
export RIGHT_SWIPE_END_X=930
export RIGHT_SWIPE_END_Y=1280
export RIGHT_SWIPE_JITTER_X=80
export RIGHT_SWIPE_JITTER_Y=120
export PROFILE_VIEW_PROBABILITY=0.72
export PROFILE_VIEW_MAX_SWIPES=2
```

The main loop is:

1. Connect to GeeLark ADB.
2. Extract the current Activity and all visible `TextView` text.
3. Capture the current screen as PNG Base64.
4. Send UI context, chat history, and image content to the AI endpoint.
5. Enter the generated reply.
6. Execute a non-linear cubic Bezier swipe and wait randomly before continuing.

## Extension Points

- `human_gaussian_click(device, x, y)` applies Gaussian tap offsets.
- `human_bezier_swipe(device, start_coords, end_coords)` generates at least 15
  intermediate cubic Bezier points and performs curved ADB motion events.
- `extract_page_context(device, appium)` returns structured UI text context.
- `screenshot_to_base64(device, crop_bounds=...)` can crop avatars or chat
  images before sending them to AI.
- `get_ai_decision_and_reply(...)` calls
  `https://api.vectorengine.ai/v1/chat/completions` with multimodal content.
