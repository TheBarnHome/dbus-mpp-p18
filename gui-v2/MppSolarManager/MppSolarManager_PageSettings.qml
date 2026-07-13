pragma ComponentBehavior: Bound

import QtQuick
import Victron.VenusOS

Page {
	id: root

	title: "MPP Solar P18"
	readonly property string managerUid: BackendConnection.type === BackendConnection.MqttSource
			? "mqtt/mppsolar/0"
			: "dbus/com.victronenergy.mppsolar.manager"

	VeQuickItem {
		id: deviceCount
		uid: root.managerUid + "/DeviceCount"
	}

	GradientListView {
		model: VisibleItemModel {
			PrimaryListLabel {
				text: "No P18 inverter is currently connected."
				preferredVisible: !deviceCount.valid || deviceCount.value === 0
			}

			SettingsColumn {
				width: parent ? parent.width : 0

				Repeater {
					model: 7

					delegate: ListNavigation {
						id: deviceDelegate
						property int slotIndex: index
						readonly property string slotUid: root.managerUid + "/Devices/" + slotIndex
						text: customName.valid && customName.value ? customName.value : serial.value
						secondaryText: serial.valid ? serial.value : ""
						preferredVisible: connected.valid && connected.value === 1
						onClicked: Global.pageManager.pushPage(devicePage, {
							"title": text,
							"slotIndex": slotIndex
						})

						VeQuickItem { id: connected; uid: deviceDelegate.slotUid + "/Connected" }
						VeQuickItem { id: serial; uid: deviceDelegate.slotUid + "/Serial" }
						VeQuickItem { id: customName; uid: deviceDelegate.slotUid + "/CustomName" }
					}
				}
			}
		}
	}

	Component {
		id: devicePage

		Page {
			id: deviceSettingsPage
			required property int slotIndex
			readonly property string slotUid: root.managerUid + "/Devices/" + slotIndex

			GradientListView {
				model: VisibleItemModel {
					ListTextField {
						text: "Name"
						maximumLength: 32
						dataItem.uid: deviceSettingsPage.slotUid + "/CustomName"
					}

					ListSpinBox {
						text: "VRM device instance"
						from: 1
						to: 255
						decimals: 0
						stepSize: 1
						dataItem.uid: deviceSettingsPage.slotUid + "/DeviceInstance"
					}

					PrimaryListLabel {
						text: "Changing the instance restarts both services and may create a new external history series. Conflicting instances are rejected."
					}

					ListSpinBox {
						text: "Polling interval"
						suffix: " s"
						from: 5
						to: 60
						decimals: 0
						stepSize: 1
						dataItem.uid: deviceSettingsPage.slotUid + "/PollInterval"
					}

					ListText {
						text: "Serial number"
						dataItem.uid: deviceSettingsPage.slotUid + "/Serial"
					}

					ListText {
						text: "Current HID path"
						dataItem.uid: deviceSettingsPage.slotUid + "/Hidraw"
					}
				}
			}
		}
	}
}
