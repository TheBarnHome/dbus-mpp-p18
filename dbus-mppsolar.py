#!/usr/bin/env python3

"""
Handle automatic connection with MPP Solar inverter compatible device (VEVOR)
This will output 2 dbus services, one for Inverter data another one for control
via VRM of the features.
"""
VERSION = 'v0.3'

from gi.repository import GLib
import platform
import argparse
import logging
import sys
import os
import json
from enum import Enum
import datetime
import dbus
import dbus.service
import time
import atexit
import signal
from inverterd import Client, Format

logging.basicConfig(level=logging.WARNING, format='%(asctime)s - %(levelname)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

# our own packages
sys.path.insert(1, os.path.join(os.path.dirname(__file__), 'velib_python'))
from vedbus import VeDbusService, VeDbusItemExport, VeDbusItemImport
from settingsdevice import SettingsDevice
from mppsolar_common import (
    HISTORY_PATHS,
    HistoryStore,
    POLL_DEFAULT,
    allocate_instance,
    clamp_poll_interval,
    count_parallel_members,
    load_manifest,
    service_suffix,
    settings_prefix,
    validate_serial,
)

port = None
host = '127.0.0.1'
usb_path = ''
output_format=Format.JSON

# For production history
energyProductionDays = None
currentDay = None
minBatteryVoltage = None
maxBatteryVoltage = None
maxBatteryCurrent = None
maxPVPower = None
maxPVVoltage = None

numberOfChargers = 1

# Inverter commands to read from the serial
def safe_runInverterCommands(command: str, params: tuple = (), timeout_sec: int = 10):
    """
    Exécute une commande sur l'onduleur via la librairie inverterd.

    :param command: La commande à exécuter (ex: 'get-status', 'get-year-generated')
    :param params: Tuple de paramètres à passer à la commande (ex: (2021,), (2021, 2022))
    :return: Le résultat de la commande
    """
    global args
    global mainloop
    global port
    global host
    global output_format

    c = Client(port, host)
    c.sock.settimeout(timeout_sec)
    c.connect()
    c.format(output_format)
    
    # Exécution avec ou sans paramètres selon que le tuple est vide ou non
    if params:
        output = c.exec(command, params)
    else:
        output = c.exec(command)

    parsed = json.loads(output)

    return parsed

def runInverterCommands(command: str, params: tuple = (), timeout_sec: int = 10):
    """
    Exécute une commande avec un timeout fini. Le manager redémarre la paire
    driver/inverterd si le backend ne répond plus.
    """
    try:
        return safe_runInverterCommands(command, params, timeout_sec)
    except TimeoutError:
        logging.error("inverterd timed out while running %s", command)
        raise

def find_battery_service():
    bus = dbus.SystemBus()
    om = bus.get_object('org.freedesktop.DBus', '/org/freedesktop/DBus')
    iface = dbus.Interface(om, 'org.freedesktop.DBus')
    names = iface.ListNames()

    for name in names:
        if name.startswith('com.victronenergy.battery.'):
            return name
    return None

def setOutputSource(source):
    #POP<NN>: Setting device output source priority
    #    NN = 00 for utility first, 01 for solar first, 02 for SBU priority
    #   For PI18, Output POP0 [0: Solar-Utility-Batter],  POP1 [1: Solar-Battery-Utility]
    return runInverterCommands('set-output-source-priority', (source,))

def setChargerPriority(priority):
    #PCP<NN>: Setting device charger priority
    #  For KS: 00 for utility first, 01 for solar first, 02 for solar and utility, 03 for only solar charging
    #  For MKS: 00 for utility first, 01 for solar first, 03 for only solar charging
    #   For PI18, 0: Solar first, 1: Solar and Utility, 2: Only solar
    return runInverterCommands('set-charge-source-priority', (priority,))

def setMaxChargingVoltage(bulk, float):
    #MCHGV : Setting bulk and float voltage
    # For PI18 : MCHGV552,540 will set Bulk - CV voltage [480~584] in 0.1V xxx, Float voltage [480~584] in 0.1V
   try:
    return runInverterCommands('set-max-charge-voltage', (round(bulk, 1), round(float, 1)))
   except:
    logging.warning("Fail to set max charging voltage to {} and {}".format(bulk, float), exc_info=True)
    return True

def setMaxChargingCurrent(id, current):
    #MNCHGC<mnnn><cr>: Setting max charging current (More than 100A)
    #  Setting value can be gain by QMCHGCR command.
    #  nnn is max charging current, m is parallel number.
    try:
        roundedCurrent = min(max (0, round(current / 10 / numberOfChargers) * 10), 80)
        return runInverterCommands('set-max-charge-current', (id, roundedCurrent,))
    except:
        logging.warning("Fail to set max charging current to {:d}".format(current))
        return True
    
def setMaxUtilityChargingCurrent(id, current):
    #MUCHGC<nnn><cr>: Setting utility max charging current
    #  Setting value can be gain by QMCHGCR command.
    #  nnn is max charging current, m is parallel number.
    roundedCurrent = min(max(2, round(current / 10 / numberOfChargers) * 10), 80)
    try:
       return runInverterCommands('set-max-ac-charge-current', (id, roundedCurrent,))
    except:
        logging.warning("Fail to set max charging current to {:d}".format(current))
        return True

def isNaN(num):
    return num != num


# Allow to have multiple DBUS connections
class SystemBus(dbus.bus.BusConnection):
    def __new__(cls):
        return dbus.bus.BusConnection.__new__(cls, dbus.bus.BusConnection.TYPE_SYSTEM) 
class SessionBus(dbus.bus.BusConnection):
    def __new__(cls):
        return dbus.bus.BusConnection.__new__(cls, dbus.bus.BusConnection.TYPE_SESSION)
def dbusconnection():
    return SessionBus() if 'DBUS_SESSION_BUS_ADDRESS' in os.environ else SystemBus()

class DbusMppSolarService(object):
    def __init__(self, tty, serial_number, backend_port, connection='MPP Solar P18'):
        global numberOfChargers
        global port
        global usb_path

        self.tty = tty
        self.serial_number = validate_serial(serial_number)
        self._initializing = True
        self.settings_base = settings_prefix(self.serial_number)
        self._restart_requested = False
        self.exit_code = 0
        self._consecutive_errors = 0
        self._poll_source = None
        self._queued_updates = []
        self._last_p18_alerts = None
        self.history_store = HistoryStore(self.serial_number)
        self.history = self.history_store.load()

        port = int(backend_port)
        usb_path = tty
        if not os.path.exists(usb_path):
            raise RuntimeError(f"Inverter not connected on {tty}")

        manifest = load_manifest()
        saved = manifest.get(self.serial_number, {})
        preferred_instance = int(saved.get('device_instance', 0) or 0)
        used_instances = self._used_device_instances()
        deviceinstance = allocate_instance(used_instances, preferred_instance)
        default_name = str(saved.get('custom_name') or f"MPP Solar {self.serial_number[-6:]}")[:32]
        default_poll = clamp_poll_interval(saved.get('poll_interval', POLL_DEFAULT))
        bus = dbusconnection()
        self._settings = SettingsDevice(
            bus=bus,
            supportedSettings={
                'custom_name': [f'{self.settings_base}/CustomName', default_name, 0, 0],
                'device_instance': [f'{self.settings_base}/DeviceInstance', deviceinstance, 1, 255],
                'poll_interval': [f'{self.settings_base}/PollInterval', default_poll, 5, 60],
            },
            eventCallback=self._setting_changed,
        )
        deviceinstance = int(self._settings['device_instance'])
        if deviceinstance in used_instances:
            deviceinstance = allocate_instance(used_instances)
            self._settings['device_instance'] = deviceinstance
        self.custom_name = str(self._settings['custom_name'])[:32]
        self.poll_interval = clamp_poll_interval(self._settings['poll_interval'])

        logging.info(
            "Connected serial %s on %s, port %s, DeviceInstance %s",
            self.serial_number, tty, port, deviceinstance,
        )
        
        # Create the services
        suffix = service_suffix(self.serial_number)
        self._dbusinverter = VeDbusService(f'com.victronenergy.inverter.mppsolar-inverter.{suffix}', bus=bus, register=False)
        self._dbusmppt = VeDbusService(f'com.victronenergy.solarcharger.mppsolar-charger.{suffix}', bus=bus, register=False)

        # Set up default paths
        self.setupInverterDefaultPaths(self._dbusinverter, connection, deviceinstance, "MPP Solar Inverter")
        self.setupChargerDefaultPaths(self._dbusmppt, connection, deviceinstance, "MPP Solar Charger")

        # Create paths for inverter
        self._dbusinverter.add_path('/Dc/0/Voltage', 0)
        self._dbusinverter.add_path('/Ac/Out/L1/V', 0)
        self._dbusinverter.add_path('/Ac/Out/L1/I', 0)
        self._dbusinverter.add_path('/Ac/Out/L1/P', 0)
        self._dbusinverter.add_path('/Ac/Out/L1/F', 0)
        # This service represents the inverter side of an off-grid all-in-one
        # device. Victron inverter mode only accepts documented switch values;
        # operating activity is reported separately through /State.
        self._dbusinverter.add_path('/Mode', 2)                     #<- 2=Inverter only
        self._dbusinverter.add_path('/State', 0)                    #<- 0=Off; 1=Low Power; 2=Fault; 9=Inverting
        self._dbusinverter.add_path('/Temperature', 123)

        # Standard Victron inverter alarms: 0=OK, 1=Warning, 2=Alarm.
        self._dbusinverter.add_path('/Alarms/LowVoltage', 0)
        self._dbusinverter.add_path('/Alarms/HighVoltage', 0)
        self._dbusinverter.add_path('/Alarms/HighTemperature', 0)
        self._dbusinverter.add_path('/Alarms/Overload', 0)

        # P18 values without a reliable equivalent in the Victron inverter API.
        # These paths remain useful through D-Bus/MQTT without creating false
        # alarms in Venus OS (notably LineFail on an off-grid installation).
        self._dbusinverter.add_path('/Diagnostics/P18/FaultCode', 0)
        self._dbusinverter.add_path('/Diagnostics/P18/LineFail', 0)
        self._dbusinverter.add_path('/Diagnostics/P18/OutputCircuitShort', 0)
        self._dbusinverter.add_path('/Diagnostics/P18/FanLock', 0)
        self._dbusinverter.add_path('/Diagnostics/P18/EepromFail', 0)
        self._dbusinverter.add_path('/Diagnostics/P18/PowerLimit', 0)

        logging.info(f"Paths for Inverter created.")

        # Create paths for charger
        # general data
        self._dbusmppt.add_path('/NrOfTrackers', 1)
        self._dbusmppt.add_path('/Pv/V', 0)
        self._dbusmppt.add_path('/Pv/0/V', 0)
        self._dbusmppt.add_path('/Pv/0/P', 0)
        self._dbusmppt.add_path('/Yield/Power', 0)
        self._dbusmppt.add_path('/DC/0/Temperature', 123)
        self._dbusmppt.add_path('/Dc/0/Voltage', 0)
        self._dbusmppt.add_path('/Dc/0/Current', 0)

        # external control
        self._dbusmppt.add_path('/Link/NetworkMode', 1) # <- Bitmask
                        # 0x1 = External control
                        # 0x4 = External voltage/current control
                        # 0x8 = Controled by BMS (causes Error #67, BMS lost, if external control is interrupted).
        self._dbusmppt.add_path('/Link/BatteryCurrent', 0)
        self._dbusmppt.add_path('/Link/ChargeCurrent', 0)
        self._dbusmppt.add_path('/Link/ChargeVoltage', 0)
        self._dbusmppt.add_path('/Link/NetworkStatus', 4) # <- Bitmask
                        # 0x01 = Slave
                        # 0x02 = Master
                        # 0x04 = Standalone
                        # 0x20 = Using I-sense (/Link/BatteryCurrent)
                        # 0x40 = Using T-sense (/Link/TemperatureSense)
                        # 0x80 = Using V-sense (/Link/VoltageSense)
        self._dbusmppt.add_path('/Link/TemperatureSense', 0)
        self._dbusmppt.add_path('/Link/TemperatureSenseActive', 0)
        self._dbusmppt.add_path('/Link/VoltageSense', 0)
        self._dbusmppt.add_path('/Link/VoltageSenseActive', 0)
        # settings
        self._dbusmppt.add_path('/Settings/BmsPresent', None)
        self._dbusmppt.add_path('/Settings/ChargeCurrentLimit', 80)
        # other paths
        self._dbusmppt.add_path('/Yield/User', self.history.get('/Yield/User', 0))
        self._dbusmppt.add_path('/Yield/System', self.history.get('/Yield/System', 0))
        self._dbusmppt.add_path('/ErrorCode', 0)
        self._dbusmppt.add_path('/DeviceOffReason', 0)
        # hass-victron reads these standard alarm registers. Missing D-Bus
        # paths are exposed by Modbus as 0xffff, which is not a valid alarm.
        self._dbusmppt.add_path('/Alarms/LowVoltage', 0)
        self._dbusmppt.add_path('/Alarms/HighVoltage', 0)
        self._dbusmppt.add_path('/State', 0)
        # Victron ChargerMode only accepts 1 (On) or 4 (Off). Charging activity
        # is reported separately through /State and /MppOperationMode.
        self._dbusmppt.add_path('/Mode', 1)
        self._dbusmppt.add_path('/MppOperationMode', 0)
        self._dbusmppt.add_path('/Relay/0/State', None)

        # Raw P18 solar warnings which do not all have an unambiguous Victron
        # error code. They are intentionally exposed as diagnostics first.
        self._dbusmppt.add_path('/Diagnostics/P18/Pv1VoltageHigh', 0)
        self._dbusmppt.add_path('/Diagnostics/P18/Pv2VoltageHigh', 0)
        self._dbusmppt.add_path('/Diagnostics/P18/Mppt1OverloadWarning', 0)
        self._dbusmppt.add_path('/Diagnostics/P18/Mppt2OverloadWarning', 0)
        self._dbusmppt.add_path('/Diagnostics/P18/BatteryTooLowToChargeForScc1', 0)
        self._dbusmppt.add_path('/Diagnostics/P18/BatteryTooLowToChargeForScc2', 0)
        self._dbusmppt.add_path('/Diagnostics/P18/CurrentHidraw', self.tty)
        self._dbusmppt.add_path('/Diagnostics/P18/BackendPort', port)
        self._dbusmppt.add_path('/Diagnostics/P18/NumberOfChargers', 1)
        self._dbusinverter.add_path('/Diagnostics/P18/CurrentHidraw', self.tty)
        self._dbusinverter.add_path('/Diagnostics/P18/BackendPort', port)
        self._dbusinverter.add_path('/Diagnostics/P18/NumberOfChargers', 1)
        self._dbusinverter.add_path(
            '/Diagnostics/P18/RefreshTopology', 0, writeable=True,
            onchangecallback=self._change_refresh_topology,
        )
        
        # history
        self._dbusmppt.add_path('/History/Overall/DaysAvailable', 1)

        # history daily
        for history_path in HISTORY_PATHS:
            if history_path.startswith('/History/'):
                self._dbusmppt.add_path(history_path, self.history.get(history_path, 0))

            
        # self._dbusmppt.add_path('/History/Overall/MaxPvVoltage', 0)
        # self._dbusmppt.add_path('/History/Overall/MaxBatteryVoltage', 0)
        # self._dbusmppt.add_path('/History/Overall/MinBatteryVoltage', 0)

        logging.info(f"Paths for 'solarcharger' created.")

        self._dbusinverter.register()
        self._dbusmppt.register()

        logging.info(f'Added to D-Bus: {self._dbusinverter}')
        logging.info(f'Added to D-Bus: {self._dbusmppt}')

        self._schedule_poll()
        GLib.timeout_add_seconds(60, self._refresh_parallel_count)
        GLib.timeout_add_seconds(300, self._save_history)
        self._refresh_parallel_count()
        self._initializing = False

    def _used_device_instances(self):
        used = set()
        try:
            bus = dbusconnection()
            for name in bus.list_names():
                if not (
                    name.startswith('com.victronenergy.inverter.')
                    or name.startswith('com.victronenergy.solarcharger.')
                ):
                    continue
                if name.endswith(service_suffix(self.serial_number)):
                    continue
                value = VeDbusItemImport(bus, name, '/DeviceInstance').get_value()
                if value is not None:
                    used.add(int(value))
        except Exception:
            logging.warning("Unable to enumerate used Device Instances", exc_info=True)
        return used

    def _schedule_poll(self):
        if self._poll_source is not None:
            GLib.source_remove(self._poll_source)
        self._poll_source = GLib.timeout_add_seconds(self.poll_interval, self._update)

    def _setting_changed(self, setting, oldvalue, newvalue):
        if setting == 'custom_name':
            self.custom_name = str(newvalue)[:32]
            if hasattr(self, '_dbusinverter'):
                self._dbusinverter['/CustomName'] = self.custom_name
                self._dbusmppt['/CustomName'] = self.custom_name
        elif setting == 'poll_interval':
            self.poll_interval = clamp_poll_interval(newvalue)
            if hasattr(self, '_dbusinverter'):
                self._dbusinverter['/Settings/PollInterval'] = self.poll_interval
                self._dbusmppt['/Settings/PollInterval'] = self.poll_interval
                self._schedule_poll()
        elif setting == 'device_instance':
            new_instance = int(newvalue)
            if new_instance in self._used_device_instances():
                logging.error("Rejecting conflicting Device Instance %s", new_instance)
                if oldvalue is not None and int(oldvalue) != new_instance:
                    GLib.idle_add(self._restore_device_instance, int(oldvalue))
                return
            if hasattr(self, '_dbusinverter'):
                self._dbusinverter['/Settings/DeviceInstance'] = new_instance
                self._dbusmppt['/Settings/DeviceInstance'] = new_instance
                if not self._initializing:
                    self._request_restart()

    def _restore_device_instance(self, value):
        self._settings['device_instance'] = int(value)
        return False

    def _request_restart(self):
        global mainloop
        self._restart_requested = True
        self.exit_code = 75
        GLib.idle_add(mainloop.quit)

    def _change_custom_name(self, path, value):
        value = str(value)[:32]
        self._settings['custom_name'] = value
        return True

    def _change_poll_interval(self, path, value):
        try:
            value = int(value)
        except (TypeError, ValueError):
            return False
        if not 5 <= value <= 60:
            return False
        self._settings['poll_interval'] = value
        return True

    def _change_device_instance(self, path, value):
        try:
            value = int(value)
        except (TypeError, ValueError):
            return False
        current = int(self._settings['device_instance'])
        if value == current:
            return True
        if not 1 <= value <= 255 or value in self._used_device_instances():
            logging.warning("Device Instance %s is invalid or already in use", value)
            return False
        self._settings['device_instance'] = value
        return True

    def _change_refresh_topology(self, path, value):
        GLib.idle_add(self._refresh_parallel_count)
        return True

    def _refresh_parallel_count(self):
        global numberOfChargers
        responses = []
        for parallel_id in range(7):
            try:
                responses.append(runInverterCommands('get-p-rated', (parallel_id,), timeout_sec=4))
            except Exception:
                continue
        fallback = 1
        try:
            names = dbusconnection().list_names()
            fallback = len([name for name in names if name.startswith('com.victronenergy.inverter.mppsolar-inverter.sn_')])
        except Exception:
            pass
        numberOfChargers = count_parallel_members(responses, fallback)
        self._dbusinverter['/Diagnostics/P18/NumberOfChargers'] = numberOfChargers
        self._dbusmppt['/Diagnostics/P18/NumberOfChargers'] = numberOfChargers
        return True

    def _save_history(self):
        if hasattr(self, '_dbusmppt'):
            self.history_store.save({path: self._dbusmppt[path] for path in HISTORY_PATHS})
        return True

    def shutdown(self):
        try:
            self._save_history()
        except Exception:
            logging.exception("Unable to persist history during shutdown")
    
    def setupInverterDefaultPaths(self, service, connection, deviceinstance, productname):
        # Create the management objects, as specified in the ccgx dbus-api document
        service.add_path('/Mgmt/ProcessName', __file__)
        service.add_path('/Mgmt/ProcessVersion', 'version f{VERSION}, and running on Python ' + platform.python_version())
        service.add_path('/Mgmt/Connection', connection)

        # Create the mandatory objects
        service.add_path('/DeviceInstance', deviceinstance)
        service.add_path('/Serial', self.serial_number)
        service.add_path('/CustomName', self.custom_name, writeable=True, onchangecallback=self._change_custom_name)
        service.add_path('/Settings/PollInterval', self.poll_interval, writeable=True, onchangecallback=self._change_poll_interval)
        service.add_path('/Settings/DeviceInstance', deviceinstance, writeable=True, onchangecallback=self._change_device_instance)
        service.add_path('/ProductId', None)
        service.add_path('/ProductName', productname)
        service.add_path('/FirmwareVersion', None)
        service.add_path('/HardwareVersion', None)
        service.add_path('/Connected', 1)

        # Create the paths for modifying the system manually
        service.add_path('/Settings/Reset', None, writeable=True, onchangecallback=self._change)
        service.add_path('/Settings/Charger', None, writeable=True, onchangecallback=self._change)
        service.add_path('/Settings/Output', None, writeable=True, onchangecallback=self._change)

    def setupChargerDefaultPaths(self, service, connection, deviceinstance, productname):
        # Create the management objects, as specified in the ccgx dbus-api document
        service.add_path('/Mgmt/ProcessName', __file__)
        service.add_path('/Mgmt/ProcessVersion', 'version f{VERSION}, and running on Python ' + platform.python_version())
        service.add_path('/Mgmt/Connection', connection)

        # Create the mandatory objects
        service.add_path('/DeviceInstance', deviceinstance)
        service.add_path('/Serial', self.serial_number)
        service.add_path('/CustomName', self.custom_name, writeable=True, onchangecallback=self._change_custom_name)
        service.add_path('/Settings/PollInterval', self.poll_interval, writeable=True, onchangecallback=self._change_poll_interval)
        service.add_path('/Settings/DeviceInstance', deviceinstance, writeable=True, onchangecallback=self._change_device_instance)
        service.add_path('/ProductId', None)
        service.add_path('/ProductName', productname)
        service.add_path('/FirmwareVersion', None)
        service.add_path('/HardwareVersion', None)
        service.add_path('/Connected', 1)

        # Create the paths for modifying the system manually
        service.add_path('/Settings/Reset', None, writeable=True, onchangecallback=self._change)
        service.add_path('/Settings/Charger', None, writeable=True, onchangecallback=self._change)
        service.add_path('/Settings/Output', None, writeable=True, onchangecallback=self._change)

    def _updateInternal(self):
        # Store in the paths all values that were updated from _handleChangedValue
        with self._dbusinverter as i, self._dbusmppt as m:
            for path, value, in self._queued_updates:
                i[path] = value
                m[path] = value
            self._queued_updates = []

    def _update(self):
        global mainloop
        logging.info("{} updating".format(datetime.datetime.now().time()))
        try:
            result = self._update_PI18()
            self._consecutive_errors = 0
            return result
        except Exception:
            logging.exception('Error in update loop', exc_info=True)
            self._consecutive_errors += 1
            self._updateInternal()
            if self._consecutive_errors >= 3:
                logging.error("Three consecutive polling failures; asking manager to restart device")
                self.exit_code = 1
                mainloop.quit()
                return False
            return True

    def _change(self, path, value):
        global mainloop
        logging.info("updated %s to %s" % (path, value))
        if path == '/Settings/Reset':
            logging.info("Restarting!")
            mainloop.quit()
            exit
        try: 
            return self._change_PI18(path, value)
        except Exception as exc:
            logging.exception('Error in change loop', exc_info=True)
            mainloop.quit()
            return False

    def _update_PI18(self):
        # Update charge voltage

        battery_service = find_battery_service()
        generated = data = mode = rated = alerts = {"result": "init", "message": "not initialized"}

        if battery_service:
            systemMaxChargeVoltage = VeDbusItemImport(dbusconnection(), battery_service, '/Info/MaxChargeVoltage')
            systemMaxChargeCurrent = VeDbusItemImport(dbusconnection(), battery_service, '/Info/MaxChargeCurrent')
            try:
                setMaxChargingVoltage(systemMaxChargeVoltage.get_value(), systemMaxChargeVoltage.get_value())
            except:
                logging.warning("bulkVoltage and/or floatVoltage not defined.")
        # try:
        #     setMaxChargingCurrent(0, systemMaxChargeCurrent.get_value())
        #     setMaxUtilityChargingCurrent(0, systemMaxChargeCurrent.get_value())
        # except:
        #     logging.warning("Max charge current not defined.", exc_info=True)
        
        try:
            generated = runInverterCommands('get-total-generated')
            data = runInverterCommands('get-status')
            mode = runInverterCommands('get-mode')
            rated = runInverterCommands('get-rated')
            alerts = runInverterCommands('get-errors')

        except:
            results = {
                "generated": generated,
                "data": data,
                "mode": mode,
                "rated": rated,
                "alerts": alerts
            }

            # Vérifier s'il y a des erreurs
            for name, result in results.items():
                if isinstance(result, dict) and result.get("result") == "error":
                    logging.warning(f"Error in update PI18 loop. {name} → {result.get('message')}")
            raise RuntimeError("P18 polling command failed") from exc

        with self._dbusinverter as i, self._dbusmppt as m:
            # 0=Off;1=Low Power;2=Fault;9=Inverting
            invMode = mode.get('data', {}).get('mode', i['/State'])
            if invMode == 'Battery mode':
                i['/State'] = 9 # Inverting
            elif invMode == 'Fault mode':
                i['/State'] = 2 # Fault mode
            else:
                i['/State'] = 0 # OFF

            # Normal operation, read data
            i['/Dc/0/Voltage'] = data.get('data').get('battery_voltage', {}).get("value", i['/Dc/0/Voltage'])

            i['/Ac/Out/L1/V'] = data.get('data').get('ac_output_voltage', {}).get("value", i['/Ac/Out/L1/V'])
            i['/Ac/Out/L1/P'] = data.get('data').get('ac_output_active_power', {}).get("value", i['/Ac/Out/L1/P'])
            if i['/Ac/Out/L1/V'] != 0 and i['/Ac/Out/L1/P'] != 0:
                output_current = i['/Ac/Out/L1/P'] / i['/Ac/Out/L1/V']
                i['/Ac/Out/L1/I'] = output_current
            i['/Ac/Out/L1/F'] = data.get('data').get('ac_output_freq', {}).get("value", i['/Ac/Out/L1/F'])
            i['/Temperature'] = data.get('data').get('inverter_heat_sink_temp', {}).get("value", i['/Temperature'])

            # Solar charger
            if data.get('data').get('pv1_input_power', {}).get("value", 0) > 0:
                m['/State'] = 3
            else:
                m['/State'] = 0
            m['/Pv/0/V'] = data.get('data').get('pv1_input_voltage', {}).get("value", m['/Pv/0/V'])
            m['/Pv/V'] = data.get('data').get('pv1_input_voltage', {}).get("value", m['/Pv/V'])
            m['/Pv/0/P'] = data.get('data').get('pv1_input_power', {}).get("value", m['/Pv/0/P'])
            m['/Yield/Power'] = data.get('data').get('pv1_input_power', {}).get("value", m['/Yield/Power'])
            if generated.get('data').get('wh') != 0 and generated.get('data').get('wh') != None:
                m['/Yield/User'] = generated.get('data').get('wh') / 1000
                m['/Yield/System'] = generated.get('data').get('wh') / 1000
                m['/History/Overall/Yield'] = generated.get('data').get('wh') / 1000
            m['/MppOperationMode'] = 2 if (data.get('data').get('pv1_input_power', {}).get("value", 0) > 0) else 0
            m['/Link/ChargeCurrent'] =  rated.get('data').get('max_charging_current', {}).get("value",  m['/Link/ChargeCurrent']) # <- Maximum charge current. Must be written every 60 seconds. Used by GX device if there is a BMS or user limit.
            m['/Link/ChargeVoltage'] =  rated.get('data').get('battery_bulk_voltage', {}).get("value",  m['/Link/ChargeVoltage']) # <- Charge voltage. Must be written every 60 seconds. Used by GX device to communicate BMS charge voltages.
            m['/DC/0/Temperature'] = data.get('data').get('mppt1_charger_temperature', {}).get("value", m['/DC/0/Temperature'])
            m['/Dc/0/Voltage'] = data.get('data').get('battery_voltage', {}).get("value", m['/Dc/0/Voltage'])
            m['/Dc/0/Current'] = data.get('data').get('battery_charge_current', {}).get("value", m['/Dc/0/Current'])

            # P18 fault_code and warning flags are independent fields. Do not
            # gate warning processing on fault_code: doing so hides warnings
            # whenever the two-digit P18 fault code is zero.
            alert_data = alerts.get('data')
            if isinstance(alert_data, dict):
                try:
                    fault_code = int(alert_data.get('fault_code') or 0)
                except (TypeError, ValueError):
                    logging.warning("Invalid P18 fault_code: %r", alert_data.get('fault_code'))
                    fault_code = 0

                def is_active(name):
                    return bool(alert_data.get(name, False))

                inverter_fault = invMode == 'Fault mode' or fault_code != 0

                def severity(active):
                    if not active:
                        return 0
                    return 2 if inverter_fault else 1

                battery_low = is_active('battery_low')
                battery_under = is_active('battery_under')
                output_circuit_short = is_active('output_circuit_short')

                # Standard com.victronenergy.inverter alarm paths.
                low_voltage_alarm = 2 if battery_under else severity(battery_low)
                high_voltage_alarm = severity(is_active('battery_voltage_high'))
                i['/Alarms/LowVoltage'] = low_voltage_alarm
                i['/Alarms/HighVoltage'] = high_voltage_alarm
                i['/Alarms/HighTemperature'] = severity(is_active('inverter_over_temperature'))
                i['/Alarms/Overload'] = 2 if output_circuit_short else severity(is_active('over_load'))

                # The solar-charger Modbus map exposes the same battery-voltage
                # alarm state. Always publish 0/1/2 so Home Assistant never sees
                # the 0xffff sentinel used for a missing D-Bus path.
                m['/Alarms/LowVoltage'] = low_voltage_alarm
                m['/Alarms/HighVoltage'] = high_voltage_alarm

                # Preserve the complete P18 diagnosis without turning the
                # expected off-grid LineFail flag into a Venus alarm.
                i['/Diagnostics/P18/FaultCode'] = fault_code
                i['/Diagnostics/P18/LineFail'] = int(is_active('line_fail'))
                i['/Diagnostics/P18/OutputCircuitShort'] = int(output_circuit_short)
                i['/Diagnostics/P18/FanLock'] = int(is_active('fan_lock'))
                i['/Diagnostics/P18/EepromFail'] = int(is_active('eeprom_fail'))
                i['/Diagnostics/P18/PowerLimit'] = int(is_active('power_limit'))

                pv1_voltage_high = is_active('pv1_voltage_high')
                pv2_voltage_high = is_active('pv2_voltage_high')
                mppt1_overload = is_active('mppt1_overload_warning')
                mppt2_overload = is_active('mppt2_overload_warning')
                scc1_battery_low = is_active('battery_too_low_to_charge_for_scc1')
                scc2_battery_low = is_active('battery_too_low_to_charge_for_scc2')

                m['/Diagnostics/P18/Pv1VoltageHigh'] = int(pv1_voltage_high)
                m['/Diagnostics/P18/Pv2VoltageHigh'] = int(pv2_voltage_high)
                m['/Diagnostics/P18/Mppt1OverloadWarning'] = int(mppt1_overload)
                m['/Diagnostics/P18/Mppt2OverloadWarning'] = int(mppt2_overload)
                m['/Diagnostics/P18/BatteryTooLowToChargeForScc1'] = int(scc1_battery_low)
                m['/Diagnostics/P18/BatteryTooLowToChargeForScc2'] = int(scc2_battery_low)

                # Victron error 33 is PV over-voltage. MPPT overload remains a
                # diagnostic because P18 does not say whether it is current or
                # power overload (Victron codes 18/34/35 have different meanings).
                m['/ErrorCode'] = 33 if (pv1_voltage_high or pv2_voltage_high) else 0

                # Keep /Mode at 1. Off conditions are represented through State,
                # MppOperationMode and the standard DeviceOffReason bitmask.
                device_off_reason = 0
                if data.get('data', {}).get('pv1_input_power', {}).get('value', 0) <= 0:
                    device_off_reason |= 0x400  # No/low panel power.
                if scc1_battery_low or scc2_battery_low:
                    device_off_reason |= 0x800  # No/low battery power.
                if m['/ErrorCode'] != 0:
                    device_off_reason |= 0x8000  # Active alarm.
                m['/DeviceOffReason'] = device_off_reason

                normalized_alerts = {
                    'fault_code': fault_code,
                    **{name: bool(value) for name, value in alert_data.items() if name != 'fault_code'}
                }
                if normalized_alerts != self._last_p18_alerts:
                    active_alerts = [
                        name for name, value in normalized_alerts.items()
                        if name != 'fault_code' and value
                    ]
                    logging.warning(
                        "P18 alert status changed: fault_code=%s, active=%s",
                        fault_code,
                        ', '.join(active_alerts) if active_alerts else 'none'
                    )
                    self._last_p18_alerts = normalized_alerts
            else:
                # Preserve the last known alarm state on a transient malformed
                # get-errors response instead of falsely clearing every alarm.
                logging.warning("Ignoring invalid P18 get-errors response: %r", alerts)
            # History
            # if generatedToday.get("generated_energy_for_day") != 0 and generatedToday.get("generated_energy_for_day") != None:
            #     m["/History/Overall/Yield"] = generatedToday.get("generated_energy_for_day") / 1000
            
            # if generatedToday.get("day") != currentDay:
            #     # Reset daily history when day change
            #     currentDay = generatedToday.get("day")
            #     maxPVVoltage = 0
            #     maxPVPower = 0
            #     maxBatteryVoltage = 0
            #     minBatteryVoltage = 0
            #     maxBatteryCurrent = 0

            if data.get('data').get('pv1_input_voltage', {}).get("value") != None and data.get('data').get('pv1_input_voltage', {}).get("value") > m["/History/Overall/MaxPvVoltage"]:
                m["/History/Overall/MaxPvVoltage"] = data.get('data').get('pv1_input_voltage', {}).get("value")
            if data.get('data').get('pv1_input_power', {}).get("value") != None and data.get('data').get('pv1_input_power', {}).get("value") > m["/History/Overall/MaxPower"]:
                m["/History/Overall/MaxPower"] = data.get('data').get('pv1_input_power', {}).get("value")
            if data.get('data').get('battery_voltage', {}).get("value") != None and data.get('data').get('battery_voltage', {}).get("value") > m["/History/Overall/MaxBatteryVoltage"]:
                m["/History/Overall/MaxBatteryVoltage"] = data.get('data').get('battery_voltage', {}).get("value")
            battery_voltage = data.get('data').get('battery_voltage', {}).get("value")
            if battery_voltage is not None and (
                m["/History/Overall/MinBatteryVoltage"] <= 0
                or battery_voltage < m["/History/Overall/MinBatteryVoltage"]
            ):
                m["/History/Overall/MinBatteryVoltage"] = battery_voltage
            if data.get('data').get('battery_charge_current', {}).get("value") != None and data.get('data').get('battery_charge_current', {}).get("value") > m["/History/Overall/MaxBatteryCurrent"]:
                m["/History/Overall/MaxBatteryCurrent"] = data.get('data').get('battery_charge_current', {}).get("value")
            if m['/State'] == 3:
                m['/History/Overall/TimeInBulk'] += self.poll_interval

        # Execute updates of previously updated values
        self._updateInternal()
        return True

    def _change_PI18(self, path, value):
        # Link
        if path == '/Link':
            logging.info("{} : {}".format(path, value))

        if path == '/Link/ChargeCurrent':
            logging.info("/Link/ChargeCurrent : {}".format(value))

        if path == '/Link/ChargeCurrent':
            logging.info("/Link/ChargeCurrent : {}".format(value))

        # Mode settings
        if path == '/Mode': # 1=Charger Only;2=Inverter Only;3=On;4=Off(?)
            if value == 1:
                logging.info("setting mode to 'Charger Only'(Charger=Util) ({})".format(setChargerPriority(1), setOutputSource(1)))
            elif value == 2:
                logging.info("setting mode to 'Inverter Only'(Charger=Solar & Output=SBU) ({},{})".format(setChargerPriority(0), setOutputSource(2)))
            elif value == 3:
                logging.info("setting mode to 'ON=Charge+Invert'(Charger=Util & Output=SBU) ({},{})".format(setChargerPriority(1), setOutputSource(2)))
            elif value == 4:
                logging.info("setting mode to 'OFF'(Charger=Solar) ({})".format(setChargerPriority(3), setOutputSource(2)))
            else:
                logging.info("setting mode not understood ({})".format(value))
            self._queued_updates.append((path, value))        
        return True # accept the change

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--serial", "-s", required=True, help="Current /dev/hidraw path")
    parser.add_argument("--serial-number", required=True, help="Validated permanent P18 serial")
    parser.add_argument("--port", required=True, type=int, help="Manager-owned inverterd TCP port")
    global args
    args = parser.parse_args()

    from dbus.mainloop.glib import DBusGMainLoop
    # Have a mainloop, so we can send/receive asynchronous calls to and from dbus
    DBusGMainLoop(set_as_default=True)

    global mainloop
    mainloop = GLib.MainLoop()
    mppservice = DbusMppSolarService(
        tty=args.serial,
        serial_number=args.serial_number,
        backend_port=args.port,
    )
    logging.info('Created service & connected to dbus, switching over to GLib.MainLoop() (= event based)')

    def stop_handler(signum, frame):
        logging.info("Received signal %s", signum)
        mainloop.quit()

    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)
    atexit.register(mppservice.shutdown)
    mainloop.run()
    mppservice.shutdown()
    return mppservice.exit_code

if __name__ == "__main__":
    sys.exit(main())
