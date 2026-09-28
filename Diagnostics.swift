import SwiftUI

@MainActor final class ReceiverDiagnostics: ObservableObject {
    @Published var running = false
    @Published var stopping = false
    @Published var active = false
    @Published var readings: [String: String] = [:]
    @Published var observation = ""
    @Published var status = "iPhone이 받는 위치와 실제 움직임을 비교합니다."
    @Published var lastReceived: Date?
    private var process: Process?
    private var buffer = Data()
    private var session = UUID()
    var onStopped: (() -> Void)?

    func start() {
        guard !running else { return }
        guard let worker = Bundle.main.url(forResource: "diagnostic_worker", withExtension: "py") else {
            status = "진단 도우미 파일이 없습니다."; return
        }
        let task = Process(), pipe = Pipe(), errors = Pipe()
        let token = UUID(); session = token
        task.executableURL = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".local/share/uv/tools/pymobiledevice3/bin/python3")
        task.arguments = ["-B", "-u", worker.path]
        task.standardOutput = pipe; task.standardError = errors
        buffer = Data(); readings = [:]; lastReceived = nil; observation = ""; active = false
        status = "진단 준비 중…"
        pipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            if data.isEmpty { handle.readabilityHandler = nil; return }
            Task { @MainActor in
                guard self?.session == token else { return }
                self?.consume(data)
            }
        }
        errors.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            if data.isEmpty { handle.readabilityHandler = nil; return }
            Task { @MainActor in
                guard self?.session == token else { return }
                self?.status = String(String(decoding: data, as: UTF8.self).suffix(1200))
            }
        }
        task.terminationHandler = { [weak self] task in
            let code = task.terminationStatus
            Task { @MainActor in
                guard let self, self.session == token else { return }
                let requested = self.stopping
                self.running = false; self.stopping = false; self.active = false; self.process = nil
                if requested { self.status = "수신 진단 종료" }
                else if code != 0 { self.status += " · 진단을 완료하지 못했습니다." }
                self.onStopped?(); self.onStopped = nil
            }
        }
        do { try task.run(); process = task; running = true }
        catch { status = "진단 시작 실패: \(error.localizedDescription)" }
    }
    func stop() {
        guard let process, running, !stopping else { return }
        stopping = true; status = "진단 종료 중…"; process.terminate()
    }
    private func consume(_ data: Data) {
        buffer.append(data)
        guard buffer.count < 131072 else { status = "진단 응답이 너무 큽니다."; stop(); return }
        while let newline = buffer.firstIndex(of: 10) {
            let line = buffer[..<newline]; buffer.removeSubrange(...newline)
            guard let event = try? JSONSerialization.jsonObject(with: line) as? [String: Any] else { continue }
            if event["kind"] as? String == "diagnostic", let values = event["readings"] as? [String: String] {
                readings = values; active = event["active"] as? Bool == true
                observation = event["observation"] as? String ?? ""
                lastReceived = Date(); status = active ? "iPhone 수신값 확인 중" : "iPhone 진단 일시 중지"
            } else if let message = event["message"] as? String { status = message }
        }
    }
}

struct DiagnosticsPanel: View {
    @ObservedObject var model: ReceiverDiagnostics
    @State private var expanded = false
    var body: some View {
        DisclosureGroup(isExpanded: $expanded) {
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    Button(model.running ? "진단 중지" : "iPhone 수신 진단 시작") {
                        if model.running { model.stop() } else { model.start() }
                    }.disabled(model.stopping)
                    Text(model.status).font(.caption).lineLimit(2).textSelection(.enabled)
                }
                if let received = model.lastReceived {
                    TimelineView(.periodic(from: .now, by: 1)) { context in
                        let fresh = model.running && model.active && context.date.timeIntervalSince(received) < 5
                        VStack(alignment: .leading, spacing: 6) {
                            if !fresh { Text("현재 측정값이 아닙니다 · 마지막 수신 결과").foregroundStyle(.orange) }
                            HStack(alignment: .top, spacing: 22) {
                                value("수신 속도", "speed")
                                value("가상 위치 표시", "source")
                                value("움직임 분류", "activity")
                            }
                            HStack(spacing: 22) {
                                value("중력 제외 가속도", "acceleration")
                                value("회전 속도", "rotation")
                            }
                        }.font(.caption).opacity(fresh ? 1 : 0.6)
                    }
                    Text(model.observation).font(.caption)
                }
                Text("iPhone에 진단 앱이 열립니다. 다른 앱으로 전환하면 측정이 멈춥니다. 가속도가 작아도 이동 중일 수 있으며, 이 결과는 대상 앱의 주행 인정 여부를 뜻하지 않습니다.")
                    .font(.caption).foregroundStyle(.secondary)
            }.padding(.top, 8)
        } label: { Text("iPhone 위치·움직임 진단").font(.callout.bold()) }
    }
    private func value(_ title: String, _ key: String) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(title).foregroundStyle(.secondary)
            Text(model.readings[key] ?? "—").monospacedDigit().textSelection(.enabled)
        }
    }
}
