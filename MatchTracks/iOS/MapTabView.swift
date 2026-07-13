import SwiftUI
import MapKit
import MatchTracksKit

/// The GPS track over a map, with the detected field's touchlines drawn as an
/// oriented rectangle.
struct MapTabView: View {
    let session: MatchSession
    let field: DetectedField?

    var body: some View {
        Map(initialPosition: initialCameraPosition) {
            if trackCoordinates.count >= 2 {
                MapPolyline(coordinates: trackCoordinates)
                    .stroke(.blue, lineWidth: 3)
            }
            if let fieldCorners {
                MapPolygon(coordinates: fieldCorners)
                    .foregroundStyle(.green.opacity(0.15))
                    .stroke(.green, lineWidth: 2)
            }
        }
        .mapStyle(.hybrid)
    }

    /// The track, thinned to at most 1000 points for rendering.
    private var trackCoordinates: [CLLocationCoordinate2D] {
        let samples = session.locationSamples
        guard !samples.isEmpty else { return [] }
        let strideLength = max(1, samples.count / 1000)
        return stride(from: 0, to: samples.count, by: strideLength).map { index in
            CLLocationCoordinate2D(latitude: samples[index].latitude,
                                   longitude: samples[index].longitude)
        }
    }

    /// The field rectangle's four corners in map coordinates.
    private var fieldCorners: [CLLocationCoordinate2D]? {
        guard let rectangle = field?.rectangle else { return nil }
        return FieldCornerCalculator.corners(of: rectangle)
    }

    private var initialCameraPosition: MapCameraPosition {
        if let rectangle = field?.rectangle {
            return .region(MKCoordinateRegion(
                center: CLLocationCoordinate2D(latitude: rectangle.centerLatitude,
                                               longitude: rectangle.centerLongitude),
                latitudinalMeters: rectangle.lengthMeters * 2,
                longitudinalMeters: rectangle.lengthMeters * 2))
        }
        if let firstSample = session.locationSamples.first {
            return .region(MKCoordinateRegion(
                center: CLLocationCoordinate2D(latitude: firstSample.latitude,
                                               longitude: firstSample.longitude),
                latitudinalMeters: 400, longitudinalMeters: 400))
        }
        return .automatic
    }
}

/// Converts a FieldRectangle into its four corner coordinates using the same
/// conventions as `FieldGeometry.fieldRelativeCoordinates`.
enum FieldCornerCalculator {

    static func corners(of rectangle: FieldRectangle) -> [CLLocationCoordinate2D] {
        let halfLength = rectangle.lengthMeters / 2
        let halfWidth = rectangle.widthMeters / 2
        let fieldFrameCorners: [(along: Double, across: Double)] = [
            (-halfLength, -halfWidth), (halfLength, -halfWidth),
            (halfLength, halfWidth), (-halfLength, halfWidth),
        ]

        let headingRadians = rectangle.headingDegrees * .pi / 180
        let alongAxisEast = sin(headingRadians)
        let alongAxisNorth = cos(headingRadians)
        let acrossAxisEast = -alongAxisNorth
        let acrossAxisNorth = alongAxisEast

        let metersPerDegreeLatitude = FieldGeometry.earthRadiusMeters * .pi / 180
        let metersPerDegreeLongitude = metersPerDegreeLatitude
            * cos(rectangle.centerLatitude * .pi / 180)

        return fieldFrameCorners.map { corner in
            let eastMeters = corner.along * alongAxisEast + corner.across * acrossAxisEast
            let northMeters = corner.along * alongAxisNorth + corner.across * acrossAxisNorth
            return CLLocationCoordinate2D(
                latitude: rectangle.centerLatitude + northMeters / metersPerDegreeLatitude,
                longitude: rectangle.centerLongitude + eastMeters / metersPerDegreeLongitude)
        }
    }
}
