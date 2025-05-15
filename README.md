# dbus-mpp-p18 for Venus OS

This project allows you to connect one or more **MPP Solar Hybrid 5kW VII** inverters (Voltronic-style, P18 protocol) to **Venus OS**. It retrieves real-time **voltage**, **current**, and **power** values from the inverters and makes them available on **D-Bus** for battery system integration. It controls the **voltage** and **current* charge for the inverter, based on battery requierments.

---

## 🔗 Based On

This project builds upon the excellent work of:

- [gch1p/inverter-tools](https://github.com/gch1p/inverter-tools) – inverter communication backend
- [gch1p/inverterd-client](https://github.com/gch1p/inverterd-client) – Python client library for communicating with `inverterd`
- [DarkZeros/dbus-mppsolar](https://github.com/DarkZeros/dbus-mppsolar) – D-Bus integration for Venus OS

---

## 🧱 Prebuilt Binary

The `inverterd` binary is provided precompiled and tested on **Raspberry Pi 3** with **Venus OS**.

You can use the provided binary or compile it yourself (see below).

---

## 🐳 Build `inverterd` Using Docker

To compile `inverterd` for **ARM (armv7)** (compatible with Venus OS):

```bash
# Enable ARM emulation support
mount -t binfmt_misc binfmt_misc /proc/sys/fs/binfmt_misc
docker run --rm --privileged multiarch/qemu-user-static --reset -p yes

# Build the Docker image
docker build -t inverter-tools-arm .

# Launch the build container
docker run --rm -it inverter-tools-arm bash
```

Inside the container:

```bash
cd /opt/inverter-tools/build
# You will find the compiled inverterd binary here
```

Copy the binary to your Venus OS device at `/data/etc/dbus-mppsolar`.

---

## 📥 Clone the Repository

Clone this repository **with submodules** in the correct path:

```bash
git clone --recurse-submodules https://github.com/TheBarnHome/dbus-mpp-p18 /data/etc/dbus-mppsolar
```

---

## 🧩 Configuration – `config.json`

You must provide a `config.json` file to define which inverters are connected and how they should be exposed to D-Bus.

Example configuration:

```json
{
  "/dev/hidraw1": {
    "productname": "Master",
    "deviceinstance": 0,
    "numberOfChargers": 2,
    "updateInterval": 10000
  },
  "/dev/hidraw0": {
    "productname": "Slave_1",
    "deviceinstance": 1,
    "numberOfChargers": 2,
    "updateInterval": 10000
  }
}
```

Each key corresponds to a USB HID device (usually `/dev/hidrawX`). For each inverter, define:

- `productname`: A display name for the inverter on D-Bus
- `deviceinstance`: Unique identifier (integer) per inverter on D-Bus (0 = master)
- `numberOfChargers`: Number of internal chargers (for for charge current when multiple inverters are used)
- `updateInterval`: Polling interval in milliseconds (e.g. `10000` = 10 seconds)

Place this file in the project directory:

```
/data/etc/dbus-mppsolar/config.json
```

> ⚠️ Make sure the correct `/dev/hidrawX` device numbers are used based on your system. You can run `dmesg | grep hidraw` or `ls /dev/hidraw*` to determine them.

---

## 🚀 Installation

To install:

```bash
cd /data/etc/dbus-mppsolar
bash install.sh
```

This script will:

- Ensure your system is using the correct software feed
- Install required dependencies (`python3-pip`, `git`, etc.)
- Install `inverterd` via pip3
- Set udev rules to automatically launch on USB insert (hidraw device)
- Ensure scripts are executable
- Configure init startup logic for early detection
- Reload udev rules and initialize services

---

## ⚙️ What It Does

- Communicates with inverters using the **P18 protocol** over `/dev/hidrawX`
- Uses the high-performance `inverterd` daemon
- Retrieves data via the **Python `inverterd-client`**
- Publishes real-time metrics to **D-Bus** for consumption by **Venus OS** components (battery, system overview, etc.)
- Supports multiple inverters connected simultaneously
- Automatically starts on boot or USB device insertion

---

## 📦 Dependencies

Installed automatically:
- Python 3 and `pip3`
- [`inverterd`](https://github.com/gch1p/inverter-tools)
- [`inverterd-client`](https://github.com/gch1p/inverterd-client)
- Git
- udev (for automatic detection)

---

## ✅ Compatibility

- ✅ Venus OS on Raspberry Pi 3 (tested)
- ✅ Multiple inverters via `hidraw`
- ✅ Automatic reconnection on USB event
- ✅ Integrates with Victron D-Bus
