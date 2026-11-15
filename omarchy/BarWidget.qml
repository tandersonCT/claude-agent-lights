import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Commons as Commons
import qs.Ui

// One light per running Claude Code session, so you can walk away from tmux
// and see at a glance which session needs you:
//   red, blinking    waiting on you (permission or a question)
//   green, pulsing   finished, and you haven't been back to it yet
//   yellow, slow blink  working (slower than red, so red still reads as urgent)
// Finished sessions disappear once you visit their tmux pane or click the
// light. Hover a light for the session name, folder and how long it has been
// in that state; click it to jump to its tmux pane and terminal window.
// All of the logic lives in the `agent-lights` helper next to this file (see
// its docstring and tests/test_agent_lights.py); this only draws its output.
// A session running a lean or superpowers plan gets a segmented accent ring
// (one arc per task); running subagents show as a row of tiny dots under it.
// Settings (shell.json entry): "intervalMs" (default 2000), "maxLights" (8).
BarWidget {
  id: root
  moduleName: "tanderson.agent-lights"

  readonly property int intervalMs: Math.max(1000, Number(setting("intervalMs", 2000)) || 2000)
  readonly property int maxLights: Math.max(1, Number(setting("maxLights", 8)) || 8)
  readonly property string helper: String(Qt.resolvedUrl("agent-lights")).replace(/^file:\/\//, "")
  readonly property var colors: ({ waiting: "#f38ba8", done: "#a6e3a1", busy: "#f9e2af" })  // Catppuccin Mocha
  readonly property var labels: ({ waiting: "Waiting on you", done: "Done", busy: "Working" })

  property var lights: []
  property string lastScan: ""
  property double now: Date.now()

  // Shared animation phases: every red light blinks and every green light
  // breathes in step, and a rebuilt dot picks up mid-phase instead of restarting.
  property real blink: 1
  property real breathe: 1
  SequentialAnimation on blink {
    running: root.visible
    loops: Animation.Infinite
    NumberAnimation { to: 0.2; duration: 500; easing.type: Easing.InOutQuad }
    NumberAnimation { to: 1.0; duration: 500; easing.type: Easing.InOutQuad }
  }
  property real work: 1
  SequentialAnimation on work {
    running: root.visible
    loops: Animation.Infinite
    NumberAnimation { to: 0.3; duration: 900; easing.type: Easing.InOutQuad }
    NumberAnimation { to: 1.0; duration: 900; easing.type: Easing.InOutQuad }
  }
  SequentialAnimation on breathe {
    running: root.visible
    loops: Animation.Infinite
    NumberAnimation { to: 0.4; duration: 1200; easing.type: Easing.InOutSine }
    NumberAnimation { to: 1.0; duration: 1200; easing.type: Easing.InOutSine }
  }
  readonly property var shown: lights.slice(0, maxLights)
  readonly property int overflow: Math.max(0, lights.length - maxLights)

  readonly property real dotPx: Math.max(6, Math.round(Style.bar.iconSlot * 0.36))
  readonly property real slot: Math.round(dotPx * 2.2)
  // Plan ring and the subagent row (only on lights that have them).
  readonly property real ringPx: Math.round(dotPx * 2.0)
  readonly property real ringSlot: ringPx + 9
  readonly property color ringColor: Commons.Color.accent  // same as the usage gauge
  readonly property color subagentColor: root.bar ? root.bar.barForeground : Commons.Color.foreground
  readonly property real subDotPx: 2.5
  readonly property real subRowH: 6   // extra slot height when the row shows
  readonly property int maxSubDots: 4

  // Segmented ring: one arc per task (lit = done, half = current, faint = to
  // do); above 12 tasks one continuous arc.
  function paintRing(ctx, size, plan) {
    ctx.reset()
    var c = size / 2, r = root.ringPx / 2
    var lw = 1.5
    var deg = Math.PI / 180, top = -90 * deg
    function arc(from, to, alpha) {
      ctx.beginPath()
      ctx.strokeStyle = Qt.rgba(root.ringColor.r, root.ringColor.g, root.ringColor.b, alpha)
      ctx.lineWidth = lw
      ctx.lineCap = "butt"
      ctx.arc(c, c, r, from, to, false)
      ctx.stroke()
    }
    var alpha = { done: 1, current: 0.55, todo: 0.22 }
    if (plan && plan.states && plan.states.length <= 12) {
      var each = 360 / plan.states.length, gap = plan.states.length > 1 ? 2 : 0
      for (var i = 0; i < plan.states.length; i++)
        arc(top + (i * each + gap / 2) * deg, top + ((i + 1) * each - gap / 2) * deg, alpha[plan.states[i]] || 0.22)
    } else if (plan) {
      arc(0, 2 * Math.PI, 0.22)
      if (plan.done > 0) arc(top, top + 360 * deg * plan.done / plan.total, 1)
    }
  }

  visible: lights.length > 0
  implicitWidth: visible ? (vertical ? barSize : flow.implicitWidth + Style.space(8)) : 0
  implicitHeight: visible ? (vertical ? flow.implicitHeight + Style.space(6) : barSize) : 0

  function ago(ms) {
    var s = Math.max(0, Math.round((root.now - ms) / 1000))
    if (s < 60) return s + "s"
    if (s < 3600) return Math.round(s / 60) + "m"
    return Math.floor(s / 3600) + "h " + Math.round((s % 3600) / 60) + "m"
  }

  function shortCwd(path) {
    var home = Quickshell.env("HOME") || ""
    return home && path.indexOf(home) === 0 ? "~" + path.slice(home.length) : path
  }

  function tooltipFor(l) {
    var subs = l.subagents || []
    return l.name + "  ·  " + shortCwd(l.cwd) + "\n" + labels[l.light] + " · " + ago(l.since)
      + (l.plan ? " · plan " + l.plan.name + " " + l.plan.done + "/" + l.plan.total : "")
      + (l.tmux ? "\ntmux " + l.tmux.split(".")[0] : "")
      + subs.map(function(d) { return "\n↳ " + d }).join("")
  }

  function rescan() {
    if (!scanProc.running) scanProc.running = true
  }

  Process {
    id: scanProc
    command: [root.helper, "scan"]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        root.now = Date.now()
        // Rebuild the dots only when something changed, so an open tooltip
        // isn't torn down by every scan.
        var out = text.trim() || "[]"
        if (out === root.lastScan) return
        root.lastScan = out
        var parsed = []
        try { parsed = JSON.parse(out) } catch (e) { parsed = [] }
        root.lights = Array.isArray(parsed) ? parsed : []
      }
    }
  }

  Process {
    id: jumpProc
    onExited: root.rescan()
  }

  Timer {
    interval: root.intervalMs
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: root.rescan()
  }

  Grid {
    id: flow
    anchors.centerIn: parent
    columns: root.vertical ? 1 : Math.max(1, root.shown.length + (root.overflow > 0 ? 1 : 0))
    spacing: 0

    Repeater {
      model: root.shown

      WidgetButton {
        id: lightButton
        required property var modelData
        bar: root.bar
        hasVisualContent: true
        labelVisible: false
        readonly property var plan: modelData.plan || null
        readonly property var subs: modelData.subagents || []
        readonly property bool ringed: plan !== null
        readonly property real rowH: subs.length > 0 ? root.subRowH : 0
        fixedWidth: root.vertical ? root.barSize : (ringed ? root.ringSlot : root.slot)
        fixedHeight: root.vertical ? (ringed ? root.ringSlot : root.slot) + rowH : root.barSize

        // The light (and its ring) sit above the subagent row, both centred.
        Column {
          anchors.centerIn: parent
          spacing: 1
          scale: lightButton.pressScale

          Item {
            anchors.horizontalCenter: parent.horizontalCenter
            width: lightButton.ringed ? root.ringPx + 8 : root.dotPx
            height: width

            Canvas {
              id: ring
              visible: lightButton.ringed
              anchors.fill: parent
              onPaint: root.paintRing(getContext("2d"), width, lightButton.plan)
              Component.onCompleted: requestPaint()
              Connections {
                target: root
                function onRingColorChanged() { ring.requestPaint() }
              }
            }

            Rectangle {
              id: dot
              anchors.centerIn: parent
              width: root.dotPx
              height: root.dotPx
              radius: width / 2
              color: root.colors[lightButton.modelData.light] || "#f9e2af"
              opacity: lightButton.modelData.light === "waiting" ? root.blink
                : lightButton.modelData.light === "done" ? root.breathe : root.work
            }
          }

          // One tiny dot per running subagent, below the ring (max 4, then "+").
          Row {
            visible: lightButton.subs.length > 0
            anchors.horizontalCenter: parent.horizontalCenter
            spacing: 2
            Repeater {
              model: Math.min(lightButton.subs.length, root.maxSubDots)
              Rectangle {
                width: root.subDotPx
                height: width
                radius: width / 2
                color: root.subagentColor
                opacity: 0.85
              }
            }
            Text {
              visible: lightButton.subs.length > root.maxSubDots
              text: "+"
              color: root.subagentColor
              font.pixelSize: root.subDotPx * 3
              height: root.subDotPx
              verticalAlignment: Text.AlignVCenter
            }
          }
        }
        tooltipText: root.tooltipFor(modelData)
        onPressed: function(b) {
          if (b !== Qt.LeftButton) return
          lightButton.hideOwnTooltip()
          jumpProc.command = [root.helper, "jump", String(modelData.pid)]
          jumpProc.running = true
        }

      }
    }

    OpticalGlyph {
      visible: root.overflow > 0
      width: root.vertical ? root.barSize : root.slot * 1.4
      height: root.vertical ? root.slot : root.barSize
      text: "+" + root.overflow
      fontFamily: root.bar ? root.bar.fontFamily : Style.font.family
      fontSize: Style.font.body * 0.7
      color: root.bar ? root.bar.barForeground : Commons.Color.foreground
    }
  }
}
