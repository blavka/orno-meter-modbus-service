import struct
from datetime import datetime
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest

from orno_meter_service import service
from orno_meter_service.service import decode_float32, discovery_payloads, format_state


def test_decodes_big_endian_float32_registers():
    registers = list(struct.unpack(">HH", struct.pack(">f", 1.25)))

    assert decode_float32(registers) == 1.25


def test_energy_discovery_has_total_increasing_semantics():
    topic, payload = next(
        item for item in discovery_payloads(
            discovery_prefix="homeassistant",
            topic_prefix="orno/heating",
            identifier="orno_heating_meter",
            device_name="Elektroměr topení",
        )
        if item[1]["unique_id"] == "orno_heating_meter_active_energy_total"
    )

    assert topic == "homeassistant/sensor/orno_heating_meter_active_energy_total/config"
    assert payload["device_class"] == "energy"
    assert payload["state_class"] == "total_increasing"
    assert payload["unit_of_measurement"] == "kWh"
    assert payload["state_topic"] == "orno/heating/active_energy_total/state"


def test_formats_energy_without_binary_float_artifacts():
    assert format_state("active_energy_total", 34282.08984375) == "34282.09"
    assert format_state("active_power_total", 0.019999999552965164) == "0.020"


def test_encodes_orno_clock_words_with_packed_bcd_and_iso_weekday():
    instant = datetime(2026, 10, 3, 10, 27, 33, tzinfo=ZoneInfo("Europe/Prague"))

    assert service.orno_clock_words(instant) == [0x3327, 0x1006, 0x0310, 0x2600]


def test_load_config_accepts_multiple_meters_and_opt_in_clock_settings(tmp_path):
    config_path = tmp_path / "meters.yaml"
    config_path.write_text(
        """
modbus:
  device: /dev/orno-meter
meters:
  - unit_id: 1
    topic_prefix: orno/heating
    device_identifier: heating_meter
    device_name: Heating meter
  - unit_id: 2
    topic_prefix: orno/workshop
    device_identifier: workshop_meter
    device_name: Workshop meter
mqtt:
  host: localhost
runtime:
  clock_sync:
    enabled: true
    timezone: Europe/Prague
    interval_seconds: 86400
"""
    )

    config = service.load_config(str(config_path))

    assert [meter["unit_id"] for meter in config["meters"]] == [1, 2]
    assert config["runtime"]["clock_sync"] == {
        "enabled": True,
        "timezone": "Europe/Prague",
        "interval_seconds": 86400,
    }


def test_load_config_rejects_duplicate_meter_bus_addresses(tmp_path):
    config_path = tmp_path / "duplicate-addresses.yaml"
    config_path.write_text(
        """
modbus: {device: /dev/orno-meter}
mqtt: {host: localhost}
runtime: {}
meters:
  - {unit_id: 1, topic_prefix: orno/one, device_identifier: one, device_name: One}
  - {unit_id: 1, topic_prefix: orno/two, device_identifier: two, device_name: Two}
"""
    )

    with pytest.raises(ValueError, match="unique"):
        service.load_config(str(config_path))


def test_load_config_normalizes_legacy_single_meter_settings(tmp_path):
    config_path = tmp_path / "legacy.yaml"
    config_path.write_text(
        """
modbus:
  device: /dev/orno-meter
  unit_id: 1
mqtt:
  host: localhost
  topic_prefix: orno/heating
  device_identifier: heating_meter
  device_name: Heating meter
runtime: {}
"""
    )

    config = service.load_config(str(config_path))

    assert config["meters"] == [{
        "unit_id": 1,
        "topic_prefix": "orno/heating",
        "device_identifier": "heating_meter",
        "device_name": "Heating meter",
    }]
    assert config["runtime"]["clock_sync"] == {
        "enabled": False,
        "timezone": "Europe/Prague",
        "interval_seconds": 86400,
    }


def test_publish_cycle_reads_each_meter_sequentially_on_one_serial_client(monkeypatch):
    config = {
        "modbus": {"device": "/dev/null"},
        "mqtt": {"host": "localhost"},
        "runtime": {"clock_sync": {"enabled": False, "timezone": "Europe/Prague", "interval_seconds": 86400}},
        "meters": [
            {"unit_id": 1, "topic_prefix": "orno/one", "device_identifier": "one", "device_name": "One"},
            {"unit_id": 2, "topic_prefix": "orno/two", "device_identifier": "two", "device_name": "Two"},
        ],
    }
    serial = Mock(connect=Mock(return_value=True))
    monkeypatch.setattr(service, "ModbusSerialClient", Mock(return_value=serial))
    read = Mock(side_effect=[{"voltage_l1": 230.0}, {"voltage_l1": 231.0}])
    monkeypatch.setattr(service, "read_measurements", read)
    publisher = Mock()
    publisher.publish.return_value.wait_for_publish.return_value = None
    monkeypatch.setattr(service.mqtt, "Client", Mock(return_value=publisher))

    service.publish_cycle(config)

    assert read.call_args_list == [((serial, 1),), ((serial, 2),)]
    assert serial.close.call_count == 1
    state_topics = [call.args[0] for call in publisher.publish.call_args_list if call.args[0].endswith("/state")]
    assert state_topics == ["orno/one/voltage_l1/state", "orno/two/voltage_l1/state"]


def test_synchronize_clock_reads_writes_and_reads_back_on_same_client(monkeypatch):
    client = Mock()
    client.read_holding_registers.side_effect = [
        Mock(isError=Mock(return_value=False), registers=[0, 0, 0, 0]),
        Mock(isError=Mock(return_value=False), registers=[0x3427, 0x1006, 0x0310, 0x2600]),
    ]
    client.write_registers.return_value = Mock(isError=Mock(return_value=False))
    monkeypatch.setattr(service.time, "sleep", Mock())
    instant = datetime(2026, 10, 3, 10, 27, 33, tzinfo=ZoneInfo("Europe/Prague"))

    service.synchronize_clock(client, 2, instant)

    assert client.read_holding_registers.call_args_list == [
        ((), {"address": 0x003C, "count": 4, "device_id": 2}),
        ((), {"address": 0x003C, "count": 4, "device_id": 2}),
    ]
    assert client.write_registers.call_args == ((), {"address": 0x003C, "values": [0x3327, 0x1006, 0x0310, 0x2600], "device_id": 2})


def test_synchronize_clock_rejects_a_readback_that_does_not_match_written_date_and_time(monkeypatch):
    client = Mock()
    client.read_holding_registers.side_effect = [
        Mock(isError=Mock(return_value=False), registers=[0, 0, 0, 0]),
        Mock(isError=Mock(return_value=False), registers=[0x0000, 0x0000, 0x0000, 0x0000]),
    ]
    client.write_registers.return_value = Mock(isError=Mock(return_value=False))
    monkeypatch.setattr(service.time, "sleep", Mock())

    with pytest.raises(RuntimeError, match="read-back"):
        service.synchronize_clock(
            client, 1, datetime(2026, 10, 3, 10, 27, 33, tzinfo=ZoneInfo("Europe/Prague"))
        )


def test_change_address_rejects_unconfirmed_or_out_of_range_values():
    with pytest.raises(ValueError, match="--confirm"):
        service.validate_address_change(1, 2, confirm=False)
    with pytest.raises(ValueError, match="1..247"):
        service.validate_address_change(0, 2, confirm=True)
    with pytest.raises(ValueError, match="1..247"):
        service.validate_address_change(1, 248, confirm=True)


def test_change_address_writes_register_two_and_confirms_new_address(monkeypatch):
    client = Mock()
    client.read_holding_registers.side_effect = [
        Mock(isError=Mock(return_value=False), registers=[7]),
        Mock(isError=Mock(return_value=False), registers=[9]),
    ]
    client.write_register.return_value = Mock(isError=Mock(return_value=False))
    monkeypatch.setattr(service.time, "sleep", Mock())

    service.change_address(client, 7, 9, confirm=True)

    assert client.read_holding_registers.call_args_list == [
        ((), {"address": 0x0002, "count": 1, "device_id": 7}),
        ((), {"address": 0x0002, "count": 1, "device_id": 9}),
    ]
    assert client.write_register.call_args == ((), {"address": 0x0002, "value": 9, "device_id": 7})
