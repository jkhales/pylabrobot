from dataclasses import dataclass, field, fields
import datetime
import enum
from functools import wraps
import os
import platform
import textwrap
import xml
import xml.etree.ElementTree as ET
import re
import asyncio
from typing import List, Literal, Optional

import requests

SOAP_NAMESPACE = {
    "SOAP-ENV": "http://schemas.xmlsoap.org/soap/envelope/",
    "s": "http://sila.coop",
}

import xml.etree.ElementTree as ET
from dataclasses import asdict, is_dataclass
from typing import Any
import datetime


def to_pascal_case(snake_str: str) -> str:
    """Convert snake_case or lower case strings to PascalCase."""
    components = snake_str.split("_")
    return "".join(x.title() for x in components)


def to_camel_case(snake_str: str) -> str:
    """Convert snake_case or lower case strings to camelCase."""
    components = snake_str.split("_")
    return components[0] + "".join(x.title() for x in components[1:])


class DeviceState(enum.Enum):
    IDLE = "idle"
    BUSY = "busy"


@dataclass(frozen=True)
class MetaData:
    method_name: str
    creator: str = ""
    description: str = ""
    date_time: datetime.datetime = field(default_factory=datetime.datetime.now)

    def to_dict(self):
        output = {}
        for field in fields(self):
            field_name = field.name
            value = getattr(self, field_name)
            output[to_camel_case(field_name)] = (
                value.isoformat() if field_name == "date_time" else (value or "")
            )
        return output


@dataclass(frozen=True)
class PreMethod:
    target_block_temperature: int  # °C
    target_lid_temperature: int  # °C
    metadata: MetaData

    def to_xml(self):
        root = ET.Element("PreMethod")

        for field in fields(self):
            if field.name == "metadata":
                continue
            child_element = ET.Element(to_pascal_case(field.name))
            child_element.text = str(getattr(self, field.name))
            root.append(child_element)

        root.attrib.update(self.metadata.to_dict())

        return root


@dataclass(frozen=True)
class MethodStep:
    number: int
    slope: float  # °C/s
    plateau_temperature: int
    plateau_time: int  # seconds
    over_shoot_slope_1: float  # °C/s
    over_shoot_temperature: int
    over_shoot_time: int  # seconds
    over_shoot_slope_2: float  # °C/s
    goto_number: int  # start new loop, must be smaller than step number
    loop_number: int
    pid_number: int
    lid_temp: int  # °C

    def to_xml(self):
        root = ET.Element("Step")

        for field in fields(self):
            child_element = ET.Element(to_pascal_case(field.name))
            child_element.text = str(getattr(self, field.name))
            root.append(child_element)

        return root


@dataclass(frozen=True)
class PIDMember:
    """
    The PID set refers to the parameters used to tune the PID controller:

    Kp (Proportional Gain): Determines how much the error affects the system.
      A higher value increases the controller's response to current error.
    Ki (Integral Gain): Adjusts the response based on the accumulation of past errors.
      It eliminates steady-state errors but can introduce oscillations if too high.
    Kd (Derivative Gain): Adjusts the controller based on the rate of change of the error,
      helping to smooth the response and prevent overshooting

    Defaults copied from manual.
    """

    number: str = "1"
    p_heating: int = 60  # proportional gain (Kp)
    p_cooling: int = 80  # proportional gain (Kp)
    i_heating: int = 250  # integral gain (Ki)
    i_cooling: int = 100  # integral gain (Ki)
    d_heating: int = 10  # derivative gain (Kd)
    d_cooling: int = 10  # derivative gain (Kd)
    p_lid: int = 100  # proportional gain (Kp)
    i_lid: int = 70  # integral gain (Ki)

    def to_xml(self):
        root = ET.Element("PID")

        for field in fields(self):
            if field.name == "number":
                continue
            child_element = ET.Element(to_pascal_case(field.name))
            child_element.text = str(getattr(self, field.name))
            root.append(child_element)

        root.attrib.update({"number": str(self.number)})

        return root


@dataclass(frozen=True)
class Method:
    variant: Literal[
        960000, 96
    ]  # must match the hardware variant of the INHECO ODTC device
    plate_type: int
    fluid_quantity: int
    post_heating: bool
    start_block_temperature: int
    start_lid_temperature: int
    start_lid_temperature: int
    steps: list[MethodStep]
    pid_set: list[PIDMember]
    metadata: MetaData

    def to_xml(self):
        root = ET.Element("Method")

        for field in fields(self):
            if field.name in ["metadata", "steps", "pid_set"]:
                continue
            child_element = ET.Element(to_pascal_case(field.name))
            child_element.text = str(getattr(self, field.name))
            root.append(child_element)

        for step in self.steps:
            root.append(step.to_xml())
        pid_root = ET.Element("PIDSet")
        for pid_element in self.pid_set:
            pid_root.append(pid_element.to_xml())
        root.append(pid_root)
        root.attrib.update(self.metadata.to_dict())

        return root


class ThermoCyclerStep:
    def __init__(
        self, temperature: float, duration: float, lid_temperature: float, slope: float
    ):
        self.temperature = temperature
        self.duration = duration
        self.lid_temperature = lid_temperature
        self.slope = slope


def wait_until_idle(method):
    @wraps(method)
    async def wrapper(self, *args, **kwargs):
        res = await method(self, *args, **kwargs)
        while True:
            if await self.get_state() == DeviceState.IDLE:
                break
            await asyncio.sleep(1)

        return res

    return wrapper


class ThermoCycler:
    def __init__(self, ip: str) -> None:
        self.ip = ip
        self.port = 8080
        self.timeout = 5

    async def run(self, steps: List[ThermoCyclerStep]):
        print("Running steps", steps)

    @classmethod
    async def get_device_ip(cls):
        # list devices using arp
        arp_entries = []
        platform_name = platform.system()
        if platform_name.lower() == "windows":
            # Windows: Internet Address, Physical Address, Type
            pattern = re.compile(
                r"(\d{1,3}(?:\.\d{1,3}){3})\s+([0-9a-fA-F\-]{17})\s+(\w+)"
            )
            for match in pattern.finditer(output):
                arp_entries.append(
                    {
                        "IP Address": match.group(1),
                        "MAC Address": match.group(2),
                        "Type": match.group(3),
                        "Name": None,  # Windows doesn't show a name in arp -a output
                    }
                )
        elif platform_name.lower() == "darwin" or platform_name.lower() == "Linux":
            # macOS & Linux: Optional Name, IP Address, MAC Address
            pattern = re.compile(
                r"(?:(\S+)\s+)?\((\d{1,3}(?:\.\d{1,3}){3})\)\s+at\s+([0-9a-fA-F\:]{17})"
            )
            for match in pattern.finditer(output):
                arp_entries.append(
                    {
                        "Name": match.group(1)
                        if match.group(1)
                        else None,  # Capture the name if present
                        "IP Address": match.group(2),
                        "MAC Address": match.group(3),
                    }
                )
        return arp_entries

    async def get_status(self):
        request_id = 1305594243
        request = f"""
    <s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">
      <s:Body>
        <GetStatus xmlns="http://sila.coop" xmlns:i="http://www.w3.org/2001/XMLSchema-instance">
          <requestId>{request_id}</requestId>
        </GetStatus>
      </s:Body>
    </s:Envelope>
    """

        resp = """
    <?xml version="1.0" encoding="UTF-8"?>
    <SOAP-ENV:Envelope
      xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/"
      xmlns:SOAP-ENC="http://schemas.xmlsoap.org/soap/encoding/"
      xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
      xmlns:xsd="http://www.w3.org/2001/XMLSchema"
      xmlns:i="http://inheco.com"
      xmlns:s="http://sila.coop"
    >
    <SOAP-ENV:Body>
      <s:GetStatusResponse>
      <s:GetStatusResult>
        <s:returnCode>1</s:returnCode>
        <s:message>Success.</s:message>
        <s:duration>PT1S</s:duration>
        <s:deviceClass>30</s:deviceClass>
      </s:GetStatusResult>
      <s:deviceId>122b63a3-8fe1-40df-a535-8803a971951a</s:deviceId>
      <s:state>inError</s:state>
      <s:subStates>
        <s:CommandDescription>
          <s:requestId>881683259</s:requestId>
          <s:commandName>Reset</s:commandName>
          <s:queuePosition>1</s:queuePosition>
          <s:startedAt>2024-09-12T10:35:20Z</s:startedAt>
          <s:currentState>processing</s:currentState>
          <s:dataWaiting xsi:nil="true"/>
        </s:CommandDescription>
      </s:subStates>
      <s:locked>false</s:locked>
      <s:PMSId>http://169.254.193.225:7071/ihc</s:PMSId>
      <s:currentTime>2024-09-12T10:35:21Z</s:currentTime>
      </s:GetStatusResponse></SOAP-ENV:Body>
    </SOAP-ENV:Envelope>
    """

        data = ET.fromstring(resp)
        # state = inError
        status = data.find(".//{http://sila.coop}state").text

    command_id = 980077706

    def send_command(self, command: str):
        ThermoCycler.command_id += 1
        req = f"""<s:Envelope
      xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">
      <s:Body>
        <{command}
          xmlns="http://sila.coop"
          xmlns:i="http://www.w3.org/2001/XMLSchema-instance">
          <requestId>{ThermoCycler.command_id}</requestId>
          <lockId i:nil="true"/>
        </{command}>
      </s:Body>
    </s:Envelope>"""
        # req = textwrap.dedent(req).replace("\n", "")
        # remove all white space before each line
        req = " ".join([line.lstrip() for line in req.split("\n")])
        # print(req)

        res = requests.post(
            f"http://{self.ip}:{self.port}/",
            data=req,
            headers={
                "Content-Type": "text/xml; charset=utf-8",
                "SOAPAction": f"http://sila.coop/{command}",
            },
            timeout=self.timeout,
        )
        # print(res.text)
        return res

    @wait_until_idle
    async def open_door(self):
        return self.send_command("OpenDoor")

    @wait_until_idle
    async def close_door(self):
        return self.send_command("CloseDoor")

    @wait_until_idle
    async def stop(self):
        return self.send_command("StopMethod")

    def get_status1(self):
        return self.send_command("GetStatus")

    async def get_state(self) -> DeviceState:
        status = self.get_status1()
        root = ET.fromstring(status.text)
        return DeviceState(root.find(".//s:state", SOAP_NAMESPACE).text)


###################
# ChatGPT PID guess
###################

# what do you think this means?
#   <PIDSet>
#     <PID number="1">
#       <PHeating>60</PHeating>
#       <PCooling>80</PCooling>
#       <IHeating>250</IHeating>
#       <ICooling>100</ICooling>
#       <DHeating>10</DHeating>
#       <DCooling>10</DCooling>
#       <PLid>100</PLid>
#       <ILid>70</ILid>
#     </PID>
#   </PIDSet>

# This XML snippet defines a PID set for an unknown device, likely controlling both heating and cooling systems, and possibly a lid temperature in a thermal control device (e.g., an ODTC or other temperature-regulating equipment). Let's break it down:

# Interpretation:
# <PIDSet>: This is the container for one or more PID controllers.

# <PID number="1">: This defines the first (and possibly only) PID controller in the set. The attributes within this element are likely used for regulating heating, cooling, and possibly the lid of a thermal control device.

# PID Parameters for Heating and Cooling:
# <PHeating>60</PHeating>: This represents the proportional gain (Kp) for heating. A value of 60 means the controller will respond relatively aggressively to errors in temperature during heating, correcting the current error based on this gain.

# <PCooling>80</PCooling>: The proportional gain (Kp) for cooling. A value of 80 means the system will respond even more aggressively when cooling, possibly because cooling is slower than heating and needs stronger feedback.

# <IHeating>250</IHeating>: The integral gain (Ki) for heating. A high value of 250 means that accumulated errors (i.e., persistent differences between the setpoint and actual temperature) during heating are corrected over time. This reduces the steady-state error but could introduce oscillations if set too high.

# <ICooling>100</ICooling>: The integral gain (Ki) for cooling. A value of 100 corrects past errors during cooling, but less aggressively than heating. This indicates a less aggressive adjustment to past errors compared to heating.

# <DHeating>10</DHeating>: The derivative gain (Kd) for heating. A value of 10 suggests that the system will use the rate of change of the error to prevent overshooting during heating. This is a moderate value to smooth out rapid temperature changes.

# <DCooling>10</DCooling>: The derivative gain (Kd) for cooling. It is the same as the heating derivative gain, which suggests the system has similar requirements for damping oscillations in both heating and cooling processes.

# PID Parameters for the Lid:
# <PLid>100</PLid>: The proportional gain for controlling the temperature of the lid. A high value of 100 means the system will react strongly to errors in the lid’s temperature.

# <ILid>70</ILid>: The integral gain for the lid temperature control. A value of 70 indicates moderate correction of past errors for the lid, allowing for smooth and consistent control over time without too much overshoot.

# Summary:
# This XML configuration defines a PID controller for:

# Heating and cooling processes with separate proportional (P), integral (I), and derivative (D) gains.
# A lid temperature control system with its own PID settings (proportional and integral, but no derivative term).
# Each of these settings fine-tunes how the device responds to temperature errors during heating, cooling, and lid control, with aggressive responses for proportional gains, and integral adjustments for accumulated errors. The specific values suggest the device requires more careful control during cooling and lid heating, likely due to thermal inertia or sensitivity to temperature fluctuations in these components.
