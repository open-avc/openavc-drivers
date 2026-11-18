"""Loopback BACnet controller with real objects, priority arbitration and COV.

The protocol is provided by BACpypes3, independently of the driver's client
adapter. Changing a control updates a BACnet object, whose own COV detector
notifies subscribed clients. No traffic leaves loopback.
"""
from __future__ import annotations

import asyncio

from bacpypes3.app import Application
from bacpypes3.errors import ExecutionError
from bacpypes3.local.cov import GenericCriteria
from bacpypes3.local.device import DeviceObject
from bacpypes3.local.networkport import NetworkPortObject
from bacpypes3.local.analog import AnalogInputObject, AnalogValueObjectCmd
from bacpypes3.local.binary import BinaryInputObject, BinaryOutputObject
from bacpypes3.local.multistate import MultiStateOutputObject
from bacpypes3.basetypes import PriorityValue
from openavc.simulator.base import BaseSimulator


class Controller(Application):
    owner = None

    def request(self, apdu):
        if self.owner and self.owner._silenced:
            future = asyncio.get_running_loop().create_future()
            future.set_result(None)
            return future
        return super().request(apdu)

    async def indication(self, apdu):
        if self.owner and self.owner.has_error_behavior("no_response"):
            return
        await super().indication(apdu)

    async def _refuse(self, apdu, code):
        raise ExecutionError(errorClass="services", errorCode=code)

    async def do_SubscribeCOVRequest(self, apdu):
        if self.owner and "cov_unavailable" in self.owner._active_errors:
            await self._refuse(apdu, "cov-subscription-failed")
            return
        await super().do_SubscribeCOVRequest(apdu)

    async def do_ReadPropertyMultipleRequest(self, apdu):
        if self.owner and "bulk_unavailable" in self.owner._active_errors:
            await self._refuse(apdu, "optional-functionality-not-supported")
            return
        await super().do_ReadPropertyMultipleRequest(apdu)

    async def do_WritePropertyRequest(self, apdu):
        if self.owner and "writes_denied" in self.owner._active_errors:
            await self._refuse(apdu, "write-access-denied")
            return
        await super().do_WritePropertyRequest(apdu)


class ModeObject(MultiStateOutputObject):
    # BACpypes3 0.1.0 assigns numeric COV-increment criteria to multistate
    # objects, which have no covIncrement. State changes use generic COV.
    _cov_criteria = GenericCriteria


def make_objects():
    return {
        "temperature": AnalogInputObject(objectIdentifier=("analog-input", 1), objectName="Room Temperature",
                                          presentValue=22.5, units="degrees-celsius", covIncrement=0.1),
        "setpoint": AnalogValueObjectCmd(objectIdentifier=("analog-value", 1), objectName="Room Setpoint",
                                        presentValue=21.0, units="degrees-celsius", covIncrement=0.1,
                                        minPresValue=10.0, maxPresValue=35.0, resolution=0.5),
        "occupancy": BinaryInputObject(objectIdentifier=("binary-input", 1), objectName="Room Occupancy",
                                       presentValue="inactive", inactiveText="Vacant", activeText="Occupied"),
        "occupied_mode": BinaryOutputObject(objectIdentifier=("binary-output", 1), objectName="Room Occupied Mode",
                                             presentValue="inactive", inactiveText="Unoccupied", activeText="Occupied"),
        "mode": ModeObject(objectIdentifier=("multi-state-output", 1), objectName="Room HVAC Mode",
                                        presentValue=1, numberOfStates=3, stateText=["Auto", "Heating", "Cooling"]),
    }


class BACnetIPSimulator(BaseSimulator):
    SIMULATOR_INFO = {
        "driver_id": "bacnet_ip", "name": "BACnet/IP Controller", "category": "utility", "transport": "udp",
        "initial_state": {"temperature": 22.5, "setpoint": 21.0, "occupancy": False,
                          "occupied_mode": False, "mode": 1, "higher_priority": False, "extra_points": 0},
        "controls": [
            {"key": "temperature", "type": "slider", "label": "Room temperature", "min": 10, "max": 35, "step": 0.5},
            {"key": "setpoint", "type": "slider", "label": "Setpoint at priority 16", "min": 10, "max": 35, "step": 0.5},
            {"key": "occupancy", "type": "toggle", "label": "Room occupied"},
            {"key": "occupied_mode", "type": "toggle", "label": "Occupied mode"},
            {"key": "mode", "type": "select", "label": "HVAC mode", "options": [1, 2, 3], "labels": {"1": "Auto", "2": "Heating", "3": "Cooling"}},
            {"key": "higher_priority", "type": "toggle", "label": "Facilities override: 19 C at priority 8"},
            {"key": "extra_points", "type": "select", "label": "Additional import points", "options": [0, 40, 140],
             "labels": {"0": "None", "40": "40 points (multiple batches)", "140": "140 points (exceeds import capacity)"}},
        ],
        "error_modes": {
            "cov_unavailable": {"description": "Controller refuses COV subscriptions"},
            "bulk_unavailable": {"description": "Controller only supports individual property reads"},
            "writes_denied": {"description": "Controller refuses writes"},
        },
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.app = None
        self.objects = {}
        self._extra_points = []

    async def start(self, port):
        self.objects = make_objects()
        device = DeviceObject(objectIdentifier=("device", 12345), objectName="Demo Building Controller",
                              vendorIdentifier=999, vendorName="Simulator", modelName="Room Controller",
                              firmwareRevision="1.0")
        network = NetworkPortObject(f"127.0.0.1:{port}", objectIdentifier=("network-port", 1), objectName="Loopback")
        self.app = Controller.from_object_list([device, network, *self.objects.values()])
        self.app.owner = self
        self._extra_points = []
        self._resize_import_points(int(self.state.get("extra_points", 0)))
        layer = next(iter(self.app.link_layers.values()))
        await asyncio.wait_for(asyncio.gather(*layer.server._transport_tasks), 3)
        self._port = layer.server.local_transport.get_extra_info("sockname")[1]
        for name, obj in self.objects.items():
            obj._property_monitors["presentValue"].append(self._monitor(name))
        self._running = True

    def _monitor(self, name):
        def changed(old, new):
            value = bool(int(new)) if name in ("occupancy", "occupied_mode") else new
            BaseSimulator.set_state(self, name, value)
        return changed

    def set_state(self, key, value):
        super().set_state(key, value)
        if key == "extra_points":
            self._resize_import_points(int(value))
            return
        if key == "higher_priority":
            self._set_override(value)
            return
        obj = self.objects.get(key)
        if obj is None:
            return
        if key in ("occupancy", "occupied_mode"):
            value = "active" if value else "inactive"
        obj.presentValue = value

    def _resize_import_points(self, count):
        if not self.app or count not in (0, 40, 140):
            return
        for obj in self._extra_points:
            self.app.delete_object(obj)
        self._extra_points = []
        for i in range(count):
            obj = AnalogInputObject(objectIdentifier=("analog-input", 100 + i),
                                    objectName=f"Extra Room Temperature {i + 1:03d}",
                                    presentValue=22.0, units="degrees-celsius", covIncrement=0.1)
            self.app.add_object(obj)
            self._extra_points.append(obj)

    def _set_override(self, enabled):
        if not self.objects:
            return
        entry = PriorityValue(real=19.0) if enabled else PriorityValue(null=())
        self.objects["setpoint"].priorityArray[7] = entry

    async def stop(self):
        if self.app:
            for detector in list(self.app._cov_detections.values()):
                for subscription in list(detector.cov_subscriptions):
                    subscription.cancel_subscription()
            self.app.close()
            self.app = None
        self._running = False
        await asyncio.sleep(0)
