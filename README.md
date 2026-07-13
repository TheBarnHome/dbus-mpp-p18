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

## 🧩 Automatic discovery and configuration

No `config.json` is required. `mppsolar-manager.py` scans `/dev/hidraw*` every two
seconds, accepts only protocol 18 devices with a valid unique serial number, and
automatically adds/removes them. The P18 serial is the permanent identity, so a
USB reorder from `hidraw0` to `hidraw1` does not change the name, Device Instance,
or history.

Configure each device from Venus OS **Device information**:

- **Name** (`/CustomName`);
- **MPP Solar VRM instance** (`/Settings/DeviceInstance`, 1–255);
- **MPP Solar polling interval** (`/Settings/PollInterval`, 5–60 seconds).

Settings are stored by `com.victronenergy.settings` under
`/Settings/Devices/mppsolar_<serial>/`. A conflicting Device Instance is rejected;
an accepted instance change restarts that inverter's two services. The number of
parallel chargers is detected from P18 `get-p-rated` IDs 0–6 and falls back to the
number of active P18 devices.

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
- Start a single hotplug manager on boot and USB changes
- Ensure scripts are executable
- Configure init startup logic for early detection
- Add the two MPP Solar fields to Classic UI's `PageDeviceInfo.qml`, with an
  idempotent backup/rollback patch reapplied from `/data/rc.local`
- Install the prepared global gui-v2 plugin only when the official `/data/apps`
  plugin framework is available (Venus OS is never upgraded by this project)
- Reload udev rules and initialize services

The stable services use the serial number in their names:

```
com.victronenergy.inverter.mppsolar-inverter.sn_<serial>
com.victronenergy.solarcharger.mppsolar-charger.sn_<serial>
```

The current HID path is diagnostic only. The manager publishes its device list as
`com.victronenergy.mppsolar.manager`.

### Migration from `config.json`

On the first update, `migrate-config.py` queries the already-running backends,
builds `/data/etc/dbus-mppsolar-state/devices.json`, and captures the live D-Bus
history before stopping anything. Only after every entry has a unique serial and
Device Instance is the old file retained as `config.json.legacy`.

To validate a legacy installation manually without changing it:

```bash
python3 migrate-config.py --dry-run
```

---

## 🔄 Safe updates

Run updates directly on the Venus OS device:

```bash
cd /data/etc/dbus-mppsolar
bash update.sh --dry-run
bash update.sh
```

The updater:

- migrates a legacy `config.json` before the first service stop;
- refuses to overwrite other local changes;
- only accepts a fast-forward Git update;
- creates separate timestamped code and state backups in
  `/data/etc/dbus-mppsolar-backups`;
- validates Python, JSON, and shell syntax before restarting;
- reapplies the Classic UI extension and conditionally installs gui-v2;
- restarts only the manager and its child processes;
- verifies the manager plus every serial-indexed inverter/charger service;
- restores both the previous Git revision and persistent state on failure.

Use `bash update.sh --no-restart` to install an update without restarting the
running services. The new code will then be used on their next restart.

Persistent settings and history are outside the Git checkout in
`/data/etc/dbus-mppsolar-state`. History is written atomically every five minutes
and at controlled shutdown. A corrupt state file is renamed with a `.corrupt-*`
suffix and safe defaults are used.

---

## ⚙️ What It Does

- Communicates with inverters using the **P18 protocol** over `/dev/hidrawX`
- Uses the high-performance `inverterd` daemon
- Retrieves data via the **Python `inverterd-client`**
- Publishes real-time metrics to **D-Bus** for consumption by **Venus OS** components (battery, system overview, etc.)
- Supports multiple inverters connected simultaneously
- Automatically adds, removes, reconnects, and survives `hidraw` permutations

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
