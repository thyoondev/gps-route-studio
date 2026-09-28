import SwiftUI
import CoreLocation
import MapKit
import CoreMotion

@MainActor final class Receiver: NSObject, ObservableObject, @preconcurrency CLLocationManagerDelegate {
    private let manager = CLLocationManager()
    private let motion = CMMotionManager()
    private let activityManager = CMMotionActivityManager()
    private var timer: Timer?
    private var motionReceivedAt: Date?
    private var activity: CMMotionActivity?
    @Published var acceleration: Double?
    @Published var rotation: Double?
    @Published var motionStatus = "측정 전"
    @Published var readings: [String: String] = [:]
    @Published var observation = "측정 전"
    @Published var suspended = false
    @Published var location: CLLocation?
    @Published var derivedSpeed: Double?
    @Published var status = "시작을 누르면 이 앱이 받는 GPS 값을 표시합니다."
    @Published var active = false
    @Published var trail: [CLLocationCoordinate2D] = []
    override init() {
        super.init(); manager.delegate = self
        manager.desiredAccuracy = kCLLocationAccuracyBest
        manager.distanceFilter = kCLDistanceFilterNone
    }
    func start() {
        guard !active else { return }
        active = true; location = nil; derivedSpeed = nil; trail = []
        suspended = false
        manager.requestWhenInUseAuthorization()
        applyAuthorization()
        startMotion()
        timer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.publishDiagnostics() }
        }
        publishDiagnostics()
    }
    func stop() {
        active = false; manager.stopUpdatingLocation(); stopMotion()
        timer?.invalidate(); timer = nil
        status = "수신 중지 · 마지막 수신값"
        publishDiagnostics()
    }
    func setBackground(_ background: Bool) {
        guard active, suspended != background else { return }
        suspended = background
        if background {
            manager.stopUpdatingLocation(); stopMotion()
            status = "진단 앱이 백그라운드에 있어 측정을 멈췄습니다."
        } else { applyAuthorization(); startMotion() }
        publishDiagnostics()
    }
    private func startMotion() {
        acceleration = nil; rotation = nil; activity = nil; motionReceivedAt = nil
        motionStatus = motion.isDeviceMotionAvailable ? "센서 응답 대기" : "움직임 센서 사용 불가"
        if motion.isDeviceMotionAvailable {
            motion.deviceMotionUpdateInterval = 0.2
            motion.startDeviceMotionUpdates(to: .main) { [weak self] value, error in
                Task { @MainActor in
                    guard let self, self.active, !self.suspended else { return }
                    if let error { self.motionStatus = "센서 오류: \(error.localizedDescription)"; return }
                    guard let value else { return }
                    let a = value.userAcceleration, r = value.rotationRate
                    self.acceleration = sqrt(a.x*a.x + a.y*a.y + a.z*a.z)
                    self.rotation = sqrt(r.x*r.x + r.y*r.y + r.z*r.z)
                    self.motionReceivedAt = Date(); self.motionStatus = "센서 수신 중"
                }
            }
        }
        if CMMotionActivityManager.isActivityAvailable() {
            activityManager.startActivityUpdates(to: .main) { [weak self] value in
                Task { @MainActor in
                    guard let self, self.active, !self.suspended else { return }
                    self.activity = value
                }
            }
        }
    }
    private func stopMotion() {
        motion.stopDeviceMotionUpdates(); activityManager.stopActivityUpdates()
        acceleration = nil; rotation = nil; activity = nil; motionReceivedAt = nil
        motionStatus = "측정 중지"
    }
    private var activityText: String {
        guard CMMotionActivityManager.isActivityAvailable() else { return "이 기기에서 사용 불가" }
        switch CMMotionActivityManager.authorizationStatus() {
        case .denied: return "동작 및 피트니스 권한 거부"
        case .restricted: return "동작 및 피트니스 권한 제한"
        case .notDetermined: return "동작 및 피트니스 권한 응답 대기"
        case .authorized: break
        @unknown default: return "권한 상태 알 수 없음"
        }
        guard let activity else { return "움직임 분류 대기" }
        var types: [String] = []
        if activity.automotive { types.append("차량 이동") }
        if activity.stationary { types.append("정지") }
        if activity.walking { types.append("걷기") }
        if activity.running { types.append("달리기") }
        if activity.cycling { types.append("자전거") }
        if activity.unknown || types.isEmpty { types.append("분류 불명") }
        let confidence: String
        switch activity.confidence {
        case .low: confidence = "낮음"
        case .medium: confidence = "중간"
        case .high: confidence = "높음"
        @unknown default: confidence = "알 수 없음"
        }
        return types.joined(separator: " · ") + " (신뢰도 \(confidence))"
    }
    private func publishDiagnostics() {
        let locationFresh = active && !suspended && location.map { abs($0.timestamp.timeIntervalSinceNow) < 5 } == true
        let motionFresh = active && !suspended && motionReceivedAt.map { abs($0.timeIntervalSinceNow) < 2 } == true
        let source = locationFresh ? location?.sourceInformation : nil
        let speed = locationFresh ? location?.speed : nil
        let sourceText = source.map { $0.isSimulatedBySoftware ? "소프트웨어 시뮬레이션" : "소프트웨어 표시 없음" } ?? "확인 불가"
        readings = [
            "speed": speed.map { $0 < 0 ? "유효하지 않은 속도" : String(format: "%.1f km/h", $0 * 3.6) } ?? "새 위치 수신 대기",
            "source": sourceText,
            "accessory": source.map { $0.isProducedByAccessory ? "외부 장치 표시 있음" : "외부 장치 표시 없음" } ?? "확인 불가",
            "activity": active && !suspended ? activityText : "측정 중지",
            "acceleration": motionFresh ? acceleration.map { String(format: "%.4f g", $0) } ?? "—" : "새 센서값 수신 대기",
            "rotation": motionFresh ? rotation.map { String(format: "%.4f rad/s", $0) } ?? "—" : "새 센서값 수신 대기",
            "sensor": motionStatus,
            "locationAge": location.map { String(format: "%.0f초 전", max(0, -$0.timestamp.timeIntervalSinceNow)) } ?? "수신 없음"
        ]
        if !active || suspended { observation = "측정 중지 · 진단 앱을 앞에 열고 수신을 시작하세요." }
        else if !locationFresh { observation = "최신 위치를 기다리는 중입니다. 위치 권한과 수신 상태를 확인하세요." }
        else if source?.isSimulatedBySoftware == true { observation = "이 진단 앱에는 가상 위치로 전달됩니다. 다른 앱이 이 정보를 검사하는지는 확인할 수 없습니다." }
        else { observation = "수신값만으로 대상 앱의 주행 인정 여부를 판단할 수 없습니다." }
        let snapshot: [String: Any] = ["version": 1, "active": active && !suspended, "readings": readings, "observation": observation]
        if ProcessInfo.processInfo.arguments.contains("--diagnostics"),
           let data = try? JSONSerialization.data(withJSONObject: snapshot),
           let json = String(data: data, encoding: .utf8) {
            print("GPS_DIAGNOSTIC \(json)"); fflush(stdout)
        }
    }
    func applyAuthorization() {
        guard active, !suspended else { return }
        switch manager.authorizationStatus {
        case .authorizedAlways, .authorizedWhenInUse:
            manager.startUpdatingLocation(); status = "수신 대기 중"
        case .denied, .restricted:
            status = "설정에서 GPS Receiver의 위치 권한을 허용하세요."
        default: status = "위치 권한을 허용해 주세요."
        }
    }
    func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) { applyAuthorization() }
    func locationManager(_ manager: CLLocationManager, didUpdateLocations locations: [CLLocation]) {
        guard active, !suspended else { return }
        for fix in locations {
            guard fix.horizontalAccuracy >= 0, abs(fix.timestamp.timeIntervalSinceNow) < 10,
                  location == nil || fix.timestamp > location!.timestamp else { continue }
            if let previous = location {
                let interval = fix.timestamp.timeIntervalSince(previous.timestamp)
                derivedSpeed = interval > 0 && interval < 10 ? fix.distance(from: previous) / interval * 3.6 : nil
            } else { derivedSpeed = nil }
            location = fix; trail.append(fix.coordinate)
            if trail.count > 2000 { trail.removeFirst(trail.count - 2000) }
            status = "Core Location 수신 중"
        }
    }
    func locationManager(_ manager: CLLocationManager, didFailWithError error: Error) { status = "수신 오류: \(error.localizedDescription)" }
}

struct ReceiverView: View {
    @Environment(\.scenePhase) private var scenePhase
    @StateObject var model = Receiver()
    @State private var camera: MapCameraPosition = .automatic
    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    Map(position: $camera) {
                        MapPolyline(coordinates: model.trail).stroke(.orange, lineWidth: 3)
                        if let fix = model.location { Marker("실제 수신 좌표", coordinate: fix.coordinate) }
                    }.frame(height: 260).clipShape(RoundedRectangle(cornerRadius: 18))
                    Text(model.status).font(.headline)
                    Text("위치·움직임 진단").font(.title3.bold())
                    row("가상 위치 표시", model.readings["source"] ?? "—")
                    row("움직임 분류", model.readings["activity"] ?? "—")
                    row("중력 제외 가속도", model.readings["acceleration"] ?? "—")
                    row("회전 속도", model.readings["rotation"] ?? "—")
                    Text(model.observation).font(.callout)
                    row("iOS 수신 속도", rawSpeed)
                    row("좌표 변화로 계산한 속도", model.derivedSpeed.map { String(format: "%.1f km/h", $0) } ?? "—")
                    if let fix = model.location {
                        row("원시 speed 값", String(format: "%.3f m/s", fix.speed))
                        row("수평 정확도", String(format: "%.1f m", fix.horizontalAccuracy))
                        row("좌표", String(format: "%.6f, %.6f", fix.coordinate.latitude, fix.coordinate.longitude))
                        row("시뮬레이션 출처", fix.sourceInformation.map { $0.isSimulatedBySoftware ? "소프트웨어" : "소프트웨어 표시 없음" } ?? "정보 없음")
                        TimelineView(.periodic(from: .now, by: 1)) { context in
                            let age = max(0, context.date.timeIntervalSince(fix.timestamp))
                            row("마지막 수신", String(format: "%.0f초 전%@", age, age > 5 ? " · 오래된 값" : ""))
                        }
                    }
                    HStack {
                        Button(model.active ? "수신 중지" : "수신 시작") { if model.active { model.stop() } else { model.start() } }.buttonStyle(.borderedProminent)
                        Button("위치 보기") { camera = .automatic }.buttonStyle(.bordered)
                    }
                    Text("iOS 속도가 음수이면 유효하지 않은 값입니다. 좌표로 계산한 속도는 별도 추정값이며, 다른 앱이 받는 속도를 보장하지 않습니다.").font(.footnote).foregroundStyle(.secondary)
                    Text("가속도가 작아도 일정한 속도로 이동 중일 수 있습니다. 움직임 분류도 추정값이며, 대상 앱의 판정 결과가 아닙니다.").font(.footnote).foregroundStyle(.secondary)
                    Text("진단값은 연결된 Mac에만 표시하며 저장하지 않습니다. 다른 앱으로 전환하면 센서 진단이 멈춥니다. 위치 시뮬레이션 중지는 Mac의 ‘중지 · GPS 복구’에서 수행하세요.").font(.footnote).foregroundStyle(.secondary)
                }.padding()
            }.navigationTitle("GPS Receiver")
                .onAppear { if ProcessInfo.processInfo.arguments.contains("--diagnostics") { model.start() } }
                .onChange(of: scenePhase) { _, phase in
                    if phase == .background { model.setBackground(true) }
                    else if phase == .active { model.setBackground(false) }
                }
        }
    }
    var rawSpeed: String {
        guard let fix = model.location else { return "—" }
        return fix.speed < 0 ? "유효하지 않음 (\(fix.speed))" : String(format: "%.1f km/h", fix.speed * 3.6)
    }
    func row(_ title: String, _ value: String) -> some View {
        HStack(alignment: .top) { Text(title).foregroundStyle(.secondary); Spacer(); Text(value).monospacedDigit().multilineTextAlignment(.trailing) }.font(.subheadline)
    }
}

@main struct ReceiverApp: App { var body: some Scene { WindowGroup { ReceiverView() } } }
