import Cocoa
// claude-local-first usage meter: shows Claude usage in the menu bar, plus suggestions for moving work to cheaper tiers.
// It only READS two small JSON files (default ~/.local-first/usage.json, or $CLAUDE_USAGE_FILE, and suggestions.json next to
// it). It makes no network calls, uses no private Claude API and uses no Claude usage. Something else writes the files:
// usage_from_transcripts.py (estimate from your local logs) and `localfirst refresh` (suggestions).
// Buttons in the menu run the `localfirst` command (accept / decline / never) and nothing else; the meter never moves
// work by itself. "Open cheap mode" opens the claude-cheap launcher in a new Terminal window.
//
// File format (all keys optional except five_hour):
//   {"five_hour": 42, "five_hour_resets_in": "2h 10m", "weekly": 30,
//    "weekly_model": 12, "weekly_model_label": "Opus", "extra_spent": 5, "extra_limit": 50,
//    "zone": "GREEN", "updated": "2026-01-01T12:00:00Z", "estimate": true}
// zone is GREEN / AMBER / RED; if missing the meter works it out from the percentages.
// Optional CPU readout: checked once a minute while calm; above CPU_HOT it updates every 5 s, shows in the menu bar
// with the busiest process, and drops back after 2 min below CPU_COOL. Local only (host_statistics + ps).
let CPU_HOT = 70.0, CPU_COOL = 60.0
let CALM_EVERY = 60.0, HOT_EVERY = 5.0, COOL_DOWN = 120.0

class App: NSObject, NSApplicationDelegate {
  let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
  let path: String = {
    if let p = ProcessInfo.processInfo.environment["CLAUDE_USAGE_FILE"], !p.isEmpty { return (p as NSString).expandingTildeInPath }
    let home = ProcessInfo.processInfo.environment["LOCAL_FIRST_HOME"].map { ($0 as NSString).expandingTildeInPath }
      ?? (NSHomeDirectory() as NSString).appendingPathComponent(".local-first")
    return (home as NSString).appendingPathComponent("usage.json")
  }()
  var suggestionsPath: String { ((path as NSString).deletingLastPathComponent as NSString).appendingPathComponent("suggestions.json") }
  var localfirstBin: String {
    if let p = ProcessInfo.processInfo.environment["LOCALFIRST_BIN"], !p.isEmpty { return (p as NSString).expandingTildeInPath }
    return (NSHomeDirectory() as NSString).appendingPathComponent(".local/bin/localfirst")
  }
  var cheapBin: String {
    if let p = ProcessInfo.processInfo.environment["CHEAP_BIN"], !p.isEmpty { return (p as NSString).expandingTildeInPath }
    return (NSHomeDirectory() as NSString).appendingPathComponent(".local/bin/claude-cheap")
  }
  struct Suggestion { let id: String; let text: String }
  func loadSuggestions() -> [Suggestion] {
    guard let data = FileManager.default.contents(atPath: suggestionsPath),
          let d = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
          let arr = d["suggestions"] as? [[String: Any]] else { return [] }
    return arr.compactMap { a in
      guard let id = a["id"] as? String, let text = a["text"] as? String else { return nil }
      return Suggestion(id: id, text: text)
    }
  }
  // Menu actions. The only things the meter ever runs: the localfirst command, and the cheap-mode launcher in Terminal.
  @objc func answer(_ sender: NSMenuItem) {
    guard let a = sender.representedObject as? [String], a.count == 2 else { return }
    let verb = a[0], id = a[1]
    var args = [verb, id]
    if verb == "never" { args = ["decline", id, "--never"] }
    let p = Process(); p.executableURL = URL(fileURLWithPath: localfirstBin); p.arguments = args
    p.standardOutput = FileHandle.nullDevice; p.standardError = FileHandle.nullDevice
    try? p.run(); p.waitUntilExit()
    refresh()
  }
  @objc func openCheapMode() {
    let script = "tell application \"Terminal\"\nactivate\ndo script \"\u{27}\(cheapBin.replacingOccurrences(of: "\"", with: "").replacingOccurrences(of: "\u{27}", with: ""))\u{27}\"\nend tell"
    var err: NSDictionary?
    NSAppleScript(source: script)?.executeAndReturnError(&err)
  }
  func addSuggestions(_ menu: NSMenu, _ list: [Suggestion]) {
    guard !list.isEmpty else { return }
    menu.addItem(.separator())
    menu.addItem(withTitle: "Suggestions (\(list.count)) - nothing moves unless you accept", action: nil, keyEquivalent: "")
    for s in list {
      let item = NSMenuItem(title: s.text, action: nil, keyEquivalent: "")
      let sub = NSMenu()
      for (label, verb) in [("Accept", "accept"), ("Not now", "decline"), ("Never suggest this again", "never")] {
        let m = NSMenuItem(title: label, action: #selector(answer(_:)), keyEquivalent: "")
        m.target = self; m.representedObject = [verb, s.id]; sub.addItem(m)
      }
      item.submenu = sub
      menu.addItem(item)
    }
  }
  var timer: Timer?
  var cpuTimer: Timer?
  var lastTicks: [UInt32]?
  var cpu = -1.0
  var hot = false
  var coolSince: Date?
  var topProc = ""
  func applicationDidFinishLaunching(_ n: Notification) {
    _ = sampleCPU()
    refresh()
    timer = Timer.scheduledTimer(withTimeInterval: 20, repeats: true) { _ in self.refresh() }
    scheduleCPU(CALM_EVERY)
    Timer.scheduledTimer(withTimeInterval: 2, repeats: false) { _ in self.cpuTick() }   // first real reading
  }
  func scheduleCPU(_ every: Double) {
    cpuTimer?.invalidate()
    cpuTimer = Timer.scheduledTimer(withTimeInterval: every, repeats: true) { _ in self.cpuTick() }
    cpuTimer?.tolerance = every * 0.2
  }
  // Whole-Mac CPU % since the previous sample (all cores, 0-100).
  func sampleCPU() -> Double {
    var info = host_cpu_load_info()
    var count = mach_msg_type_number_t(MemoryLayout<host_cpu_load_info>.size / MemoryLayout<integer_t>.size)
    let r = withUnsafeMutablePointer(to: &info) {
      $0.withMemoryRebound(to: integer_t.self, capacity: Int(count)) { host_statistics(mach_host_self(), HOST_CPU_LOAD_INFO, $0, &count) }
    }
    guard r == KERN_SUCCESS else { return -1 }
    let t = [info.cpu_ticks.0, info.cpu_ticks.1, info.cpu_ticks.2, info.cpu_ticks.3] // user, system, idle, nice
    defer { lastTicks = t }
    guard let p = lastTicks else { return -1 }
    let d = (0..<4).map { Double(t[$0] &- p[$0]) }
    let total = d.reduce(0, +)
    return total > 0 ? (total - d[2]) / total * 100 : -1
  }
  func busiestProcess() -> String {
    let p = Process(); p.executableURL = URL(fileURLWithPath: "/bin/ps")
    p.arguments = ["-Aceo", "pcpu=,comm=", "-r"]
    let pipe = Pipe(); p.standardOutput = pipe; p.standardError = FileHandle.nullDevice
    guard (try? p.run()) != nil else { return "" }
    let out = String(data: pipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
    p.waitUntilExit()
    guard let line = out.split(separator: "\n").first else { return "" }
    let parts = line.trimmingCharacters(in: .whitespaces).split(separator: " ", maxSplits: 1)
    guard parts.count == 2 else { return "" }
    return "\(parts[1]) (\(parts[0])% of one core)"
  }
  func cpuTick() {
    let v = sampleCPU()
    if v < 0 { return }
    cpu = v
    if !hot && cpu >= CPU_HOT {
      hot = true; coolSince = nil; scheduleCPU(HOT_EVERY)
    } else if hot {
      if cpu < CPU_COOL {
        if coolSince == nil { coolSince = Date() }
        if Date().timeIntervalSince(coolSince!) >= COOL_DOWN { hot = false; coolSince = nil; topProc = ""; scheduleCPU(CALM_EVERY) }
      } else { coolSince = nil }
    }
    if hot { topProc = busiestProcess() }
    refresh()
  }
  func cpuLines() -> [String] {
    guard cpu >= 0 else { return ["CPU: measuring…"] }
    var l = [String(format: "CPU: %.0f%%   (%@)", cpu, hot ? "busy, updating every 5 s" : "checked every minute")]
    if hot && !topProc.isEmpty { l.append("Busiest: \(topProc)") }
    return l
  }
  func cpuTitle() -> NSAttributedString? {
    guard hot, cpu >= 0 else { return nil }
    let c: NSColor = cpu >= 90 ? .systemRed : .systemOrange
    return NSAttributedString(string: String(format: "  CPU %.0f%%", cpu),
      attributes: [.foregroundColor: c, .font: NSFont.monospacedDigitSystemFont(ofSize: 12, weight: .medium)])
  }
  func num(_ d: [String: Any], _ k: String) -> Double? { (d[k] as? NSNumber)?.doubleValue }
  func refresh() {
    let menu = NSMenu()
    guard let data = FileManager.default.contents(atPath: path),
          let d = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else {
      let t = NSMutableAttributedString(string: "Claude –")
      if let c = cpuTitle() { t.append(c) }
      item.button?.attributedTitle = t
      menu.addItem(withTitle: "No usage data yet", action: nil, keyEquivalent: "")
      menu.addItem(withTitle: "Looking for: \(path)", action: nil, keyEquivalent: "")
      for line in cpuLines() { menu.addItem(withTitle: line, action: nil, keyEquivalent: "") }
      addQuit(menu); item.menu = menu; return
    }
    let five = num(d, "five_hour") ?? 0, wk = num(d, "weekly"), model = num(d, "weekly_model")
    let label = (d["weekly_model_label"] as? String) ?? "one model"
    let spent = num(d, "extra_spent"), lim = num(d, "extra_limit")
    var zone = (d["zone"] as? String ?? "").uppercased()
    if zone.isEmpty {
      let worst = max(five, wk ?? 0, model ?? 0)
      zone = worst >= 100 ? "RED" : (worst >= 80 ? "AMBER" : "GREEN")
    }
    let colour: NSColor = zone == "UNCALIBRATED" ? .secondaryLabelColor : (zone == "RED" ? .systemRed : (zone == "AMBER" ? .systemOrange : .systemGreen))
    var stale = false
    var updatedText = "?"
    if let u = d["updated"] as? String, let t = ISO8601DateFormatter().date(from: u) {
      stale = Date().timeIntervalSince(t) > 45 * 60
      let f = DateFormatter(); f.dateFormat = "h:mm a"; updatedText = f.string(from: t)
    }
    let isEstimate = (d["estimate"] as? Bool) ?? false
    let title = NSMutableAttributedString(string: "● ", attributes: [.foregroundColor: stale ? NSColor.secondaryLabelColor : colour])
    var short = String(format: "5h %.0f%%", five)
    if let w = wk { short += String(format: "  wk %.0f%%", w) }
    title.append(NSAttributedString(string: short + (isEstimate ? " ~" : ""),
      attributes: [.font: NSFont.monospacedDigitSystemFont(ofSize: 12, weight: .medium)]))
    let sugg = loadSuggestions()
    if !sugg.isEmpty { title.append(NSAttributedString(string: " •", attributes: [.foregroundColor: NSColor.systemBlue])) }   // suggestions waiting
    if let c = cpuTitle() { title.append(c) }
    item.button?.attributedTitle = title
    var lines = ["Zone: \(zone)\(stale ? "  (data is old)" : "")",
      String(format: "5-hour window: %.0f%%   resets in %@", five, (d["five_hour_resets_in"] as? String) ?? "?")]
    if let w = wk { lines.append(String(format: "Weekly, all models: %.0f%%", w)) }
    if let m = model { lines.append(String(format: "Weekly, %@: %.0f%%", label, m)) }
    if let s = spent, let l = lim { lines.append(String(format: "Extra usage: $%.0f of $%.0f", s, l)) }
    if isEstimate { lines.append("Numbers are estimates from local transcripts") }
    lines.append("Updated \(updatedText)")
    for line in lines { menu.addItem(withTitle: line, action: nil, keyEquivalent: "") }
    menu.addItem(.separator())
    for line in cpuLines() { menu.addItem(withTitle: line, action: nil, keyEquivalent: "") }
    addSuggestions(menu, sugg)
    addQuit(menu); item.menu = menu
  }
  func addQuit(_ m: NSMenu) {
    m.addItem(.separator())
    let cheap = NSMenuItem(title: "Open cheap mode (Terminal)", action: #selector(openCheapMode), keyEquivalent: "")
    cheap.target = self
    m.addItem(cheap)
    m.addItem(withTitle: "Quit usage meter", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
  }
}
let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let delegate = App()
app.delegate = delegate
app.run()
