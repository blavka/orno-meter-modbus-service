"""ORNO OR-WE-517 Modbus RTU bridge with MQTT Discovery and safe commissioning."""

from __future__ import annotations

import argparse
import json
import struct
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import paho.mqtt.client as mqtt
import yaml
from pymodbus.client import ModbusSerialClient

CLOCK_REGISTER = 0x003C
METER_ADDRESS_REGISTER = 0x0002
DEFAULT_CLOCK_SYNC = {
    "enabled": False,
    "timezone": "Europe/Prague",
    "interval_seconds": 86400,
}


class Measurement:
    def __init__(self, key: str, address: int, unit: str, device_class: str, state_class: str) -> None:
        self.key = key
        self.address = address
        self.unit = unit
        self.device_class = device_class
        self.state_class = state_class


MEASUREMENTS = (
    Measurement("voltage_l1", 0x000E, "V", "voltage", "measurement"),
    Measurement("voltage_l2", 0x0010, "V", "voltage", "measurement"),
    Measurement("voltage_l3", 0x0012, "V", "voltage", "measurement"),
    Measurement("frequency", 0x0014, "Hz", "frequency", "measurement"),
    Measurement("current_l1", 0x0016, "A", "current", "measurement"),
    Measurement("current_l2", 0x0018, "A", "current", "measurement"),
    Measurement("current_l3", 0x001A, "A", "current", "measurement"),
    Measurement("active_power_total", 0x001C, "kW", "power", "measurement"),
    Measurement("active_power_l1", 0x001E, "kW", "power", "measurement"),
    Measurement("active_power_l2", 0x0020, "kW", "power", "measurement"),
    Measurement("active_power_l3", 0x0022, "kW", "power", "measurement"),
    Measurement("active_energy_total", 0x0100, "kWh", "energy", "total_increasing"),
    Measurement("active_energy_l1", 0x0102, "kWh", "energy", "total_increasing"),
    Measurement("active_energy_l2", 0x0104, "kWh", "energy", "total_increasing"),
    Measurement("active_energy_l3", 0x0106, "kWh", "energy", "total_increasing"),
)


def decode_float32(registers: list[int]) -> float:
    """Decode the ORNO big-endian IEEE-754 two-register representation."""
    if len(registers) != 2:
        raise ValueError("FLOAT32 requires exactly two Modbus registers")
    return struct.unpack(">f", struct.pack(">HH", *registers))[0]


def bcd_byte(value: int) -> int:
    """Encode an integer from 0 through 99 as one packed BCD byte."""
    if not 0 <= value <= 99:
        raise ValueError("BCD byte value must be in 0..99")
    return (value // 10 << 4) | (value % 10)


def orno_clock_words(instant: datetime) -> list[int]:
    """Encode a local datetime as ORNO clock registers: SSMM, HHWD, DDMM, YY00."""
    if instant.tzinfo is None:
        raise ValueError("clock time must be timezone-aware")
    return [
        (bcd_byte(instant.second) << 8) | bcd_byte(instant.minute),
        (bcd_byte(instant.hour) << 8) | bcd_byte(instant.isoweekday()),
        (bcd_byte(instant.day) << 8) | bcd_byte(instant.month),
        bcd_byte(instant.year % 100) << 8,
    ]


def discovery_payloads(*, discovery_prefix: str, topic_prefix: str, identifier: str, device_name: str):
    """Return retained MQTT Discovery configuration for each measurement."""
    device = {
        "identifiers": [identifier],
        "name": device_name,
        "manufacturer": "ORNO",
        "model": "OR-WE-517",
    }
    for item in MEASUREMENTS:
        payload = {
            "name": item.key.replace("_", " ").title(),
            "unique_id": f"{identifier}_{item.key}",
            "state_topic": f"{topic_prefix}/{item.key}/state",
            "availability_topic": f"{topic_prefix}/availability",
            "unit_of_measurement": item.unit,
            "device_class": item.device_class,
            "state_class": item.state_class,
            "device": device,
        }
        yield f"{discovery_prefix}/sensor/{identifier}_{item.key}/config", payload


def format_state(key: str, value: float) -> str:
    """Format measurements at their meaningful display precision."""
    if key.startswith("active_energy"):
        return f"{value:.2f}"
    if key.startswith("active_power"):
        return f"{value:.3f}"
    if key.startswith("current"):
        return f"{value:.2f}"
    if key.startswith("voltage"):
        return f"{value:.1f}"
    if key == "frequency":
        return f"{value:.2f}"
    return str(value)


def read_measurements(client: ModbusSerialClient, unit_id: int) -> dict[str, float]:
    """Read documented holding registers only for one addressed meter."""
    first = client.read_holding_registers(address=0x000E, count=24, device_id=unit_id)
    second = client.read_holding_registers(address=0x0100, count=8, device_id=unit_id)
    if first.isError() or second.isError():
        raise RuntimeError("Modbus holding-register read failed")
    blocks = ((0x000E, first.registers), (0x0100, second.registers))
    result: dict[str, float] = {}
    for item in MEASUREMENTS:
        for start, registers in blocks:
            offset = item.address - start
            if 0 <= offset <= len(registers) - 2:
                result[item.key] = decode_float32(registers[offset : offset + 2])
                break
        else:
            raise RuntimeError(f"measurement address not present in a read block: {item.address:#x}")
    return result


def _read_clock(client: ModbusSerialClient, unit_id: int) -> list[int]:
    response = client.read_holding_registers(address=CLOCK_REGISTER, count=4, device_id=unit_id)
    if response.isError() or len(response.registers) != 4:
        raise RuntimeError("Modbus clock read failed")
    return response.registers


def synchronize_clock(client: ModbusSerialClient, unit_id: int, instant: datetime) -> tuple[list[int], list[int]]:
    """Read, write, and read back an ORNO clock without releasing the serial client."""
    before = _read_clock(client, unit_id)
    words = orno_clock_words(instant)
    response = client.write_registers(address=CLOCK_REGISTER, values=words, device_id=unit_id)
    if response.isError():
        raise RuntimeError("Modbus clock write failed")
    time.sleep(1)
    after = _read_clock(client, unit_id)
    # Seconds advance during the settle delay; minute, hour, weekday, date and year must match.
    if after[1:] != words[1:] or (after[0] & 0x00FF) != (words[0] & 0x00FF):
        raise RuntimeError("Modbus clock read-back mismatch")
    return before, after


def validate_address_change(current_unit_id: int, new_unit_id: int, *, confirm: bool) -> None:
    """Validate the explicitly-confirmed, documented Modbus address change."""
    if not confirm:
        raise ValueError("change-address requires --confirm")
    for label, value in (("current unit ID", current_unit_id), ("new unit ID", new_unit_id)):
        if not 1 <= value <= 247:
            raise ValueError(f"{label} must be in 1..247")


def change_address(client: ModbusSerialClient, current_unit_id: int, new_unit_id: int, *, confirm: bool) -> None:
    """Commission one address change: read old address, write function 06, read new address."""
    validate_address_change(current_unit_id, new_unit_id, confirm=confirm)
    old_value = client.read_holding_registers(address=METER_ADDRESS_REGISTER, count=1, device_id=current_unit_id)
    if old_value.isError() or old_value.registers != [current_unit_id]:
        raise RuntimeError("current meter address could not be verified")
    response = client.write_register(address=METER_ADDRESS_REGISTER, value=new_unit_id, device_id=current_unit_id)
    if response.isError():
        raise RuntimeError("Modbus meter address write failed")
    time.sleep(1)
    new_value = client.read_holding_registers(address=METER_ADDRESS_REGISTER, count=1, device_id=new_unit_id)
    if new_value.isError() or new_value.registers != [new_unit_id]:
        raise RuntimeError("new meter address could not be verified")


def _normalize_meter(meter: dict[str, Any]) -> dict[str, Any]:
    required = ("unit_id", "topic_prefix", "device_identifier", "device_name")
    missing = [key for key in required if key not in meter]
    if missing:
        raise ValueError(f"meter missing configuration: {', '.join(missing)}")
    unit_id = meter["unit_id"]
    if not isinstance(unit_id, int) or not 1 <= unit_id <= 247:
        raise ValueError("meter unit_id must be in 1..247")
    return {key: meter[key] for key in required}


def load_config(path: str) -> dict[str, Any]:
    """Load multi-meter configuration, accepting the original single-meter layout."""
    data = yaml.safe_load(Path(path).read_text())
    if not isinstance(data, dict):
        raise ValueError("configuration must be a YAML mapping")
    for section in ("modbus", "mqtt", "runtime"):
        if not isinstance(data.get(section), dict):
            raise ValueError(f"missing {section} configuration")
    meters = data.get("meters")
    if meters is None:
        meters = [{
            "unit_id": data["modbus"].get("unit_id", 1),
            "topic_prefix": data["mqtt"].get("topic_prefix"),
            "device_identifier": data["mqtt"].get("device_identifier"),
            "device_name": data["mqtt"].get("device_name"),
        }]
    if not isinstance(meters, list) or not meters:
        raise ValueError("meters must be a non-empty YAML list")
    data["meters"] = [_normalize_meter(meter) for meter in meters if isinstance(meter, dict)]
    if len(data["meters"]) != len(meters):
        raise ValueError("each meter must be a YAML mapping")
    for key in ("unit_id", "topic_prefix", "device_identifier"):
        values = [meter[key] for meter in data["meters"]]
        if len(set(values)) != len(values):
            raise ValueError(f"meter {key} values must be unique")
    clock_sync = data["runtime"].get("clock_sync", {})
    if not isinstance(clock_sync, dict):
        raise ValueError("runtime.clock_sync must be a YAML mapping")
    normalized_sync = {**DEFAULT_CLOCK_SYNC, **clock_sync}
    if not isinstance(normalized_sync["enabled"], bool):
        raise ValueError("runtime.clock_sync.enabled must be boolean")
    if not isinstance(normalized_sync["interval_seconds"], (int, float)) or normalized_sync["interval_seconds"] <= 0:
        raise ValueError("runtime.clock_sync.interval_seconds must be positive")
    try:
        ZoneInfo(normalized_sync["timezone"])
    except Exception as exc:
        raise ValueError("runtime.clock_sync.timezone must be an IANA timezone") from exc
    data["runtime"]["clock_sync"] = normalized_sync
    return data


def _serial_client(modbus: dict[str, Any]) -> ModbusSerialClient:
    return ModbusSerialClient(
        port=modbus["device"], baudrate=modbus.get("baudrate", 9600),
        bytesize=modbus.get("bytesize", 8), parity=modbus.get("parity", "E"),
        stopbits=modbus.get("stopbits", 1), timeout=modbus.get("timeout_seconds", 2),
    )


def publish_cycle(config: dict[str, Any], *, synchronize_clocks: bool = False, now: datetime | None = None) -> None:
    """Poll every meter sequentially on one serial client, then publish all results."""
    modbus = config["modbus"]
    mqtt_config = config["mqtt"]
    client = _serial_client(modbus)
    if not client.connect():
        raise RuntimeError("could not open Modbus serial adapter")
    readings: list[tuple[dict[str, Any], dict[str, float]]] = []
    try:
        for meter in config["meters"]:
            readings.append((meter, read_measurements(client, meter["unit_id"])))
            if synchronize_clocks:
                sync_config = config["runtime"]["clock_sync"]
                instant = now or datetime.now(ZoneInfo(sync_config["timezone"]))
                synchronize_clock(client, meter["unit_id"], instant)
    finally:
        client.close()

    publisher = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    publisher.will_set("orno-meter-service/availability", "offline", retain=True)
    publisher.connect(mqtt_config["host"], int(mqtt_config.get("port", 1883)), 30)
    publisher.loop_start()
    try:
        for meter, values in readings:
            for topic, payload in discovery_payloads(
                discovery_prefix=mqtt_config.get("discovery_prefix", "homeassistant"),
                topic_prefix=meter["topic_prefix"], identifier=meter["device_identifier"],
                device_name=meter["device_name"],
            ):
                publisher.publish(topic, json.dumps(payload, separators=(",", ":")), retain=True).wait_for_publish()
            publisher.publish(f"{meter['topic_prefix']}/availability", "online", retain=True).wait_for_publish()
            for key, value in values.items():
                publisher.publish(f"{meter['topic_prefix']}/{key}/state", format_state(key, value), retain=True).wait_for_publish()
    finally:
        publisher.loop_stop()
        publisher.disconnect()


def commission_change_address(config: dict[str, Any], current_unit_id: int, new_unit_id: int, *, confirm: bool) -> None:
    """Run the standalone commissioning action; never invoked by the service loop."""
    validate_address_change(current_unit_id, new_unit_id, confirm=confirm)
    client = _serial_client(config["modbus"])
    if not client.connect():
        raise RuntimeError("could not open Modbus serial adapter")
    try:
        change_address(client, current_unit_id, new_unit_id, confirm=True)
    finally:
        client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", choices=("run", "change-address"), default="run")
    parser.add_argument("--config", required=True)
    parser.add_argument("--once", action="store_true", help="run one polling cycle")
    parser.add_argument("--current-unit-id", type=int, help="current address for change-address")
    parser.add_argument("--new-unit-id", type=int, help="new address for change-address")
    parser.add_argument("--confirm", action="store_true", help="explicitly authorize change-address")
    args = parser.parse_args()
    config = load_config(args.config)
    if args.command == "change-address":
        if args.current_unit_id is None or args.new_unit_id is None:
            parser.error("change-address requires --current-unit-id and --new-unit-id")
        commission_change_address(config, args.current_unit_id, args.new_unit_id, confirm=args.confirm)
        return

    interval = float(config["runtime"].get("poll_interval_seconds", 5))
    clock_sync = config["runtime"]["clock_sync"]
    last_sync: float | None = None
    while True:
        now = time.monotonic()
        sync_due = bool(clock_sync["enabled"]) and (last_sync is None or now - last_sync >= clock_sync["interval_seconds"])
        try:
            publish_cycle(config, synchronize_clocks=sync_due)
            if sync_due:
                last_sync = now
        except Exception as exc:
            print(f"poll failed: {exc}", flush=True)
        if args.once:
            return
        time.sleep(interval)


if __name__ == "__main__":
    main()
