"""The ONVIF discovery companion reads a ProbeMatch the way a camera sends it.

The AXIS P3265-V that first met this companion (2026-09-08) announced its name
percent-encoded (``AXIS%20P3265-V``), no manufacturer scope at all, and an
endpoint UUID that the scan card then showed as a serial number. Scopes are
RFC 3986 URIs, so the decoding is the spec's, and the serial is its own scope.
"""

from __future__ import annotations

from pathlib import Path

from _platform_stubs import install_stubs, load_module

REPO_ROOT = Path(__file__).resolve().parent.parent

install_stubs()
COMPANION = load_module(
    "onvif_camera_discovery_under_test",
    REPO_ROOT / "cameras" / "onvif_camera_discovery.py",
)

# What an AXIS P3265-V on factory firmware answers, trimmed to the elements
# the companion reads (captured 2026-09-08).
AXIS_PROBE_MATCH = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope" '
    'xmlns:wsa="http://schemas.xmlsoap.org/ws/2004/08/addressing" '
    'xmlns:wsdd="http://schemas.xmlsoap.org/ws/2005/04/discovery" '
    'xmlns:dn="http://www.onvif.org/ver10/network/wsdl">'
    "<SOAP-ENV:Header/><SOAP-ENV:Body><wsdd:ProbeMatches><wsdd:ProbeMatch>"
    "<wsa:EndpointReference><wsa:Address>urn:uuid:fa4113a5-3ed5-4390-b9c7-e517de5aed85</wsa:Address></wsa:EndpointReference>"
    "<wsdd:Types>dn:NetworkVideoTransmitter tds:Device</wsdd:Types>"
    "<wsdd:Scopes>onvif://www.onvif.org/Profile/Streaming onvif://www.onvif.org/Profile/G "
    "onvif://www.onvif.org/hardware/P3265-V onvif://www.onvif.org/name/AXIS%20P3265-V "
    "onvif://www.onvif.org/Profile/M onvif://www.onvif.org/Profile/T onvif://www.onvif.org/location/</wsdd:Scopes>"
    "<wsdd:XAddrs>http://192.168.1.122/onvif/device_service https://192.168.1.122/onvif/device_service</wsdd:XAddrs>"
    "<wsdd:MetadataVersion>1</wsdd:MetadataVersion>"
    "</wsdd:ProbeMatch></wsdd:ProbeMatches></SOAP-ENV:Body></SOAP-ENV:Envelope>"
).encode()


def test_axis_probe_match_reads_a_decoded_name_and_no_invented_fields():
    match = COMPANION._parse_probe_match(AXIS_PROBE_MATCH, "192.168.1.122")
    assert match is not None
    response = COMPANION._build_response(match)
    assert response["model"] == "P3265-V"
    assert response["device_name"] == "AXIS P3265-V"
    assert response["endpoint_reference"] == "urn:uuid:fa4113a5-3ed5-4390-b9c7-e517de5aed85"
    # AXIS publishes neither a manufacturer nor a serial scope: nothing is made up.
    assert "manufacturer" not in response
    assert "serial_number" not in response
    assert response["xaddrs"][0] == "http://192.168.1.122/onvif/device_service"
    assert COMPANION._build_txt(match) == {"hardware": "P3265-V"}


def test_serial_and_mac_scopes_are_read_when_a_camera_publishes_them():
    body = AXIS_PROBE_MATCH.replace(
        b"onvif://www.onvif.org/location/",
        b"onvif://www.onvif.org/SerialNumber/ACCC8E123456 "
        b"onvif://www.onvif.org/MacAddress/ac:cc:8e:12:34:56 "
        b"onvif://www.onvif.org/manufacturer/Acme%20Cameras",
    )
    response = COMPANION._build_response(COMPANION._parse_probe_match(body, "10.0.0.5"))
    assert response["serial_number"] == "ACCC8E123456"
    assert response["mac_address"] == "ac:cc:8e:12:34:56"
    assert response["manufacturer"] == "Acme Cameras"
