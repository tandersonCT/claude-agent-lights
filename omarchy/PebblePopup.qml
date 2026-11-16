// The Pebble Shell drawer (denon.bar's PopupPanel) for the colour picker.
// It must come in through this module import, the same one denon's own widgets
// use: that is the PanelMotion instance the rail registers with, so the drawer
// attaches to the rail and takes its frosted surface. Loading PopupPanel.qml by
// URL instead creates a second, unregistered PanelMotion and a floating card.
// BarWidget loads this file with a Loader; without denon.bar the import fails
// and it falls back to Omarchy's PopupCard.
import QtQuick
import "../denon.bar/panels" as Motion

Motion.PopupPanel {
}
