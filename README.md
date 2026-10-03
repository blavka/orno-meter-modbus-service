# ORNO OR-WE-517 Modbus service

Modbus RTU bridge for ORNO OR-WE-517 electricity meters. It polls documented holding registers and publishes retained MQTT state plus Home Assistant MQTT Discovery entities.

## Multi-meter configuration

All meters share one RS-485 serial transport and are polled sequentially; requests are never overlapped. Copy `config/config.example.yaml` to `config/local.yaml` and define each meter under `meters`:

```yaml
modbus:
  device: /dev/orno-meter
  baudrate: 9600
  bytesize: 8
  parity: E
  stopbits: 1
  timeout_seconds: 2
meters:
  - unit_id: 1
    topic_prefix: orno/heating
    device_identifier: orno_heating_meter
    device_name: Elektroměr topení
mqtt:
  host: 127.0.0.1
  discovery_prefix: homeassistant
runtime:
  poll_interval_seconds: 5
  clock_sync:
    enabled: false
    timezone: Europe/Prague
    interval_seconds: 86400
```

The original single-meter layout (`modbus.unit_id` plus the meter-specific MQTT fields) remains accepted for compatibility. New configurations should use `meters`.

## Clock synchronization safety

Clock synchronization is disabled by default. When `runtime.clock_sync.enabled` is explicitly `true`, the service uses `Europe/Prague` (or another IANA timezone) and synchronizes at `interval_seconds` (default `86400`). For every configured meter, one exclusive serial connection performs a clock read, function-16 write to register `0x003C`, waits one second, then reads back before the connection is released. The clock words are packed BCD: `SSMM`, `HHWD`, `DDMM`, `YY00`; `WD` is the ISO weekday in the lower byte.

Do not enable automatic writes until a commissioning read/write/read-back and physical display check have succeeded. No energy-reset operation is implemented.

## Address commissioning

Address changes are separate from the service loop. Stop the telemetry service before using this command; it refuses to write without `--confirm`, accepts only Modbus IDs `1..247`, reads the old address, writes register `0x0002` with function 06, then confirms by reading the new address.

```sh
uv run python -m orno_meter_service.service change-address \
  --config config/local.yaml \
  --current-unit-id 1 --new-unit-id 2 --confirm
```

## Transport

- stable host alias: `/dev/serial/by-orno/heating-meter`
- container device: `/dev/orno-meter`
- factory Modbus RTU: `9600 8E1`

## Published measurements

Total/per-phase voltage, current, active power, and active energy; frequency. Energy sensors use Home Assistant's `energy` device class and `total_increasing` state class, so the total energy sensor can be used in Energy Dashboard.

## Local checks

```sh
uv run pytest -q
```
