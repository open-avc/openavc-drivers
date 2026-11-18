"""Biamp EasyConnect MPX 250, SSH API (firmware 1.3 or later).

Source: Biamp's MPX 250 API Reference Guide, retrieved 2026-11-18:
https://support.biamp.com/Other_products/EasyConnect/MPX_250/MPX_250s_FAQ/MPX_250_API_Reference_Guide

Python is required for the platform's SSH transport. The interactive shell
answers with JSON objects, optionally following an echoed command and prompt.
Requests are serialized and matched by their documented response fields. A
timeout retires the session: a late reply must not answer another request.

The guide names events but does not specify their payloads. Notifications
are refresh hints only; state always comes from documented get/set responses.
Periodic reads also catch changes whose event format is not yet qualified.
The protocol's product code is O-PC-4, not a Biamp model name. It is reported
verbatim and is not used to claim discovery of other Ochno-derived products.
"""

from __future__ import annotations

import asyncio
import codecs
import json
import re
from typing import Any

from openavc.drivers.base import BaseDriver, ConnectionFaultError
from openavc.utils.logger import get_logger

try:
    from openavc.drivers.base import CommandRefusedError
except ImportError:  # The 0.37.0 floor predates the typed command refusal.
    CommandRefusedError = RuntimeError

log = get_logger(__name__)
REPLY_TIMEOUT = 5.0
MAX_BUFFER = 65536
EVENTS = (
    "EVENT_US_PORT_CONNECTION", "EVENT_SYSTEM_STATE_CHANGE",
    "EVENT_USB_PORT_CHANGE", "EVENT_DISPLAY_PORT_CHANGE",
)
IDENTITY = {"sn": "serial_number", "hwId": "hardware_id",
            "product": "product_code", "firmwareversion": "firmware_version"}
# Public setting -> device configuration key. No credentials/network writes.
SETTINGS = {
    "connect_switching": "autoswitchconnect",
    "disconnect_switching": "autoswitchdisconnect",
    "default_input": "defaultport",
    "lock_active_input": "lockactive",
    "disable_empty_buttons": "buttonemptydisabled",
    "button_returns_to_default": "buttonoffdefaultport",
    "disable_leds": "disableleds",
    "hdmi_cec": "hdmiceccontrol",
    "buttons_change_mode": "mtrbuttoncycle",
    "remember_mode": "mtrmodememory",
    "keep_mode_when_idle": "mtrkeepmodeinactive",
    "inherit_mode": "mtrmanualinherit",
    "share_network": "sharenetwork",
}
CONFIG_KEYS = frozenset(SETTINGS.values())
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _flag(value: Any) -> bool | None:
    if type(value) is bool:
        return value
    if type(value) is int and value in (0, 1):
        return bool(value)
    return None


def _port_number(value: Any, *, allow_none: bool = False) -> int:
    if isinstance(value, bool) or not re.fullmatch(r"[0-4]", str(value)):
        raise ValueError("Choose a USB-C input from 1 to 4.")
    number = int(value)
    if number == 0 and not allow_none:
        raise ValueError("Choose a USB-C input from 1 to 4.")
    return number


def _reply_kind(payload: dict) -> str | None:
    fields = (("port", {"active", "mode", "con_in", "con_out"}),
              ("system", set(IDENTITY)), ("events", {"events", "subscriptions"}),
              ("subscribe", {"subscriptions"}), ("network", {"status", "interface"}))
    for kind, required in fields:
        if required <= payload.keys():
            return kind
    if CONFIG_KEYS.intersection(payload):
        return "config"
    return None


class BiampMPX250Driver(BaseDriver):
    DRIVER_INFO = {'id': 'biamp_mpx250',
     'name': 'Biamp EasyConnect MPX 250',
     'manufacturer': 'Biamp',
     'category': 'switcher',
     'version': '1.0.0',
     'author': 'OpenAVC',
     'description': 'Select a USB-C laptop or room-system mode, read connection status and configure '
                    'the hub’s switching policies.',
     'source_url': 'https://support.biamp.com/Other_products/EasyConnect/MPX_250/MPX_250s_FAQ/MPX_250_API_Reference_Guide',
     'transport': 'ssh',
     'ports': [22],
     'min_platform_version': '0.37.0',
     'verified': False,
     'simulated': True,
     'tags': ['usb', 'usb-c', 'byom', 'conferencing', 'hdmi'],
     'compatible_models': [{'manufacturer': 'Biamp',
                            'models': ['EasyConnect MPX 250'],
                            'confidence': 'untested',
                            'notes': 'SSH API, firmware 1.3 or later. One hub’s four front inputs. '
                                     'Expansion and daisy-chain addressing are not qualified.'}],
     'help': {'overview': 'Switch video and USB between four named laptop inputs, or choose '
                          'room-system mode. Connection indicators report attached devices, not an '
                          'active video signal or call.',
              'setup': 'Update to firmware 1.3 or later, enable SSH in MPX Management Console and '
                       'configure the hub’s network and UC cabling. Enter its address and current '
                       'password, then name the four USB-C inputs for the installation. The initial '
                       'password is the last six characters of the Hardware ID under Device Info in '
                       'Workplace Tools. OpenAVC talks directly to the hub; the setup application can '
                       'be closed.',
              'connection': 'Enable SSH in MPX Management Console, use firmware 1.3 or later and enter '
                            'the current admin password. SSH uses port 22.'},
     'default_config': {'host': '',
                        'port': 22,
                        'username': 'admin',
                        'password': '',
                        'ssh_auth_method': 'password',
                        'poll_interval': 5,
                        'input_1_name': 'USB-C 1',
                        'input_2_name': 'USB-C 2',
                        'input_3_name': 'USB-C 3',
                        'input_4_name': 'USB-C 4'},
     'config_schema': {'host': {'type': 'string',
                                'label': 'IP Address',
                                'required': True,
                                'description': 'Address shown in MPX Management Console or Workplace '
                                               'Tools.'},
                       'port': {'type': 'integer',
                                'label': 'SSH Port',
                                'default': 22,
                                'min': 1,
                                'max': 65535,
                                'advanced': True},
                       'username': {'type': 'string',
                                    'label': 'Username',
                                    'default': 'admin',
                                    'advanced': True},
                       'password': {'type': 'string',
                                    'label': 'Password',
                                    'secret': True,
                                    'required': True,
                                    'description': 'Initially the last six characters of the Hardware '
                                                   'ID shown under Device Info in Workplace Tools. '
                                                   'Enter the current password if it has been '
                                                   'changed.'},
                       'poll_interval': {'type': 'integer',
                                         'label': 'Status Refresh Interval (seconds)',
                                         'default': 5,
                                         'min': 1,
                                         'max': 300,
                                         'advanced': True,
                                         'description': 'Refreshes status even when an event '
                                                        'notification is unavailable.'},
                       'input_1_name': {'type': 'string',
                                        'label': 'USB-C 1 Name',
                                        'default': 'USB-C 1'},
                       'input_2_name': {'type': 'string',
                                        'label': 'USB-C 2 Name',
                                        'default': 'USB-C 2'},
                       'input_3_name': {'type': 'string',
                                        'label': 'USB-C 3 Name',
                                        'default': 'USB-C 3'},
                       'input_4_name': {'type': 'string',
                                        'label': 'USB-C 4 Name',
                                        'default': 'USB-C 4'}},
     'state_variables': {'active_input': {'type': 'integer',
                                          'label': 'Active Input',
                                          'min': 0,
                                          'max': 4,
                                          'control': True,
                                          'cloud_priority': 'high',
                                          'help': '0 means no input is selected. USB-C inputs are 1 to '
                                                  '4.'},
                         'active_input_name': {'type': 'string',
                                               'label': 'Active Input Name',
                                               'control': False,
                                               'cloud_priority': 'high'},
                         'mode': {'type': 'enum',
                                  'label': 'Meeting Mode',
                                  'values': [{'value': 'BYOD', 'label': 'Laptop (BYOD)'},
                                             {'value': 'UC', 'label': 'Room system (UC)'}],
                                  'control': True,
                                  'cloud_priority': 'high'},
                         'serial_number': {'type': 'string',
                                           'label': 'Serial Number',
                                           'control': False,
                                           'cloud_priority': 'low'},
                         'hardware_id': {'type': 'string',
                                         'label': 'Hardware ID',
                                         'control': False,
                                         'cloud_priority': 'low'},
                         'product_code': {'type': 'string',
                                          'label': 'Reported Product Code',
                                          'control': False,
                                          'cloud_priority': 'low'},
                         'firmware_version': {'type': 'string',
                                              'label': 'Firmware Version',
                                              'control': False,
                                              'cloud_priority': 'low'},
                         'network_status': {'type': 'string',
                                            'label': 'Network Status',
                                            'control': False,
                                            'cloud_priority': 'low'},
                         'network_interface': {'type': 'string',
                                               'label': 'Network Interface',
                                               'control': False,
                                               'cloud_priority': 'low'},
                         'network_address': {'type': 'string',
                                             'label': 'Network Address',
                                             'control': False,
                                             'cloud_priority': 'low'},
                         'wifi_network': {'type': 'string',
                                          'label': 'Wi-Fi Network',
                                          'control': False,
                                          'cloud_priority': 'low'},
                         'wifi_signal': {'type': 'integer',
                                         'label': 'Wi-Fi Signal',
                                         'unit': 'dBm',
                                         'control': False,
                                         'cloud_priority': 'low'},
                         'events_subscribed': {'type': 'boolean',
                                               'label': 'Event Subscription Active',
                                               'control': False,
                                               'cloud_priority': 'low',
                                               'help': 'The hub confirmed at least one event '
                                                       'subscription. Status is also read '
                                                       'periodically.'},
                         'connect_switching': {'type': 'enum',
                                               'label': 'Switch When a Laptop Connects',
                                               'help': 'Choose when a newly connected laptop takes '
                                                       'control.',
                                               'values': [{'value': 'off', 'label': 'Do not switch'},
                                                          {'value': 'on',
                                                           'label': 'Only when no input is selected'},
                                                          {'value': 'always',
                                                           'label': 'Always select the new laptop'}],
                                               'control': False,
                                               'cloud_priority': 'low'},
                         'disconnect_switching': {'type': 'enum',
                                                  'label': 'Switch When a Laptop Disconnects',
                                                  'help': 'Uses the default input. Last laptop means '
                                                          'the last connected device; active laptop '
                                                          'means the selected input.',
                                                  'values': [{'value': 'off', 'label': 'Do not switch'},
                                                             {'value': 'on',
                                                              'label': 'When the last laptop '
                                                                       'disconnects'},
                                                             {'value': 'active',
                                                              'label': 'When the active laptop '
                                                                       'disconnects'},
                                                             {'value': 'both',
                                                              'label': 'Both conditions'}],
                                                  'control': False,
                                                  'cloud_priority': 'low'},
                         'default_input': {'type': 'enum',
                                           'label': 'Default Input',
                                           'help': 'Input selected at startup and by the disconnect '
                                                   'and button policies that use a default.',
                                           'values': [{'value': '0', 'label': 'None'},
                                                      {'value': '1', 'label': 'USB-C 1'},
                                                      {'value': '2', 'label': 'USB-C 2'},
                                                      {'value': '3', 'label': 'USB-C 3'},
                                                      {'value': '4', 'label': 'USB-C 4'}],
                                           'control': False,
                                           'cloud_priority': 'low'},
                         'lock_active_input': {'type': 'boolean',
                                               'label': 'Lock the Active Input',
                                               'help': 'Prevents switching away from the active port. '
                                                       'In a chain, configure every applicable hub.',
                                               'control': False,
                                               'cloud_priority': 'low'},
                         'disable_empty_buttons': {'type': 'boolean',
                                                   'label': 'Disable Buttons for Empty Inputs',
                                                   'help': 'Prevents the physical buttons selecting an '
                                                           'input with nothing connected.',
                                                   'control': False,
                                                   'cloud_priority': 'low'},
                         'button_returns_to_default': {'type': 'boolean',
                                                       'label': 'Buttons Return to the Default Input',
                                                       'help': 'Deactivating an input with its button '
                                                               'selects the default input instead of '
                                                               'leaving no input selected.',
                                                       'control': False,
                                                       'cloud_priority': 'low'},
                         'disable_leds': {'type': 'boolean',
                                          'label': 'Turn Off Hub LEDs',
                                          'help': 'Turns off all LEDs on the hub hardware.',
                                          'control': False,
                                          'cloud_priority': 'low'},
                         'hdmi_cec': {'type': 'boolean',
                                      'label': 'HDMI CEC Control',
                                      'help': 'Enables the hub’s HDMI CEC control. This does not '
                                              'provide separate display power feedback.',
                                      'control': False,
                                      'cloud_priority': 'low'},
                         'buttons_change_mode': {'type': 'boolean',
                                                 'label': 'Buttons Cycle Laptop and Room System Modes',
                                                 'help': 'Allows the hub’s buttons to cycle between UC '
                                                         'and BYOD modes.',
                                                 'control': False,
                                                 'cloud_priority': 'low'},
                         'remember_mode': {'type': 'boolean',
                                           'label': 'Remember Mode While Idle',
                                           'help': 'Remembers the UC mode after the active input is '
                                                   'cleared. Overrides the per-input default mode.',
                                           'control': False,
                                           'cloud_priority': 'low'},
                         'keep_mode_when_idle': {'type': 'boolean',
                                                 'label': 'Keep Room System Mode While Idle',
                                                 'help': 'Keeps UC mode even when no input is active.',
                                                 'control': False,
                                                 'cloud_priority': 'low'},
                         'inherit_mode': {'type': 'boolean',
                                          'label': 'Keep Mode When Changing Input',
                                          'help': 'A manual input change inherits the current UC mode.',
                                          'control': False,
                                          'cloud_priority': 'low'},
                         'share_network': {'type': 'boolean',
                                           'label': 'Share Network with Laptops',
                                           'help': 'Shares the hub’s network connection through the '
                                                   'USB cable to the laptop. Follow the site’s network '
                                                   'policy.',
                                           'control': False,
                                           'cloud_priority': 'low'}},
     'child_entity_types': {'input': {'label': 'USB-C Input',
                                      'label_plural': 'USB-C Inputs',
                                      'id_format': {'type': 'integer', 'min': 1, 'max': 4},
                                      'label_field': 'label',
                                      'summary_fields': ['connected', 'selected'],
                                      'state_variables': {'label': {'type': 'string',
                                                                    'label': 'Name',
                                                                    'control': False},
                                                          'connected': {'type': 'boolean',
                                                                        'label': 'Connected',
                                                                        'control': False,
                                                                        'cloud_priority': 'high'},
                                                          'selected': {'type': 'boolean',
                                                                       'label': 'Selected',
                                                                       'control': False,
                                                                       'cloud_priority': 'high'}}},
                            'peripheral': {'label': 'Rear Port',
                                           'label_plural': 'Rear Ports',
                                           'id_format': {'type': 'integer', 'min': 5, 'max': 8},
                                           'label_field': 'label',
                                           'summary_fields': ['connected'],
                                           'state_variables': {'label': {'type': 'string',
                                                                         'label': 'Name',
                                                                         'control': False},
                                                               'connected': {'type': 'boolean',
                                                                             'label': 'Connected',
                                                                             'control': False,
                                                                             'cloud_priority': 'low'}}}},
     'device_settings': {'connect_switching': {'type': 'enum',
                                               'label': 'Switch When a Laptop Connects',
                                               'state_key': 'connect_switching',
                                               'default': 'on',
                                               'setup': False,
                                               'help': 'Choose when a newly connected laptop takes '
                                                       'control.',
                                               'values': [{'value': 'off', 'label': 'Do not switch'},
                                                          {'value': 'on',
                                                           'label': 'Only when no input is selected'},
                                                          {'value': 'always',
                                                           'label': 'Always select the new laptop'}]},
                         'disconnect_switching': {'type': 'enum',
                                                  'label': 'Switch When a Laptop Disconnects',
                                                  'state_key': 'disconnect_switching',
                                                  'default': 'active',
                                                  'setup': False,
                                                  'help': 'Uses the default input. Last laptop means '
                                                          'the last connected device; active laptop '
                                                          'means the selected input.',
                                                  'values': [{'value': 'off', 'label': 'Do not switch'},
                                                             {'value': 'on',
                                                              'label': 'When the last laptop '
                                                                       'disconnects'},
                                                             {'value': 'active',
                                                              'label': 'When the active laptop '
                                                                       'disconnects'},
                                                             {'value': 'both',
                                                              'label': 'Both conditions'}]},
                         'default_input': {'type': 'enum',
                                           'label': 'Default Input',
                                           'state_key': 'default_input',
                                           'default': '0',
                                           'setup': False,
                                           'help': 'Input selected at startup and by the disconnect '
                                                   'and button policies that use a default.',
                                           'values': [{'value': '0', 'label': 'None'},
                                                      {'value': '1', 'label': 'USB-C 1'},
                                                      {'value': '2', 'label': 'USB-C 2'},
                                                      {'value': '3', 'label': 'USB-C 3'},
                                                      {'value': '4', 'label': 'USB-C 4'}]},
                         'lock_active_input': {'type': 'boolean',
                                               'label': 'Lock the Active Input',
                                               'state_key': 'lock_active_input',
                                               'default': False,
                                               'setup': False,
                                               'help': 'Prevents switching away from the active port. '
                                                       'In a chain, configure every applicable hub.'},
                         'disable_empty_buttons': {'type': 'boolean',
                                                   'label': 'Disable Buttons for Empty Inputs',
                                                   'state_key': 'disable_empty_buttons',
                                                   'default': False,
                                                   'setup': False,
                                                   'help': 'Prevents the physical buttons selecting an '
                                                           'input with nothing connected.'},
                         'button_returns_to_default': {'type': 'boolean',
                                                       'label': 'Buttons Return to the Default Input',
                                                       'state_key': 'button_returns_to_default',
                                                       'default': False,
                                                       'setup': False,
                                                       'help': 'Deactivating an input with its button '
                                                               'selects the default input instead of '
                                                               'leaving no input selected.'},
                         'disable_leds': {'type': 'boolean',
                                          'label': 'Turn Off Hub LEDs',
                                          'state_key': 'disable_leds',
                                          'default': False,
                                          'setup': False,
                                          'help': 'Turns off all LEDs on the hub hardware.'},
                         'hdmi_cec': {'type': 'boolean',
                                      'label': 'HDMI CEC Control',
                                      'state_key': 'hdmi_cec',
                                      'default': False,
                                      'setup': False,
                                      'help': 'Enables the hub’s HDMI CEC control. This does not '
                                              'provide separate display power feedback.'},
                         'buttons_change_mode': {'type': 'boolean',
                                                 'label': 'Buttons Cycle Laptop and Room System Modes',
                                                 'state_key': 'buttons_change_mode',
                                                 'default': False,
                                                 'setup': False,
                                                 'help': 'Allows the hub’s buttons to cycle between UC '
                                                         'and BYOD modes.'},
                         'remember_mode': {'type': 'boolean',
                                           'label': 'Remember Mode While Idle',
                                           'state_key': 'remember_mode',
                                           'default': False,
                                           'setup': False,
                                           'help': 'Remembers the UC mode after the active input is '
                                                   'cleared. Overrides the per-input default mode.'},
                         'keep_mode_when_idle': {'type': 'boolean',
                                                 'label': 'Keep Room System Mode While Idle',
                                                 'state_key': 'keep_mode_when_idle',
                                                 'default': False,
                                                 'setup': False,
                                                 'help': 'Keeps UC mode even when no input is active.'},
                         'inherit_mode': {'type': 'boolean',
                                          'label': 'Keep Mode When Changing Input',
                                          'state_key': 'inherit_mode',
                                          'default': False,
                                          'setup': False,
                                          'help': 'A manual input change inherits the current UC '
                                                  'mode.'},
                         'share_network': {'type': 'boolean',
                                           'label': 'Share Network with Laptops',
                                           'state_key': 'share_network',
                                           'default': False,
                                           'setup': False,
                                           'help': 'Shares the hub’s network connection through the '
                                                   'USB cable to the laptop. Follow the site’s network '
                                                   'policy.'}},
     'commands': {'refresh_status': {'label': 'Refresh Status',
                                     'help': 'Read the selected input, meeting mode and all front and '
                                             'rear connection indicators.',
                                     'query_for': 'active_input',
                                     'params': {}},
                  'select_input': {'label': 'Select Input',
                                   'help': 'Select a front USB-C input for video and USB. The hub’s '
                                           'active-input lock and UC configuration still apply.',
                                   'params': {'input': {'type': 'child_id',
                                                        'child_type': 'input',
                                                        'label': 'Input',
                                                        'required': True}},
                                   'sets': {'active_input': '{input}'}},
                  'set_mode': {'label': 'Set Meeting Mode',
                               'help': 'Use the laptop or the installed room system. Configure the '
                                       'room system’s cabling and UC role in MPX Management Console '
                                       'first.',
                               'params': {'mode': {'type': 'enum',
                                                   'label': 'Mode',
                                                   'required': True,
                                                   'values': [{'value': 'BYOD',
                                                               'label': 'Laptop (BYOD)'},
                                                              {'value': 'UC',
                                                               'label': 'Room system (UC)'}]}},
                               'sets': {'mode': '{mode}'}},
                  'use_room_system': {'label': 'Use Room System',
                                      'help': 'Select UC mode using the hub’s installed room-system '
                                              'configuration.',
                                      'params': {},
                                      'sets': {'mode': 'UC'}},
                  'use_laptop': {'label': 'Use Laptop',
                                 'help': 'Select BYOD mode so the selected laptop uses the shared '
                                         'peripherals.',
                                 'params': {},
                                 'sets': {'mode': 'BYOD'}},
                  'read_settings': {'label': 'Read Hub Settings',
                                    'help': 'Refresh the switching, button, meeting-mode and '
                                            'network-sharing settings from the hub.',
                                    'params': {},
                                    'query_for': 'connect_switching'},
                  'read_system': {'label': 'Read System Information',
                                  'help': 'Read firmware, serial number, Hardware ID and the product '
                                          'code reported by the hub.',
                                  'params': {},
                                  'query_for': 'firmware_version'},
                  'read_network': {'label': 'Read Network Status',
                                   'help': 'Read the active network interface and address, including '
                                           'Wi-Fi signal when reported.',
                                   'params': {},
                                   'query_for': 'network_interface'},
                  'restart_controller': {'label': 'Restart Controller',
                                         'help': 'Restart the hub’s CPU. The control connection closes '
                                                 'and OpenAVC reconnects.',
                                         'params': {},
                                         'confirm': 'Restart the MPX 250 controller? Control will be '
                                                    'unavailable while it restarts.'},
                  'restart_hub': {'label': 'Restart Entire Hub',
                                  'help': 'Cut power to the entire hub and restart it. Attached '
                                          'devices may lose their connection and charging.',
                                  'params': {},
                                  'confirm': 'Restart the entire MPX 250? This interrupts connected '
                                             'laptops, peripherals and charging.'}},
     'quick_actions': ['select_input', 'use_laptop', 'use_room_system', 'refresh_status'],
     'routing': {'source_child_type': 'input',
                 'command': 'select_input',
                 'source_param': 'input',
                 'planes': [{'label': 'USB-C Video and USB', 'route_property': 'active_input'}]}}

    HEALTH_FAULT_MESSAGE = "The MPX 250 stopped answering status requests."

    def __init__(self, device_id: str, config: dict, state: Any, events: Any):
        super().__init__(device_id, config, state, events)
        self._buffer = ""
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._request_lock = asyncio.Lock()
        self._waiting: tuple[str, asyncio.Future] | None = None
        self._retired = False
        self._subscribed = False
        self._refresh_task: asyncio.Task | None = None
        self._refresh_again = False
        self._owned_tasks: set[asyncio.Task] = set()

    def _resolve_delimiter(self) -> None:
        return None  # SSH and the TCP simulator both deliver raw chunks.

    async def _pre_connect(self) -> None:
        self._retired = False
        if not str(self.config.get("host", "")).strip():
            raise ConnectionFaultError("Enter the MPX 250 address.", code="invalid_config")
        transport = self.config.get("transport", "ssh")
        if transport == "ssh" and not self.config.get("password"):
            raise ConnectionFaultError("The MPX 250 password is missing.", code="auth_failed")

    async def _post_connect(self) -> None:
        # The transport starts OpenSSH before its login exchange has finished.
        identity = await self._request("system", "system", timeout=30)
        self._apply_identity(identity)
        version = re.match(r"(\d+)\.(\d+)", str(identity["firmwareversion"]))
        if version and tuple(map(int, version.groups())) < (1, 3):
            raise ConnectionFaultError(
                "Update the MPX 250 to firmware 1.3 or later in MPX Management Console.",
                code="invalid_config",
            )
        self._register_ports()
        await self._read("port")

    async def _initial_sync(self) -> None:
        await self._read("config")
        await self._read("network")
        listed = await self._request("event list", "events")
        offered = listed.get("events", [])
        wanted = [event for event in EVENTS if event in offered]
        if wanted:
            result = await self._request(
                "event subscribe " + json.dumps({"events": wanted}), "subscribe",
            )
            self._subscribed = bool(set(wanted).intersection(result["subscriptions"]))
        self.set_state("events_subscribed", self._subscribed)

    async def _close_session(self) -> None:
        self._retired = True
        self._subscribed = False
        self._refresh_again = False
        if self._waiting is not None:
            self._waiting[1].cancel()
            self._waiting = None
        current = asyncio.current_task()
        tasks = [task for task in self._owned_tasks if task is not current]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._owned_tasks.clear()
        self._refresh_task = None
        self._buffer = ""
        self._decoder.reset()
        self.set_state("events_subscribed", False)

    def _register_ports(self) -> None:
        for number in range(1, 5):
            label = str(self.config.get(f"input_{number}_name") or f"USB-C {number}")
            self.register_child("input", number, {"label": label})
            self.set_child_state("input", number, "label", label)
        for number in range(5, 9):
            self.register_child("peripheral", number, {"label": f"Rear port {number}"})

    async def _request(self, line: str, kind: str, *, timeout: float = REPLY_TIMEOUT) -> dict:
        async with self._request_lock:
            self._require_session()
            future = asyncio.get_running_loop().create_future()
            self._waiting = (kind, future)
            try:
                await self.transport.send((line + "\r").encode())
                return await asyncio.wait_for(future, timeout)
            except TimeoutError as exc:
                self._retired = True
                raise ConnectionFaultError(
                    "The MPX 250 did not answer a control request. Check that SSH is "
                    "enabled and firmware 1.3 or later is installed.", code="no_response",
                ) from exc
            finally:
                self._waiting = None

    def _require_session(self) -> None:
        if self._retired:
            raise ConnectionError("The MPX 250 control session is no longer synchronized.")
        if self.transport is None or not self.transport.connected:
            raise ConnectionError("The MPX 250 control session is not available.")

    async def on_data_received(self, data: bytes) -> None:
        # Do not suspend here: older platforms schedule receive callbacks.
        self._buffer += self._decoder.decode(data)
        if len(self._buffer) > MAX_BUFFER:
            self._buffer = ""
            log.warning("[%s] MPX 250 reply exceeded the receive limit", self.device_id)
            return
        while self._consume_buffer():
            pass

    def _consume_buffer(self) -> bool:
        self._buffer = self._buffer.lstrip(" \t\r\n>")
        if not self._buffer:
            return False
        # JSON may be pretty-printed or have no line ending yet.
        if self._buffer.startswith("{"):
            return self._consume_json()
        ending = re.search(r"[\r\n]", self._buffer)
        if ending is None:
            return False
        line = ANSI.sub("", self._buffer[:ending.start()]).strip()
        self._buffer = self._buffer[ending.end():]
        # A terminal may decorate its prompt with ANSI codes.
        if line.lstrip("> ").startswith("{"):
            self._accept_json_text(line.lstrip("> "))
        elif not line.startswith(("event subscribe ", "event unsubscribe ")):
            self._notice_event(line)
        return True

    def _consume_json(self) -> bool:
        try:
            payload, end = json.JSONDecoder().raw_decode(self._buffer)
        except ValueError:
            # A malformed complete line must not hide all later replies.
            ending = re.search(r"[\r\n]", self._buffer)
            if ending and not self._buffer[:ending.start()].rstrip().endswith(("{", ",")):
                self._buffer = self._buffer[ending.end():]
                return True
            return False
        self._buffer = self._buffer[end:]
        self._accept_object(payload)
        return True

    def _accept_json_text(self, line: str) -> None:
        try:
            self._accept_object(json.loads(line))
        except ValueError:
            return

    def _accept_object(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        if self._notice_event(json.dumps(payload)) and "subscriptions" not in payload:
            return
        waiting = self._waiting
        if waiting is None or waiting[1].done():
            return
        if _reply_kind(payload) == waiting[0]:
            waiting[1].set_result(payload)

    def _notice_event(self, text: str) -> bool:
        found = any(event in text for event in EVENTS)
        active = self._subscribed and not self._retired
        if found and active:
            self._queue_refresh()
        return found

    def _queue_refresh(self) -> None:
        self._refresh_again = True
        if self._refresh_task is not None and not self._refresh_task.done():
            return
        spawn = getattr(self, "spawn_task", None)
        task = spawn(self._event_refresh()) if spawn else asyncio.create_task(self._event_refresh())
        self._refresh_task = task
        self._owned_tasks.add(task)
        task.add_done_callback(self._refresh_finished)

    def _refresh_finished(self, task: asyncio.Task) -> None:
        self._owned_tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.warning("[%s] MPX 250 event refresh failed: %s", self.device_id, task.exception())

    async def _event_refresh(self) -> None:
        while self._refresh_again:
            await asyncio.sleep(0.5)
            self._refresh_again = False
            await self._read("port")
            await self._read("config")

    async def _read(self, kind: str) -> dict:
        line = "system" if kind == "system" else f"{kind} get"
        payload = await self._request(line, kind)
        self._apply_report(kind, payload)
        return payload

    def _apply_report(self, kind: str, payload: dict) -> None:
        handlers = {"system": self._apply_identity, "port": self._apply_ports,
                    "config": self._apply_settings, "network": self._apply_network}
        handlers[kind](payload)

    def _apply_identity(self, payload: dict) -> None:
        for source, target in IDENTITY.items():
            if isinstance(payload.get(source), str):
                self.set_state(target, payload[source])

    def _apply_ports(self, payload: dict) -> None:
        self._apply_active_input(payload.get("active"))
        mode = payload.get("mode")
        self.set_state("mode", mode if mode in ("UC", "BYOD") else None)
        self._apply_connections("input", 1, payload.get("con_in"))
        self._apply_connections("peripheral", 5, payload.get("con_out"))

    def _apply_active_input(self, active: Any) -> None:
        if type(active) is int and 0 <= active <= 4:
            self.set_state("active_input", active)
            label = self.get_child_state("input", active).get("label") if active else "None"
            self.set_state("active_input_name", label or f"USB-C {active}")
            for number in range(1, 5):
                self.set_child_state("input", number, "selected", active == number)
            return
        # A chain or newer firmware may report an address outside this hub.
        self.set_state("active_input", None)
        self.set_state("active_input_name", "")
        for number in range(1, 5):
            self.set_child_state("input", number, "selected", None)

    def _apply_connections(self, child_type: str, start: int, values: Any) -> None:
        if not isinstance(values, list) or len(values) != 4:
            values = [None] * 4
        for offset, value in enumerate(values):
            self.set_child_state(child_type, start + offset, "connected", _flag(value))

    def _setting_value(self, key: str, raw: Any) -> Any:
        declaration = self.DRIVER_INFO["device_settings"][key]
        if declaration["type"] == "boolean":
            return _flag(raw)
        values = [entry["value"] for entry in declaration["values"]]
        value = str(raw)
        return value if value in values else None

    def _apply_settings(self, payload: dict) -> None:
        for key, wire_key in SETTINGS.items():
            value = self._setting_value(key, payload.get(wire_key))
            if value is not None:
                self.set_state(key, value)

    def _apply_network(self, payload: dict) -> None:
        fields = {"status": "network_status", "interface": "network_interface",
                  "ip": "network_address", "ssid": "wifi_network"}
        for source, target in fields.items():
            value = payload.get(source)
            if isinstance(value, str):
                self.set_state(target, value)
        if type(payload.get("wifirssi")) is int:
            self.set_state("wifi_signal", payload["wifirssi"])
        if payload.get("interface") == "Ethernet":
            self.set_state("wifi_network", "")
            self.set_state("wifi_signal", None)

    async def poll(self) -> None:
        await self._read("port")
        await self._read("config")

    async def refresh_children(self) -> bool:
        self._register_ports()
        await self._read("port")
        return True

    async def _liveness_probe(self) -> None:
        await self._read("system")

    async def _write_port(self, changes: dict) -> bool:
        result = await self._request("port set " + json.dumps(changes), "port")
        self._apply_ports(result)
        # The set response is documented as the current port report.
        if any(result.get(key) != value for key, value in changes.items()):
            raise CommandRefusedError(
                "The MPX 250 did not apply the selection. Check the active-input lock, "
                "connected laptop and UC configuration in MPX Management Console."
            )
        return True

    async def set_device_setting(self, key: str, value: Any) -> bool:
        if key not in SETTINGS:
            raise ValueError(f"Unknown MPX 250 setting: {key}")
        normal = self._setting_value(key, value)
        if normal is None:
            raise ValueError(f"Choose a valid value for {self.DRIVER_INFO['device_settings'][key]['label']}.")
        wire = int(normal) if isinstance(normal, bool) or key == "default_input" else normal
        changes = {SETTINGS[key]: wire}
        response = await self._request("config set " + json.dumps(changes), "config")
        self._apply_settings(response)
        # Read once more: the device owns persistence and may reject a setting combination.
        confirmed = await self._read("config")
        if self._setting_value(key, confirmed.get(SETTINGS[key])) != normal:
            raise CommandRefusedError("The MPX 250 did not apply that setting. Check its configuration in MPX Management Console.")
        return True

    async def send_command(self, command: str, params: dict | None = None) -> Any:
        reads = {"refresh_status": "port", "read_settings": "config",
                 "read_system": "system", "read_network": "network"}
        if command in reads:
            await self._read(reads[command])
            return True
        if command == "select_input":
            return await self._write_port({"active": _port_number((params or {}).get("input"))})
        if command in ("restart_controller", "restart_hub"):
            return await self._restart(command)
        return await self._set_meeting_mode(command, params or {})

    async def _set_meeting_mode(self, command: str, values: dict) -> bool:
        modes = {"use_room_system": "UC", "use_laptop": "BYOD"}
        if command in modes:
            return await self._write_port({"mode": modes[command]})
        if command == "set_mode":
            mode = values.get("mode")
            if mode not in ("UC", "BYOD"):
                raise ValueError("Choose Room system or Laptop.")
            return await self._write_port({"mode": mode})
        raise ValueError(f"Unknown MPX 250 command: {command}")

    async def _restart(self, command: str) -> bool:
        async with self._request_lock:
            self._require_session()
            kind = "soft" if command == "restart_controller" else "hard"
            await self.transport.send(f"reboot {kind}\r".encode())
            self._retired = True  # Reboot has no reply and closes the shell.
        return True
