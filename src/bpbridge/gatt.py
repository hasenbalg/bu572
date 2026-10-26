"""GATT UUIDs used by blood pressure monitors (Bluetooth SIG assigned numbers).

16-bit shorts expand via the base UUID
0000xxxx-0000-1000-8000-00805f9b34fb.
"""

def u16(short: str) -> str:
    return f"0000{short.lower()}-0000-1000-8000-00805f9b34fb"

# --- Blood Pressure Service (0x1810) ---
BP_SERVICE            = u16("1810")
BP_MEASUREMENT        = u16("2a35")   # indicate: full record (history push + live)
INTERMEDIATE_CUFF     = u16("2a36")   # notify: cuff pressure while inflating

# --- Generic Access / Device Information (0x180A) ---
DEVICE_INFO_SERVICE   = u16("180a")
MANUFACTURER_NAME     = u16("2a29")
MODEL_NUMBER          = u16("2a24")
SERIAL_NUMBER         = u16("2a25")
FIRMWARE_REVISION     = u16("2a26")
HARDWARE_REVISION     = u16("2a27")
SOFTWARE_REVISION     = u16("2a23")

# --- Battery Service (0x180F) ---
BATTERY_SERVICE       = u16("180f")
BATTERY_LEVEL         = u16("2a19")

# --- Current Time Service (0x1805) ---
# The BU 572 accepts a writable Current Time characteristic.  We deliberately
# keep it READ-ONLY: setting the clock on a medical device is out of scope
# (see README "Design decisions").
CURRENT_TIME_SERVICE  = u16("1805")
CURRENT_TIME          = u16("2a2b")
