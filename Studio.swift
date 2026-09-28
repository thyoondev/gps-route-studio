import SwiftUI
import MapKit
import UniformTypeIdentifiers

struct Waypoint: Identifiable {
    let id = UUID()
    let coordinate: CLLocationCoordinate2D
}

@MainActor final class Studio: ObservableObject {
    @Published var points = [Waypoint(coordinate: .init(latitude: 37.56650, longitude: 126.97800)), Waypoint(coordinate: .init(latitude: 37.56510, longitude: 126.97560)), Waypoint(coordinate: .init(latitude: 37.56710, longitude: 126.97480))] { didSet { routeRevision = UUID(); saveSettings() } }
    private(set) var routeRevision = UUID()
    @Published var speed = 40.0
    @Published var jitter = 1.0
    @Published var pingpong = true { didSet { saveSettings() } }
    @Published var repeats = false { didSet { saveSettings() } }
    @Published var running = false
    @Published var paused = false
    @Published var ready = false
    @Published var stopping = false
    @Published var status = "경유지를 지도에서 클릭하세요"
    @Published var sample: CLLocationCoordinate2D?
    @Published var trail: [CLLocationCoordinate2D] = []
    @Published var sentSpeed: Double?
    @Published var coordinateSpeed: Double?
    @Published var offset = 0.0
    @Published var elapsed = 0.0
    @Published var lap = 0
    @Published var error = ""
    @Published var mode = "대기"
    @Published var target = "iPhone"
    @Published var receivedSpeed: Double?
    @Published var receivedMock: Bool?
    private var process: Process?
    private var input: FileHandle?
    private var outputBuffer = Data()
    private var loading = true
    private var sessionID = UUID()
    var onStopped: (() -> Void)?

    init() {
        if let saved = UserDefaults.standard.dictionary(forKey: "routeSettings") {
            if let value = saved["speed"] as? Double, value.isFinite, (0.1...160).contains(value) { speed = value }
            if let value = saved["jitter"] as? Double, value.isFinite, (0...50).contains(value) { jitter = value }
            target = saved["target"] as? String == "Android" ? "Android" : "iPhone"
            pingpong = saved["pingpong"] as? Bool ?? true
            repeats = saved["repeat"] as? Bool ?? false
            if let values = saved["points"] as? [[Double]], values.count <= 10000,
               values.allSatisfy({ $0.count == 2 && $0[0].isFinite && $0[1].isFinite && abs($0[0]) <= 90 && abs($0[1]) <= 180 }) {
                points = values.map { Waypoint(coordinate: .init(latitude: $0[0], longitude: $0[1])) }
            }
        }
        loading = false
    }
    func saveSettings() {
        guard !loading else { return }
        UserDefaults.standard.set(["points": points.map { [$0.coordinate.latitude, $0.coordinate.longitude] },
                                   "speed": speed, "jitter": jitter, "pingpong": pingpong, "repeat": repeats, "target": target], forKey: "routeSettings")
    }

    func send(_ value: [String: Any]) {
        guard let input, let data = try? JSONSerialization.data(withJSONObject: value) else { return }
        do { try input.write(contentsOf: data + Data([10])) }
        catch { self.error = "명령 전송 실패: \(error.localizedDescription)" }
    }
    func updateSettings() {
        speed = speed.isFinite ? min(160, max(0.1, speed)) : 40
        jitter = jitter.isFinite ? min(50, max(0, jitter)) : 1
        saveSettings(); send(["command": "settings", "speed": speed, "jitter": jitter])
    }
    func stop() { stopping = true; status = "중지 요청 · GPS 복구를 기다리는 중…"; send(["command": "stop"]) }
    func pause() { guard ready && !stopping else { return }; paused.toggle(); send(["command": "pause", "paused": paused]) }
    func start(preview: Bool) {
        guard !running, points.count > 1 else { return }
        saveSettings()
        receivedSpeed = nil; receivedMock = nil
        error = ""; trail = []; sample = nil; sentSpeed = nil; coordinateSpeed = nil; elapsed = 0; offset = 0; lap = 0; paused = false; ready = false; stopping = false
        status = preview ? "미리보기 준비 중…" : "\(target) 전송 준비 중…"
        let session = UUID(); sessionID = session
        let task = Process()
        let inPipe = Pipe(), outPipe = Pipe(), errPipe = Pipe()
        let python = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".local/share/uv/tools/pymobiledevice3/bin/python3")
        guard let worker = Bundle.main.url(forResource: preview ? "worker" : (target == "Android" ? "android_worker" : "motion_worker"), withExtension: "py") else { error = "전송 도우미가 없습니다"; return }
        task.executableURL = python
        task.arguments = ["-B", "-u", worker.path]
        task.standardInput = inPipe; task.standardOutput = outPipe; task.standardError = errPipe
        outputBuffer = Data()
        outPipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            if data.isEmpty { handle.readabilityHandler = nil; return }
            Task { @MainActor in if self?.sessionID == session { self?.consume(data) } }
        }
        errPipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            if data.isEmpty { handle.readabilityHandler = nil; return }
            let text = String(decoding: data, as: UTF8.self)
            Task { @MainActor in if self?.sessionID == session { self?.error = String(text.suffix(2000)) } }
        }
        task.terminationHandler = { [weak self] task in
            let code = task.terminationStatus
            Task { @MainActor in
                guard let self, self.sessionID == session else { return }
                self.running = false; self.process = nil; self.input = nil
                self.ready = false; self.stopping = false; self.paused = false; self.sentSpeed = nil; self.receivedSpeed = nil; self.receivedMock = nil; self.mode = "대기"
                if code != 0 { self.error += "\n작업 종료 코드 \(code). GPS 복구 상태를 확인하세요." }
                self.onStopped?(); self.onStopped = nil
            }
        }
        do {
            try task.run()
            process = task; input = inPipe.fileHandleForWriting; running = true
            mode = preview ? "미리보기" : "\(target) 주행"
            send(["points": points.map { "\($0.coordinate.latitude),\($0.coordinate.longitude)" }, "speed": speed, "jitter": jitter, "pingpong": pingpong, "repeat": repeats, "preview": preview])
        } catch { self.error = error.localizedDescription }
    }
    private func consume(_ data: Data) {
        outputBuffer.append(data)
        while let newline = outputBuffer.firstIndex(of: 10) {
            let line = outputBuffer[..<newline]; outputBuffer.removeSubrange(...newline)
            guard let event = try? JSONSerialization.jsonObject(with: line) as? [String: Any], let kind = event["kind"] as? String else { continue }
            if kind == "sample", let lat = event["lat"] as? Double, let lon = event["lon"] as? Double {
                let coordinate = CLLocationCoordinate2D(latitude: lat, longitude: lon)
                sample = coordinate; trail.append(coordinate)
                if trail.count > 2000 { trail.removeFirst(trail.count - 2000) }
                sentSpeed = event["speed"] as? Double
                coordinateSpeed = event["coordinateSpeed"] as? Double
                receivedSpeed = event["receivedSpeed"] as? Double
                receivedMock = event["receivedMock"] as? Bool
                ready = true
                offset = event["jitter"] as? Double ?? 0
                elapsed = event["elapsed"] as? Double ?? 0
                lap = event["lap"] as? Int ?? 0
            } else if let message = event["message"] as? String {
                if kind == "error" || kind == "restore_failed" { error = message }
                else { status = message }
            }
        }
    }
    func importGPX(_ url: URL) {
        let access = url.startAccessingSecurityScopedResource()
        defer { if access { url.stopAccessingSecurityScopedResource() } }
        do {
            let parser = XMLParser(data: try Data(contentsOf: url))
            let delegate = GPXReader(); parser.delegate = delegate
            guard parser.parse(), delegate.valid, delegate.points.count >= 2, delegate.points.count <= 10000 else { throw CocoaError(.fileReadCorruptFile) }
            points = delegate.points; error = ""
        } catch { self.error = "GPX를 읽지 못했습니다: \(error.localizedDescription)" }
    }
}

final class GPXReader: NSObject, XMLParserDelegate {
    var points: [Waypoint] = []; var valid = true
    func parser(_ parser: XMLParser, didStartElement element: String, namespaceURI: String?, qualifiedName: String?, attributes: [String: String]) {
        guard ["trkpt", "rtept", "wpt"].contains(element.components(separatedBy: ":").last ?? element) else { return }
        guard let lat = Double(attributes["lat"] ?? ""), let lon = Double(attributes["lon"] ?? ""), lat.isFinite, lon.isFinite, abs(lat) <= 90, abs(lon) <= 180 else { valid = false; return }
        points.append(Waypoint(coordinate: .init(latitude: lat, longitude: lon)))
    }
}

private struct WaypointList: View, Equatable {
    let points: [Waypoint]
    let revision: UUID
    let remove: (UUID) -> Void
    static func == (lhs: Self, rhs: Self) -> Bool { lhs.revision == rhs.revision }
    var body: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 6) {
                ForEach(Array(points.enumerated()), id: \.element.id) { index, point in
                    HStack {
                        Text("\(index + 1). " + String(format: "%.5f, %.5f", point.coordinate.latitude, point.coordinate.longitude)).font(.caption.monospaced())
                        Spacer()
                        Button { remove(point.id) } label: { Image(systemName: "minus.circle") }.buttonStyle(.plain)
                    }
                }
            }
        }.frame(maxHeight: 140)
    }
}

struct ContentView: View {
    @ObservedObject var model: Studio
    @ObservedObject var diagnostics: ReceiverDiagnostics
    @State private var fitRequest = UUID()
    @State private var importing = false
    @State private var coordinate = ""
    var body: some View {
        HStack(spacing: 0) {
            VStack(alignment: .leading, spacing: 18) {
                Text("GPS Route Studio").font(.title2.bold())
                Picker("전송 기기", selection: $model.target) { Text("iPhone").tag("iPhone"); Text("Android").tag("Android") }.pickerStyle(.segmented).disabled(model.running).onChange(of: model.target) { model.saveSettings() }
                Divider()
                HStack { Text("설정 속도"); Spacer(); TextField("속도", value: $model.speed, format: .number.precision(.fractionLength(1))).textFieldStyle(.roundedBorder).frame(width: 65); Text("km/h") }
                Slider(value: $model.speed, in: 0.1...160, step: 0.1).onChange(of: model.speed) { model.updateSettings() }
                HStack { Text("위치 지터 · 표준편차"); Spacer(); TextField("지터", value: $model.jitter, format: .number.precision(.fractionLength(1))).textFieldStyle(.roundedBorder).frame(width: 65); Text("m") }
                Slider(value: $model.jitter, in: 0...50, step: 0.5).onChange(of: model.jitter) { model.updateSettings() }
                Text("속도·지터는 재생 중에도 바꿀 수 있습니다. 지터는 좌표 오차이며, 속도는 설정값으로 전송합니다.").font(.caption).foregroundStyle(.secondary)
                Toggle("왕복", isOn: $model.pingpong).disabled(model.running)
                Toggle("계속 반복", isOn: $model.repeats).disabled(model.running)
                VStack(alignment: .leading, spacing: 8) {
                    HStack { Text("경유지 \(model.points.count)개").bold(); Spacer(); Button("GPX 열기") { importing = true } }
                    HStack {
                        TextField("위도,경도", text: $coordinate)
                        Button("추가") { addCoordinate() }
                    }
                    WaypointList(points: model.points, revision: model.routeRevision) { id in
                        model.points.removeAll { $0.id == id }
                    }.equatable()
                    HStack {
                        Button("모두 지우기") { model.points = [] }
                        Button("경로 보기") { fitRequest = UUID() }
                    }
                    Text("지도 클릭으로 경유지를 추가합니다. 경유지 사이는 직선으로 연결됩니다.").font(.caption).foregroundStyle(.secondary)
                }.disabled(model.running)
                Spacer(minLength: 0)
                if !model.error.isEmpty { Text(model.error).font(.caption).foregroundStyle(.red).textSelection(.enabled).lineLimit(7) }
                if model.running {
                    HStack { Button(model.paused ? "계속" : "일시정지") { model.pause() }.disabled(!model.ready || model.stopping); Button("중지 · GPS 복구") { model.stop() }.tint(.red).disabled(model.stopping) }
                } else {
                    HStack { Button("미리보기") { model.start(preview: true) }; Button("\(model.target) 주행 시작") { model.start(preview: false) }.buttonStyle(.borderedProminent) }.disabled(model.points.count < 2)
                }
                Text("USB 연결 후 시작하세요. 중지·종료하면 실제 GPS로 복구합니다.").font(.caption).foregroundStyle(.secondary)
            }.padding(24).frame(width: 340)
            VStack(spacing: 0) {
                ZStack(alignment: .topLeading) {
                    RouteMap(points: model.points, routeRevision: model.routeRevision,
                             fitRequest: fitRequest, sample: model.sample, trail: model.trail,
                             running: model.running) { coordinate in
                        model.points.append(Waypoint(coordinate: coordinate))
                    }
                    Text("숫자 묶음을 클릭하면 확대 · 모든 경유지 유지")
                        .font(.caption).padding(9).background(.regularMaterial, in: RoundedRectangle(cornerRadius: 8))
                        .padding(12).allowsHitTesting(false)
                }
                VStack(alignment: .leading, spacing: 12) {
                    HStack(spacing: 30) {
                        metric(model.mode == "미리보기" ? "좌표 계산 속도" : "\(model.target) 적용 속도", model.sentSpeed.map { String(format: "%.1f km/h", $0) } ?? "—")
                        metric("현재 위치 오차", String(format: "%.1f m", model.offset))
                        metric("경과 / 회차", "\(Int(model.elapsed))초 / \(model.lap)")
                        metric("모드", model.mode)
                    }
                    Text(model.status).font(.callout)
                    if model.target == "Android" {
                        Text(androidReception).font(.callout)
                    }
                    Text(model.mode == "미리보기" ? "미리보기는 기기에 전송하지 않습니다." : "전송 도우미가 적용을 확인한 값입니다. 다른 앱의 주행 인정 여부를 뜻하지 않습니다.").font(.caption).foregroundStyle(.secondary)
                    Divider()
                    if model.target == "iPhone" || diagnostics.running { DiagnosticsPanel(model: diagnostics) }
                }.padding(20).frame(maxWidth: .infinity, alignment: .leading).background(.regularMaterial)
            }
        }.frame(minWidth: 1040, minHeight: 730)
        .fileImporter(isPresented: $importing, allowedContentTypes: [.xml, .data]) { result in
            if case .success(let url) = result { model.importGPX(url); fitRequest = UUID() }
            if case .failure(let error) = result { model.error = error.localizedDescription }
        }
    }
    private var androidReception: String {
        let speed = model.receivedSpeed.map { String(format: "%.1f km/h", $0) } ?? "—"
        let mock = model.receivedMock.map { $0 ? "있음" : "없음" } ?? "수신 대기"
        return "도우미 수신 속도: \(speed) · 모의 위치 표시: \(mock)"
    }
    func metric(_ title: String, _ value: String) -> some View { VStack(alignment: .leading) { Text(title).font(.caption).foregroundStyle(.secondary); Text(value).font(.title3.monospacedDigit().bold()) } }
    func addCoordinate() {
        let parts = coordinate.split(separator: ",").map { $0.trimmingCharacters(in: .whitespaces) }
        guard parts.count == 2, let lat = Double(parts[0]), let lon = Double(parts[1]), lat.isFinite, lon.isFinite, abs(lat) <= 90, abs(lon) <= 180 else { model.error = "위도,경도를 확인하세요."; return }
        model.points.append(Waypoint(coordinate: .init(latitude: lat, longitude: lon))); coordinate = ""; model.error = ""
    }
}

@MainActor final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate {
    let model = Studio()
    let diagnostics = ReceiverDiagnostics()
    var window: NSWindow!
    func applicationDidFinishLaunching(_ notification: Notification) {
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1140, height: 780), styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "GPS Route Studio"; window.contentView = NSHostingView(rootView: ContentView(model: model, diagnostics: diagnostics)); window.delegate = self
        window.center(); window.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true)
        let main = NSMenu(); let item = NSMenuItem(); let menu = NSMenu()
        menu.addItem(withTitle: "GPS Route Studio 종료", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        item.submenu = menu; main.addItem(item); NSApp.mainMenu = main
    }
    func windowShouldClose(_ sender: NSWindow) -> Bool { NSApp.terminate(nil); return false }
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard model.running || diagnostics.running else { return .terminateNow }
        let finish: () -> Void = { [weak self] in
            guard let self, !self.model.running, !self.diagnostics.running else { return }
            sender.reply(toApplicationShouldTerminate: self.model.error.isEmpty)
        }
        model.onStopped = finish; diagnostics.onStopped = finish
        if model.running { model.stop() }
        if diagnostics.running { diagnostics.stop() }
        // Failed restoration leaves the window open with its error.
        return .terminateLater
    }
}

@main struct Launcher {
    @MainActor static func main() {
        let app = NSApplication.shared; let delegate = AppDelegate(); app.delegate = delegate
        app.setActivationPolicy(.regular); app.run()
    }
}
