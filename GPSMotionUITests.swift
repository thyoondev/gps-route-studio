import XCTest
import CoreLocation
import Darwin

/// A separate transport experiment: inject a complete CLLocation, including speed.
/// Does not press app controls or directly edit records. Simulated fixes may be recorded.
@MainActor final class GPSMotionUITests: XCTestCase {
    private let speedKmh = 40.0
    private let durationSeconds = 120.0
    private let waypoints = [
        CLLocation(latitude: 37.56650, longitude: 126.97800),
        CLLocation(latitude: 37.56510, longitude: 126.97560),
        CLLocation(latitude: 37.56710, longitude: 126.97480)
    ]

    override func setUpWithError() throws {
        continueAfterFailure = false
        executionTimeAllowance = 86400
    }

    func testTransmitFullLocation() async throws {
        defer { XCUIDevice.shared.location = nil }
        if let port = ProcessInfo.processInfo.environment["GPS_STUDIO_PORT"],
           let token = ProcessInfo.processInfo.environment["GPS_STUDIO_TOKEN"] {
            try await controlledPlayback(port: port, token: token)
            return
        }
        let points = waypoints + waypoints.dropLast().reversed()
        let lengths = zip(points, points.dropFirst()).map { $0.distance(from: $1) }
        let total = lengths.reduce(0, +)
        XCTAssertGreaterThan(total, 0)
        let metersPerSecond = speedKmh / 3.6
        let clock = ContinuousClock()
        let started = clock.now
        var lastLog = -1

        while true {
            try Task.checkCancellation()
            let elapsed = Self.seconds(started.duration(to: clock.now))
            guard elapsed < durationSeconds else { break }
            var offset = (elapsed * metersPerSecond).truncatingRemainder(dividingBy: total)
            var leg = 0
            while leg < lengths.count - 1 && offset >= lengths[leg] {
                offset -= lengths[leg]
                leg += 1
            }
            let a = points[leg].coordinate, b = points[leg + 1].coordinate
            let fraction = offset / lengths[leg]
            // These local Seoul legs are under 1 km; linear coordinates suffice here.
            let coordinate = CLLocationCoordinate2D(
                latitude: a.latitude + (b.latitude - a.latitude) * fraction,
                longitude: a.longitude + (b.longitude - a.longitude) * fraction)
            let fix = CLLocation(
                coordinate: coordinate,
                altitude: 30,
                horizontalAccuracy: 5,
                verticalAccuracy: 5,
                course: Self.bearing(from: a, to: b),
                courseAccuracy: 1,
                speed: metersPerSecond,
                speedAccuracy: 0.2,
                timestamp: Date())
            XCUIDevice.shared.location = XCUILocation(location: fix)
            let second = Int(elapsed)
            if second / 5 != lastLog {
                lastLog = second / 5
                print("Full CLLocation sent: \(speedKmh) km/h, elapsed \(second)s. This is not receiver verification.")
            }
            try await Task.sleep(for: .milliseconds(500))
        }
        XCUIDevice.shared.location = nil
        print("Location simulation cleared. Check physical location on iPhone.")
    }

    /// USB multiplexing reaches loopback on the paired phone; no LAN listener.
    private func controlledPlayback(port: String, token: String) async throws {
        guard let port = UInt16(port), token.count >= 32 else { throw BridgeError.invalidCommand }
        let bridge = try USBLocationBridge(port: port)
        defer { bridge.close() }
        print("GPS Studio USB control ready")
        try await bridge.acceptClient()
        while true {
            let message = try await bridge.receive()
            guard message["token"] as? String == token,
                  let sequence = message["sequence"] as? Int else { throw BridgeError.invalidCommand }
            if message["command"] as? String == "stop" {
                XCUIDevice.shared.location = nil
                try await bridge.reply(["sequence": sequence, "restored": true])
                print("GPS Studio simulation cleared")
                return
            }
            guard message["command"] as? String == "fix",
                  let latitude = message["lat"] as? Double, latitude.isFinite, abs(latitude) <= 90,
                  let longitude = message["lon"] as? Double, longitude.isFinite, abs(longitude) <= 180,
                  let speed = message["speed"] as? Double, speed.isFinite, (0...300).contains(speed),
                  let course = message["course"] as? Double, course.isFinite, (0..<360).contains(course)
            else { throw BridgeError.invalidCommand }
            let fix = CLLocation(coordinate: .init(latitude: latitude, longitude: longitude),
                                 altitude: 30, horizontalAccuracy: 5, verticalAccuracy: 5,
                                 course: course, courseAccuracy: 1,
                                 speed: speed / 3.6, speedAccuracy: 0.2, timestamp: Date())
            XCUIDevice.shared.location = XCUILocation(location: fix)
            try await bridge.reply(["sequence": sequence, "applied": true, "speed": speed])
        }
    }

    private static func seconds(_ duration: Duration) -> Double {
        Double(duration.components.seconds) + Double(duration.components.attoseconds) / 1e18
    }

    private static func bearing(from a: CLLocationCoordinate2D, to b: CLLocationCoordinate2D) -> Double {
        let r = Double.pi / 180
        let delta = (b.longitude - a.longitude) * r
        let y = sin(delta) * cos(b.latitude * r)
        let x = cos(a.latitude * r) * sin(b.latitude * r) - sin(a.latitude * r) * cos(b.latitude * r) * cos(delta)
        return (atan2(y, x) / r + 360).truncatingRemainder(dividingBy: 360)
    }
}

private enum BridgeError: Error { case system(Int32), disconnected, timeout, invalidCommand }

/// Bounded newline JSON with a five-second watchdog. Any exit clears location
/// via the test's defer, including EOF, malformed input, and stalled controllers.
private final class USBLocationBridge {
    private var listener: Int32 = -1
    private var client: Int32 = -1
    private var buffer = Data()

    init(port: UInt16) throws {
        listener = Darwin.socket(AF_INET, SOCK_STREAM, 0)
        guard listener >= 0 else { throw BridgeError.system(errno) }
        do {
            var address = sockaddr_in()
            address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
            address.sin_family = sa_family_t(AF_INET)
            address.sin_port = port.bigEndian
            address.sin_addr.s_addr = inet_addr("127.0.0.1")
            let result = withUnsafePointer(to: &address) {
                $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                    Darwin.bind(listener, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
                }
            }
            guard result == 0, Darwin.listen(listener, 1) == 0,
                  fcntl(listener, F_SETFL, O_NONBLOCK) == 0 else { throw BridgeError.system(errno) }
        } catch { close(); throw error }
    }

    func close() {
        if client >= 0 { Darwin.close(client); client = -1 }
        if listener >= 0 { Darwin.close(listener); listener = -1 }
    }
    deinit { close() }

    func acceptClient() async throws {
        let deadline = ContinuousClock.now.advanced(by: .seconds(150))
        while ContinuousClock.now < deadline {
            client = Darwin.accept(listener, nil, nil)
            if client >= 0 {
                var yes: Int32 = 1
                guard fcntl(client, F_SETFL, O_NONBLOCK) == 0,
                      setsockopt(client, SOL_SOCKET, SO_NOSIGPIPE, &yes, socklen_t(MemoryLayout<Int32>.size)) == 0
                else { throw BridgeError.system(errno) }
                return
            }
            guard errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR else { throw BridgeError.system(errno) }
            try await Task.sleep(for: .milliseconds(50))
        }
        throw BridgeError.timeout
    }

    func receive() async throws -> [String: Any] {
        let deadline = ContinuousClock.now.advanced(by: .seconds(5))
        var bytes = [UInt8](repeating: 0, count: 4096)
        while ContinuousClock.now < deadline {
            if let newline = buffer.firstIndex(of: 10) {
                let line = buffer[..<newline]; buffer.removeSubrange(...newline)
                guard let message = try JSONSerialization.jsonObject(with: line) as? [String: Any] else { throw BridgeError.invalidCommand }
                return message
            }
            let count = Darwin.recv(client, &bytes, bytes.count, 0)
            if count == 0 { throw BridgeError.disconnected }
            if count > 0 {
                buffer.append(contentsOf: bytes.prefix(count))
                guard buffer.count <= 16384 else { throw BridgeError.invalidCommand }
            } else if errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR { throw BridgeError.system(errno) }
            try await Task.sleep(for: .milliseconds(20))
        }
        throw BridgeError.timeout
    }

    func reply(_ message: [String: Any]) async throws {
        let data = try JSONSerialization.data(withJSONObject: message) + Data([10])
        var offset = 0
        let deadline = ContinuousClock.now.advanced(by: .seconds(3))
        while offset < data.count {
            guard ContinuousClock.now < deadline else { throw BridgeError.timeout }
            let count = data.withUnsafeBytes { Darwin.send(client, $0.baseAddress!.advanced(by: offset), data.count - offset, 0) }
            if count > 0 { offset += count }
            else if count == 0 { throw BridgeError.disconnected }
            else if errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR { throw BridgeError.system(errno) }
            if offset < data.count { try await Task.sleep(for: .milliseconds(20)) }
        }
    }
}
