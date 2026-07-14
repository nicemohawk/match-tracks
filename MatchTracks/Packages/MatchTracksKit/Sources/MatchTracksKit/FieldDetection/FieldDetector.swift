import Foundation

/// The outcome of trying to identify which field a match track was played on.
public enum FieldMatchResult: Sendable {
    /// The track matches a field already in the database.
    case matched(DetectedField)
    /// The track fits a plausible pitch that matches nothing known.
    case inferred(FieldRectangle)
    /// The track is too short or degenerate to say anything about the field.
    case none
}

/// Identifies the field a GPS track was recorded on, robust to rotated and
/// adjacent pitches.
public struct FieldDetector: Sendable {

    public init() {}

    /// Detect the field for a match track.
    ///
    /// Applies a density filter first (stray points walked toward an adjacent
    /// pitch or GPS outliers get dropped), fits an oriented rectangle, then
    /// matches against known fields by the shared same-field predicate,
    /// preferring the nearest center when several match.
    public func detectField(fromTrack coordinates: [[Double]],
                            knownFields: [DetectedField]) -> FieldMatchResult {
        let filteredCoordinates = Self.densityFilteredCoordinates(coordinates)
        guard let observedRectangle = FieldGeometry.fitTrackAsFieldObservation(filteredCoordinates) else {
            return .none
        }

        var bestField: DetectedField?
        var bestDistance = Double.infinity
        for field in knownFields {
            guard FieldGeometry.rectanglesDescribeSameField(field.rectangle, observedRectangle) else {
                continue
            }
            let distance = FieldGeometry.haversineDistanceMeters(
                latitude1: field.rectangle.centerLatitude,
                longitude1: field.rectangle.centerLongitude,
                latitude2: observedRectangle.centerLatitude,
                longitude2: observedRectangle.centerLongitude)
            if distance < bestDistance {
                bestField = field
                bestDistance = distance
            }
        }

        if let matchedField = bestField {
            return .matched(matchedField)
        }
        return .inferred(observedRectangle)
    }

    /// Drop grid cells containing exactly one point.
    ///
    /// Bins points into `cellSizeMeters` squares in the local plane around the
    /// centroid; single-occupant cells are GPS outliers or stray walkover
    /// points toward a neighboring pitch and would skew the rectangle fit.
    public static func densityFilteredCoordinates(_ coordinates: [[Double]],
                                                  cellSizeMeters: Double = 5) -> [[Double]] {
        let validCoordinates = coordinates.filter { $0.count >= 2 }
        guard validCoordinates.count >= 3 else { return validCoordinates }

        let centroidLatitude = validCoordinates.map { $0[0] }.reduce(0, +)
            / Double(validCoordinates.count)
        let centroidLongitude = validCoordinates.map { $0[1] }.reduce(0, +)
            / Double(validCoordinates.count)
        let cosReferenceLatitude = cos(centroidLatitude * .pi / 180)

        struct Cell: Hashable {
            let column: Int
            let row: Int
        }

        func cell(for coordinate: [Double]) -> Cell {
            let eastMeters = (coordinate[1] - centroidLongitude) * .pi / 180
                * FieldGeometry.earthRadiusMeters * cosReferenceLatitude
            let northMeters = (coordinate[0] - centroidLatitude) * .pi / 180
                * FieldGeometry.earthRadiusMeters
            return Cell(column: Int((eastMeters / cellSizeMeters).rounded(.down)),
                        row: Int((northMeters / cellSizeMeters).rounded(.down)))
        }

        var occupancy: [Cell: Int] = [:]
        for coordinate in validCoordinates {
            occupancy[cell(for: coordinate), default: 0] += 1
        }

        return validCoordinates.filter { occupancy[cell(for: $0), default: 0] > 1 }
    }
}
