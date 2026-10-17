# Optional ESP32 room sensor

Use an ESP32 development board and a DHT22 to send a room reading every 30 seconds. This is a sensor node only; it does not switch the rig or a mains heater.

| ESP32 | DHT22 |
| --- | --- |
| 3V3 | VCC |
| GPIO4 | DATA, with a 4.7 kΩ pull-up to 3V3 |
| GND | GND |

Place it away from GPU exhaust, sunlight, and drafts. The daemon expects temperatures in Fahrenheit; the dashboard can display Fahrenheit or Celsius.

Create **`esp32/secrets.yaml` yourself**. ESPHome does not create a filled-in secret file for you. Supply these values locally:

| Secret | Your value |
| --- | --- |
| `wifi_ssid` | Wi-Fi network name |
| `wifi_password` | Wi-Fi password |
| `ota_password` | A unique firmware-update password |
| `furnace_temperature_url` | `http://YOUR_GPU_HOST:8787/temperature`, or your HTTPS proxy URL |
| `furnace_authorization` | `Bearer ` followed by your private heatd token |

The file is ignored by Git. Keep sensor firmware and build artifacts private too: compiled firmware includes these credentials.

Install ESPHome in its own environment, then validate and flash:

```bash
cd esp32
esphome config esp32-furnace.yaml
esphome run esp32-furnace.yaml
```

Configure heatd to listen on an address the sensor can reach. The default loopback listener is for local clients only. HTTP is suitable only for a trusted private network; use HTTPS for other deployments. The node ignores non-finite sensor values. If readings stop for 120 seconds, automatic heating pauses.

The configuration uses the documented [DHT sensor](https://esphome.io/components/sensor/dht/), [HTTP request](https://esphome.io/components/http_request/), and [ESPHome OTA](https://esphome.io/components/ota/esphome/) components. It has been checked against their documented structure, but not compiled or flashed on physical hardware in this cleanup. Run the validation command with your installed ESPHome version before flashing.
