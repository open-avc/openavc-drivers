# Protocol math

The one kind of test this library keeps for a single driver: the arithmetic a
Python driver does on the wire, checked against the manufacturer's own worked
example or table. A checksum or CRC, a binary frame and its length field, a
fader, gain or pan value encoded to bytes, a scene or address computed from a
number.

Every expected value here comes from the manufacturer's document (the test
names the document and the section, table or example) or, where it says so,
from a value read back off the real equipment. A value the test or the
simulator works out for itself does not belong here: it agrees with the
driver by construction.

Everything else about a driver is proven on the equipment and by the shared
checks one folder up, which run over every driver. You do not need to add a
test to contribute a driver.

These tests load the driver on the real OpenAVC platform, so they run in CI's
platform job and skip when the platform is not installed. To run them
locally, point at an OpenAVC checkout:

```bash
OPENAVC_PLATFORM_ROOT=../openavc OPENAVC_REQUIRE_PLATFORM=1 \
    python -m pytest tests/protocol_math -v
```
