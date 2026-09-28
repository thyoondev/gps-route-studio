import SwiftUI
import MapKit

/// Reuses route pins between telemetry updates; MapKit clusters visible markers.
@MainActor struct RouteMap: NSViewRepresentable {
    let points: [Waypoint]
    let routeRevision: UUID
    let fitRequest: UUID
    let sample: CLLocationCoordinate2D?
    let trail: [CLLocationCoordinate2D]
    let running: Bool
    let addPoint: (CLLocationCoordinate2D) -> Void

    func makeCoordinator() -> Coordinator { Coordinator() }
    func makeNSView(context: Context) -> MKMapView {
        let map = MKMapView()
        map.delegate = context.coordinator
        map.showsCompass = true
        map.showsZoomControls = true
        map.isPitchEnabled = false
        for identifier in ["waypoint", "cluster", "current"] {
            map.register(MKMarkerAnnotationView.self, forAnnotationViewWithReuseIdentifier: identifier)
        }
        let click = NSClickGestureRecognizer(target: context.coordinator, action: #selector(Coordinator.clicked(_:)))
        click.delegate = context.coordinator
        map.addGestureRecognizer(click)
        context.coordinator.map = map
        return map
    }
    func updateNSView(_ map: MKMapView, context: Context) {
        let coordinator = context.coordinator
        coordinator.addPoint = running ? nil : addPoint
        let firstRoute = coordinator.routeRevision == nil
        if coordinator.routeRevision != routeRevision {
            coordinator.updateRoute(points, on: map)
            coordinator.routeRevision = routeRevision
        }
        if firstRoute || coordinator.fitRequest != fitRequest {
            coordinator.fitRequest = fitRequest
            coordinator.fitRoute(on: map, animated: !firstRoute)
        }
        coordinator.updatePlayback(sample: sample, trail: trail, on: map)
    }
    static func dismantleNSView(_ map: MKMapView, coordinator: Coordinator) {
        map.delegate = nil
        coordinator.addPoint = nil
    }

    @MainActor final class Coordinator: NSObject, MKMapViewDelegate, NSGestureRecognizerDelegate {
        weak var map: MKMapView?
        var addPoint: ((CLLocationCoordinate2D) -> Void)?
        var routeRevision: UUID?
        var fitRequest: UUID?
        private var pins: [UUID: RoutePin] = [:]
        private var routeLine: MKGeodesicPolyline?
        private var trailLine: MKPolyline?
        private var current: MKPointAnnotation?
        private var lastTrailDraw = -Double.infinity

        func updateRoute(_ points: [Waypoint], on map: MKMapView) {
            let ids = Set(points.map(\.id))
            let removed = pins.filter { !ids.contains($0.key) }
            map.removeAnnotations(Array(removed.values))
            for id in removed.keys { pins.removeValue(forKey: id) }
            var added: [RoutePin] = []
            for (index, point) in points.enumerated() {
                let pin: RoutePin
                if let existing = pins[point.id] { pin = existing }
                else {
                    pin = RoutePin(coordinate: point.coordinate)
                    pins[point.id] = pin
                    added.append(pin)
                }
                pin.number = index + 1
                pin.endpoint = index == 0 || index == points.count - 1
                pin.title = index == 0 ? "시작 · 1" : index == points.count - 1 ? "끝 · \(index + 1)" : "경유지 \(index + 1)"
                pin.subtitle = String(format: "%.5f, %.5f", point.coordinate.latitude, point.coordinate.longitude)
                if let view = map.view(for: pin) as? MKMarkerAnnotationView { configure(view, for: pin) }
            }
            map.addAnnotations(added)
            if let routeLine { map.removeOverlay(routeLine) }
            routeLine = nil
            if points.count >= 2 {
                let coordinates = points.map(\.coordinate)
                let line = MKGeodesicPolyline(coordinates: coordinates, count: coordinates.count)
                routeLine = line
                map.addOverlay(line, level: .aboveRoads)
            }
        }

        func fitRoute(on map: MKMapView, animated: Bool) {
            guard !pins.isEmpty else { return }
            if let routeLine {
                let rect = routeLine.boundingMapRect
                let width = max(rect.size.width, 1500), height = max(rect.size.height, 1500)
                map.setVisibleMapRect(MKMapRect(x: rect.midX - width / 2, y: rect.midY - height / 2,
                                               width: width, height: height),
                                      edgePadding: NSEdgeInsets(top: 65, left: 45, bottom: 55, right: 45), animated: animated)
            } else if let pin = pins.values.first {
                map.setRegion(MKCoordinateRegion(center: pin.coordinate, latitudinalMeters: 1000, longitudinalMeters: 1000), animated: animated)
            }
        }

        func updatePlayback(sample: CLLocationCoordinate2D?, trail: [CLLocationCoordinate2D], on map: MKMapView) {
            guard let sample else {
                if let current { map.removeAnnotation(current); self.current = nil }
                if let trailLine { map.removeOverlay(trailLine); self.trailLine = nil }
                lastTrailDraw = -Double.infinity
                return
            }
            if let current {
                if current.coordinate.latitude != sample.latitude || current.coordinate.longitude != sample.longitude {
                    current.coordinate = sample
                }
            } else {
                let annotation = MKPointAnnotation()
                annotation.title = "전송 위치"
                annotation.coordinate = sample
                current = annotation
                map.addAnnotation(annotation)
            }
            // Trail redraws never rebuild the route annotations or route overlay.
            let now = ProcessInfo.processInfo.systemUptime
            guard now - lastTrailDraw >= 1 else { return }
            lastTrailDraw = now
            if let trailLine { map.removeOverlay(trailLine) }
            trailLine = nil
            if trail.count >= 2 {
                let line = MKPolyline(coordinates: trail, count: trail.count)
                trailLine = line
                map.addOverlay(line, level: .aboveRoads)
            }
        }

        private func configure(_ view: MKMarkerAnnotationView, for pin: RoutePin) {
            view.markerTintColor = pin.number == 1 ? .systemGreen : pin.endpoint ? .systemRed : .systemBlue
            view.glyphText = String(pin.number)
            view.clusteringIdentifier = pin.endpoint ? nil : "route-waypoints"
            view.displayPriority = pin.endpoint ? .required : .defaultLow
            view.titleVisibility = .adaptive
            view.subtitleVisibility = .hidden
            view.canShowCallout = true
            view.animatesWhenAdded = false
        }
        func mapView(_ mapView: MKMapView, viewFor annotation: MKAnnotation) -> MKAnnotationView? {
            if let pin = annotation as? RoutePin {
                let view = mapView.dequeueReusableAnnotationView(withIdentifier: "waypoint", for: pin) as! MKMarkerAnnotationView
                configure(view, for: pin)
                return view
            }
            if let cluster = annotation as? MKClusterAnnotation {
                let view = mapView.dequeueReusableAnnotationView(withIdentifier: "cluster", for: cluster) as! MKMarkerAnnotationView
                view.markerTintColor = .systemIndigo
                view.glyphText = String(cluster.memberAnnotations.count)
                view.titleVisibility = .hidden
                view.subtitleVisibility = .hidden
                view.displayPriority = .defaultHigh
                view.canShowCallout = false
                view.animatesWhenAdded = false
                view.setAccessibilityLabel("경유지 \(cluster.memberAnnotations.count)개 묶음 · 클릭하여 확대")
                return view
            }
            if let current, annotation === current {
                let view = mapView.dequeueReusableAnnotationView(withIdentifier: "current", for: annotation) as! MKMarkerAnnotationView
                view.markerTintColor = .systemOrange
                view.glyphImage = NSImage(systemSymbolName: "location.fill", accessibilityDescription: "전송 위치")
                view.clusteringIdentifier = nil
                view.displayPriority = .required
                view.zPriority = .max
                view.selectedZPriority = .max
                view.canShowCallout = true
                view.animatesWhenAdded = false
                return view
            }
            return nil
        }
        func mapView(_ mapView: MKMapView, rendererFor overlay: MKOverlay) -> MKOverlayRenderer {
            guard let line = overlay as? MKPolyline else { return MKOverlayRenderer(overlay: overlay) }
            let renderer = MKPolylineRenderer(polyline: line)
            let isRoute = line === routeLine
            renderer.strokeColor = isRoute ? NSColor.systemBlue.withAlphaComponent(0.65) : .systemOrange
            renderer.lineWidth = isRoute ? 4 : 3
            return renderer
        }
        func mapView(_ mapView: MKMapView, didSelect view: MKAnnotationView) {
            guard let cluster = view.annotation as? MKClusterAnnotation else { return }
            mapView.deselectAnnotation(cluster, animated: false)
            mapView.showAnnotations(cluster.memberAnnotations, animated: true)
        }
        func gestureRecognizer(_ gestureRecognizer: NSGestureRecognizer,
                               shouldRecognizeSimultaneouslyWith otherGestureRecognizer: NSGestureRecognizer) -> Bool { true }
        func gestureRecognizer(_ gestureRecognizer: NSGestureRecognizer, shouldAttemptToRecognizeWith event: NSEvent) -> Bool {
            guard let map else { return false }
            return canAddPoint(at: map.convert(event.locationInWindow, from: nil), on: map)
        }
        func gestureRecognizerShouldBegin(_ gestureRecognizer: NSGestureRecognizer) -> Bool {
            guard let map else { return false }
            return canAddPoint(at: gestureRecognizer.location(in: map), on: map)
        }
        private func canAddPoint(at location: NSPoint, on map: MKMapView) -> Bool {
            guard addPoint != nil else { return false }
            // A click on a marker, cluster, callout or map control is not a new point.
            var hit = map.hitTest(map.convert(location, to: map.superview))
            while let view = hit, view !== map {
                if view is MKAnnotationView || view is NSControl || view is MKCompassButton || view is MKZoomControl || view is MKPitchControl { return false }
                hit = view.superview
            }
            return true
        }
        @objc func clicked(_ recognizer: NSClickGestureRecognizer) {
            guard recognizer.state == .ended, let map, let addPoint else { return }
            let point = recognizer.location(in: map)
            addPoint(map.convert(point, toCoordinateFrom: map))
        }
    }
}

private final class RoutePin: MKPointAnnotation {
    var number = 0
    var endpoint = false
    init(coordinate: CLLocationCoordinate2D) {
        super.init()
        self.coordinate = coordinate
    }
}
