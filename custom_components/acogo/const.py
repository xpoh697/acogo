"""Constants for the acoGO integration."""

DOMAIN = "acogo"
VERSION = "1.0.5"

BASE_URL = "https://api.aco.com.pl/listener/v1"

CONF_DEV_ID = "dev_id"
CONF_DEVICE_PASSWORD = "device_password"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"

# Order command IDs
ORDER_EZ_OPEN = "ezOpen"
ORDER_F2_OPEN = "f2Open"
ORDER_RECEIVE_CALL = "receiveCall"
ORDER_REJECT_CALL = "rejectCall"
ORDER_END_CALL = "endCall"

# Timers (seconds)
DOOR_CALL_DELAY = 3.0
DOOR_HOLD_DELAY = 5.0
PREVIEW_AUTO_CLOSE_TIMEOUT = 30.0
PREVIEW_WATCHDOG_TIMEOUT = 45.0
PREVIEW_CAPTURE_TIMEOUT = 25.0
FAST_POLL_INTERVAL = 3.0
CALL_LATCH_DURATION = 15.0

# Models
APP_MODELS = {62, 63}
PRO_MODELS = {65, 67, 68}
STANDARD_MODELS = {64, 66}
