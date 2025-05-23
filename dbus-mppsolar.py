#!/usr/bin/env python3

"""
Handle automatic connection with MPP Solar inverter compatible device (VEVOR)
This will output 2 dbus services, one for Inverter data another one for control
via VRM of the features.
"""
VERSION = 'v0.2' 

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
import subprocess
import time
import atexit
import concurrent.futures
from inverterd import Client, Format

logging.basicConfig(level=logging.WARNING, format='%(asctime)s - %(levelname)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

# our own packages
sys.path.insert(1, os.path.join(os.path.dirname(__file__), 'velib_python'))
from vedbus import VeDbusService, VeDbusItemExport, VeDbusItemImport

process = None
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

def start_inverterd(usb_path: str):
    global process
    global port

    process = subprocess.Popen(
        ['/data/etc/dbus-mppsolar/inverterd', '--usb-path', usb_path, '--port', str(port), '--delay 1000'],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    return process

def stop_inverterd():
    if process:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()

# Inverter commands to read from the serial
def safe_runInverterCommands(command: str, params: tuple = ()):
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
    Exécute la commande inverter avec surveillance du timeout.
    Si inverterd ne répond pas, il est redémarré automatiquement.
    """
    global usb_path

    while True:
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(safe_runInverterCommands, command, params)
            try:
                return future.result(timeout=timeout_sec)
            except concurrent.futures.TimeoutError:
                logging.warning(f"[ERROR] inverterd is not responding to '{command}', restarting...")
                stop_inverterd()
                time.sleep(3)
                start_inverterd(usb_path)
                time.sleep(3)  # Laisse un peu de temps au process pour redémarrer

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
    def __init__(self, tty, deviceinstance, productname='MPPSolar', connection='MPPSolar interface', json_file_path='/data/etc/dbus-mppsolar/config.json'):
        global numberOfChargers
        global port
        global usb_path

        self._queued_updates = []
        
        # For production history
        energyProductionDays = int(1)
        currentDay = 0
        minBatteryVoltage = 100.0
        maxBatteryVoltage = 0
        maxBatteryCurrent = 0
        maxPVPower = 0
        maxPVVoltage = 0

        # Get the name from config file if available
        if os.path.exists(json_file_path):
            with open(json_file_path, 'r') as json_file:
                config = json.load(json_file)
            if tty in config:
                deviceinstance = config[tty].get('deviceinstance', 0)
                productname_value = config[tty].get('productname', None)
                self.updateInterval = config[tty].get('updateInterval', 10000)
                if productname_value is not None:
                    productname = productname_value
                    logging.info("Product named from config : {}".format(productname_value))
                numberOfChargers = config[tty].get('numberOfChargers', 1)

                port = 8305 + deviceinstance
                usb_path = tty
                start_inverterd(usb_path)

        if not os.path.exists("{}".format(usb_path)):
            logging.warning("Inverter not connected on {}".format(tty))
            sys.exit()

        logging.info(f"Connected to inverter on {tty}, setting up dbus with /DeviceInstance = {deviceinstance}")
        
        # Create the services
        hidraw = tty.strip('/dev/')
        self._dbusinverter = VeDbusService(f'com.victronenergy.inverter.mppsolar-inverter.{hidraw}', bus=dbusconnection(), register=False)
        self._dbusmppt = VeDbusService(f'com.victronenergy.solarcharger.mppsolar-charger.{hidraw}', bus=dbusconnection(), register=False)

        # Set up default paths
        self.setupInverterDefaultPaths(self._dbusinverter, connection, deviceinstance, f"Inverter {productname}")
        self.setupChargerDefaultPaths(self._dbusmppt, connection, deviceinstance, f"Charger {productname}")

        # Create paths for inverter
        self._dbusinverter.add_path('/Dc/0/Voltage', 0)
        self._dbusinverter.add_path('/Ac/Out/L1/V', 0)
        self._dbusinverter.add_path('/Ac/Out/L1/I', 0)
        self._dbusinverter.add_path('/Ac/Out/L1/P', 0)
        self._dbusinverter.add_path('/Ac/Out/L1/F', 0)
        self._dbusinverter.add_path('/Mode', 0)                     #<- Switch position: 2=Inverter on; 4=Off; 5=Low Power/ECO
        self._dbusinverter.add_path('/State', 0)                    #<- 0=Off; 1=Low Power; 2=Fault; 9=Inverting
        self._dbusinverter.add_path('/Temperature', 123)

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
        self._dbusmppt.add_path('/Yield/User', 0)
        self._dbusmppt.add_path('/Yield/System', 0)
        self._dbusmppt.add_path('/ErrorCode', 0)
        self._dbusmppt.add_path('/State', 0)
        self._dbusmppt.add_path('/Mode', 0)
        self._dbusmppt.add_path('/MppOperationMode', 0)
        self._dbusmppt.add_path('/Relay/0/State', None)
        
        # history
        self._dbusmppt.add_path('/History/Overall/DaysAvailable', 1)

        # history daily
        self._dbusmppt.add_path("/History/Overall/Yield", 0)
        self._dbusmppt.add_path("/History/Overall/Consumption", 0)
        self._dbusmppt.add_path("/History/Overall/MaxPower", 0)
        self._dbusmppt.add_path("/History/Overall/MaxPvVoltage", 0)
        self._dbusmppt.add_path("/History/Overall/MinBatteryVoltage", 0)
        self._dbusmppt.add_path("/History/Overall/MaxBatteryVoltage", 0)
        self._dbusmppt.add_path("/History/Overall/MaxBatteryCurrent", 0)
        self._dbusmppt.add_path("/History/Overall/TimeInBulk", 0)
        self._dbusmppt.add_path("/History/Overall/TimeInAbsorption", 0)
        self._dbusmppt.add_path("/History/Overall/TimeInFloat", 0)
        self._dbusmppt.add_path("/History/Overall/LastError1", 0)
        self._dbusmppt.add_path("/History/Overall/LastError2", 0)
        self._dbusmppt.add_path("/History/Overall/LastError3", 0)
        self._dbusmppt.add_path("/History/Overall/LastError4", 0)

            
        # self._dbusmppt.add_path('/History/Overall/MaxPvVoltage', 0)
        # self._dbusmppt.add_path('/History/Overall/MaxBatteryVoltage', 0)
        # self._dbusmppt.add_path('/History/Overall/MinBatteryVoltage', 0)

        logging.info(f"Paths for 'solarcharger' created.")

        self._dbusinverter.register()
        self._dbusmppt.register()

        logging.info(f'Added to D-Bus: {self._dbusinverter}')
        logging.info(f'Added to D-Bus: {self._dbusmppt}')

        GLib.timeout_add(self.updateInterval, self._update)
    
    def setupInverterDefaultPaths(self, service, connection, deviceinstance, productname):
        # Create the management objects, as specified in the ccgx dbus-api document
        service.add_path('/Mgmt/ProcessName', __file__)
        service.add_path('/Mgmt/ProcessVersion', 'version f{VERSION}, and running on Python ' + platform.python_version())
        service.add_path('/Mgmt/Connection', connection)

        # Create the mandatory objects
        service.add_path('/DeviceInstance', deviceinstance)
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
            return self._update_PI18()
        except:
            logging.exception('Error in update loop', exc_info=True)
            # mainloop.quit()
            self._updateInternal()
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
        except:
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
            self._updateInternal()
            return True

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
            m['/MppOperationMode'] = 2 if (data.get('data').get('pv1_input_power', {}).get("value", 0) > 0) else 0
            m['/Link/ChargeCurrent'] =  rated.get('data').get('max_charging_current', {}).get("value",  m['/Link/ChargeCurrent']) # <- Maximum charge current. Must be written every 60 seconds. Used by GX device if there is a BMS or user limit.
            m['/Link/ChargeVoltage'] =  rated.get('data').get('battery_bulk_voltage', {}).get("value",  m['/Link/ChargeVoltage']) # <- Charge voltage. Must be written every 60 seconds. Used by GX device to communicate BMS charge voltages.
            m['/DC/0/Temperature'] = data.get('data').get('mppt1_charger_temperature', {}).get("value", m['/DC/0/Temperature'])
            m['/Dc/0/Voltage'] = data.get('data').get('battery_voltage', {}).get("value", m['/Dc/0/Voltage'])
            m['/Dc/0/Current'] = data.get('data').get('battery_charge_current', {}).get("value", m['/Dc/0/Current'])

            # Error code handling
            if alerts.get('data').get('fault_code') != 0:
                if alerts.get('data').get('inverter_over_temperature'):
                    m['/ErrorCode'] = 17
                if alerts.get('data').get('mppt1_overload_warning'):
                    m['/ErrorCode'] = 18
                if alerts.get('data').get('inverter_over_temperature'):
                    i['/ErrorCode'] = 17
                if alerts.get('data').get('over_load'):
                    i['/ErrorCode'] = 18
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
            if data.get('data').get('battery_voltage', {}).get("value") != None and data.get('data').get('battery_voltage', {}).get("value") < m["/History/Overall/MinBatteryVoltage"]:
                m["/History/Overall/MinBatteryVoltage"] = data.get('data').get('battery_voltage', {}).get("value")
            if data.get('data').get('battery_charge_current', {}).get("value") != None and data.get('data').get('battery_charge_current', {}).get("value") > m["/History/Overall/MaxBatteryCurrent"]:
                m["/History/Overall/MaxBatteryCurrent"] = data.get('data').get('battery_charge_current', {}).get("value")

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
    parser.add_argument("--serial","-s", required=True, type=str)
    global args
    args = parser.parse_args()

    from dbus.mainloop.glib import DBusGMainLoop
    # Have a mainloop, so we can send/receive asynchronous calls to and from dbus
    DBusGMainLoop(set_as_default=True)

    mppservice = DbusMppSolarService(tty=args.serial, deviceinstance=0)
    logging.info('Created service & connected to dbus, switching over to GLib.MainLoop() (= event based)')

    global mainloop

    mainloop = GLib.MainLoop()
    mainloop.run()

    atexit.register(stop_inverterd)  # S'assure que inverterd est tué à la fin du script

if __name__ == "__main__":
    main()