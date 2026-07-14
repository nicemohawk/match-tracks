import Foundation

/// Pure geographic geometry for field detection: rectangle fitting, folded
/// compass headings, and field-relative coordinates.
///
/// Mirrors the server's geometry module so device- and server-side field
/// observations agree. Conventions:
/// - Coordinates arrive as `[latitude, longitude]` pairs (the wire format).
/// - Headings are compass bearings in degrees folded into `[0, 180)`:
///   a pitch reads the same walked in either direction, so 179.9° ≈ 0.1°.
/// - Distances and lengths are meters.
public enum FieldGeometry {

    /// Mean Earth radius used throughout (meters).
    public static let earthRadiusMeters = 6371000.8

    /// Plausible soccer-pitch bounds (meters).
    public static let fieldLengthBoundsMeters = 60.0...130.0
    public static let fieldWidthBoundsMeters = 30.0...90.0

    /// Proximity thresholds for "same physical field".
    public static let sameFieldCenterDistanceMeters = 40.0
    public static let sameFieldLengthToleranceMeters = 15.0
    public static let sameFieldWidthToleranceMeters = 15.0
    public static let sameFieldHeadingToleranceDegrees = 10.0

    /// Player tracks under-cover the pitch; fitted rectangles expand by this
    /// factor per axis before being treated as a field observation.
    public static let trackFitExpansion = 1.05

    /// Tracks with fewer points than this are ignored as field observations.
    public static let minimumTrackPointsForObservation = 200

    // MARK: - Distances and headings

    /// Great-circle distance in meters between two WGS84 points.
    public static func haversineDistanceMeters(latitude1: Double, longitude1: Double,
                                               latitude2: Double, longitude2: Double) -> Double {
        let latitude1Radians = latitude1 * .pi / 180
        let latitude2Radians = latitude2 * .pi / 180
        let deltaLatitudeRadians = (latitude2 - latitude1) * .pi / 180
        let deltaLongitudeRadians = (longitude2 - longitude1) * .pi / 180

        let haversineTerm = pow(sin(deltaLatitudeRadians / 2), 2)
            + cos(latitude1Radians) * cos(latitude2Radians) * pow(sin(deltaLongitudeRadians / 2), 2)
        let centralAngle = 2 * asin(min(1.0, sqrt(haversineTerm)))
        return earthRadiusMeters * centralAngle
    }

    /// Fold an arbitrary bearing into the axis domain `[0, 180)`.
    public static func foldHeadingDegrees(_ headingDegrees: Double) -> Double {
        let remainder = headingDegrees.truncatingRemainder(dividingBy: 180)
        return remainder < 0 ? remainder + 180 : remainder
    }

    /// True when two folded headings are within tolerance, honoring the
    /// wraparound (179° ≈ 1°). 180° flips are equivalent by construction.
    public static func headingsEquivalent(_ firstDegrees: Double, _ secondDegrees: Double,
                                          toleranceDegrees: Double = sameFieldHeadingToleranceDegrees) -> Bool {
        let difference = abs(foldHeadingDegrees(firstDegrees) - foldHeadingDegrees(secondDegrees))
        return min(difference, 180 - difference) <= toleranceDegrees
    }

    /// Weighted average of two folded headings, correct across the wrap
    /// (179° and 1° with equal weights average to 0°, never 90°).
    ///
    /// Doubles the angles, averages the weighted unit vectors, and halves the
    /// resulting angle back into `[0, 180)`.
    public static func averageHeadingsDegrees(_ firstDegrees: Double, _ firstWeight: Double,
                                              _ secondDegrees: Double, _ secondWeight: Double) -> Double {
        let doubledFirstRadians = 2 * foldHeadingDegrees(firstDegrees) * .pi / 180
        let doubledSecondRadians = 2 * foldHeadingDegrees(secondDegrees) * .pi / 180

        let vectorX = firstWeight * cos(doubledFirstRadians) + secondWeight * cos(doubledSecondRadians)
        let vectorY = firstWeight * sin(doubledFirstRadians) + secondWeight * sin(doubledSecondRadians)

        let averagedDoubledRadians = atan2(vectorY, vectorX)
        return foldHeadingDegrees(averagedDoubledRadians * 180 / .pi / 2)
    }

    // MARK: - Planar geometry

    /// A point in the local east/north plane (meters).
    public typealias PlanarPoint = (x: Double, y: Double)

    private static func crossProductZ(origin: PlanarPoint, first: PlanarPoint,
                                      second: PlanarPoint) -> Double {
        (first.x - origin.x) * (second.y - origin.y) - (first.y - origin.y) * (second.x - origin.x)
    }

    /// Monotone-chain convex hull, counter-clockwise, no repeated last vertex.
    public static func convexHull(_ points: [PlanarPoint]) -> [PlanarPoint] {
        var uniquePoints = Array(Set(points.map { HashablePoint(x: $0.x, y: $0.y) }))
            .map { (x: $0.x, y: $0.y) }
        uniquePoints.sort { $0.x == $1.x ? $0.y < $1.y : $0.x < $1.x }

        guard uniquePoints.count > 2 else { return uniquePoints }

        var lowerChain: [PlanarPoint] = []
        for point in uniquePoints {
            while lowerChain.count >= 2,
                  crossProductZ(origin: lowerChain[lowerChain.count - 2],
                                first: lowerChain[lowerChain.count - 1],
                                second: point) <= 0 {
                lowerChain.removeLast()
            }
            lowerChain.append(point)
        }

        var upperChain: [PlanarPoint] = []
        for point in uniquePoints.reversed() {
            while upperChain.count >= 2,
                  crossProductZ(origin: upperChain[upperChain.count - 2],
                                first: upperChain[upperChain.count - 1],
                                second: point) <= 0 {
                upperChain.removeLast()
            }
            upperChain.append(point)
        }

        return Array(lowerChain.dropLast()) + Array(upperChain.dropLast())
    }

    private struct HashablePoint: Hashable {
        let x: Double
        let y: Double
    }

    /// Rotating-calipers minimum-area oriented rectangle over a point cloud's
    /// convex hull.
    ///
    /// - Returns: center in the local plane, the long and short side lengths,
    ///   and the direction of the long side in radians (CCW from +x/east).
    public static func minimumAreaRect(_ points: [PlanarPoint])
        -> (center: PlanarPoint, longSide: Double, shortSide: Double, axisAngleRadians: Double)? {
        let hull = convexHull(points)
        guard hull.count >= 3 else { return nil }

        var bestArea = Double.infinity
        var bestResult: (center: PlanarPoint, longSide: Double, shortSide: Double,
                         axisAngleRadians: Double)?

        for index in 0..<hull.count {
            let currentVertex = hull[index]
            let nextVertex = hull[(index + 1) % hull.count]

            let edgeX = nextVertex.x - currentVertex.x
            let edgeY = nextVertex.y - currentVertex.y
            let edgeLength = hypot(edgeX, edgeY)
            guard edgeLength > 0 else { continue }

            let axisX = edgeX / edgeLength
            let axisY = edgeY / edgeLength
            let perpendicularX = -axisY
            let perpendicularY = axisX

            var minAlongEdge = Double.infinity, maxAlongEdge = -Double.infinity
            var minAlongPerpendicular = Double.infinity, maxAlongPerpendicular = -Double.infinity
            for point in hull {
                let projectionEdge = point.x * axisX + point.y * axisY
                let projectionPerpendicular = point.x * perpendicularX + point.y * perpendicularY
                minAlongEdge = min(minAlongEdge, projectionEdge)
                maxAlongEdge = max(maxAlongEdge, projectionEdge)
                minAlongPerpendicular = min(minAlongPerpendicular, projectionPerpendicular)
                maxAlongPerpendicular = max(maxAlongPerpendicular, projectionPerpendicular)
            }

            let extentEdge = maxAlongEdge - minAlongEdge
            let extentPerpendicular = maxAlongPerpendicular - minAlongPerpendicular
            let area = extentEdge * extentPerpendicular
            guard area < bestArea else { continue }
            bestArea = area

            let centerEdge = (minAlongEdge + maxAlongEdge) / 2
            let centerPerpendicular = (minAlongPerpendicular + maxAlongPerpendicular) / 2
            let center: PlanarPoint = (
                x: centerEdge * axisX + centerPerpendicular * perpendicularX,
                y: centerEdge * axisY + centerPerpendicular * perpendicularY
            )

            if extentEdge >= extentPerpendicular {
                bestResult = (center, extentEdge, extentPerpendicular, atan2(axisY, axisX))
            } else {
                bestResult = (center, extentPerpendicular, extentEdge,
                              atan2(perpendicularY, perpendicularX))
            }
        }

        return bestResult
    }

    // MARK: - Rectangle fitting

    /// Project `[latitude, longitude]` pairs into a local east/north plane
    /// around their centroid. Returns the centroid alongside the points.
    static func projectToLocalPlane(_ coordinates: [[Double]])
        -> (points: [PlanarPoint], centroidLatitude: Double, centroidLongitude: Double)? {
        let distinct = Array(Set(coordinates.compactMap { pair -> HashablePoint? in
            guard pair.count >= 2 else { return nil }
            return HashablePoint(x: pair[0], y: pair[1])
        }))
        guard distinct.count >= 3 else { return nil }

        let centroidLatitude = distinct.map(\.x).reduce(0, +) / Double(distinct.count)
        let centroidLongitude = distinct.map(\.y).reduce(0, +) / Double(distinct.count)
        let cosReferenceLatitude = cos(centroidLatitude * .pi / 180)

        let points = distinct.map { coordinate -> PlanarPoint in
            (
                x: (coordinate.y - centroidLongitude) * .pi / 180 * earthRadiusMeters * cosReferenceLatitude,
                y: (coordinate.x - centroidLatitude) * .pi / 180 * earthRadiusMeters
            )
        }
        return (points, centroidLatitude, centroidLongitude)
    }

    /// Fit an oriented rectangle to a cloud of `[latitude, longitude]` points.
    ///
    /// Returns nil for degenerate input (fewer than 3 distinct points or a
    /// collinear cloud with no area).
    public static func fitRectangle(toCoordinates coordinates: [[Double]]) -> FieldRectangle? {
        guard let projection = projectToLocalPlane(coordinates),
              let rect = minimumAreaRect(projection.points),
              rect.longSide > 0, rect.shortSide > 0 else {
            return nil
        }

        let cosReferenceLatitude = cos(projection.centroidLatitude * .pi / 180)
        let centerLatitude = projection.centroidLatitude
            + rect.center.y / earthRadiusMeters * 180 / .pi
        let centerLongitude = projection.centroidLongitude
            + rect.center.x / (earthRadiusMeters * cosReferenceLatitude) * 180 / .pi

        return FieldRectangle(
            centerLatitude: centerLatitude,
            centerLongitude: centerLongitude,
            lengthMeters: rect.longSide,
            widthMeters: rect.shortSide,
            headingDegrees: foldHeadingDegrees(90 - rect.axisAngleRadians * 180 / .pi)
        )
    }

    /// Fit a match GPS track as evidence of field geometry: requires at least
    /// `minimumTrackPointsForObservation` points, expands the raw fit by
    /// `trackFitExpansion` per axis, and returns nil unless the expanded fit
    /// is a plausible pitch.
    public static func fitTrackAsFieldObservation(_ coordinates: [[Double]]) -> FieldRectangle? {
        guard coordinates.count >= minimumTrackPointsForObservation,
              let rawFit = fitRectangle(toCoordinates: coordinates) else {
            return nil
        }

        var expandedFit = rawFit
        expandedFit.lengthMeters *= trackFitExpansion
        expandedFit.widthMeters *= trackFitExpansion

        guard rectangleWithinSanityBounds(expandedFit) else { return nil }
        return expandedFit
    }

    /// True when the rectangle is a plausible soccer pitch.
    public static func rectangleWithinSanityBounds(_ rectangle: FieldRectangle) -> Bool {
        fieldLengthBoundsMeters.contains(rectangle.lengthMeters)
            && fieldWidthBoundsMeters.contains(rectangle.widthMeters)
    }

    /// Same-physical-field predicate: centers within 40 m, sides within 15 m,
    /// folded headings within 10° (180° flips equivalent).
    public static func rectanglesDescribeSameField(_ first: FieldRectangle,
                                                   _ second: FieldRectangle) -> Bool {
        let centerDistance = haversineDistanceMeters(
            latitude1: first.centerLatitude, longitude1: first.centerLongitude,
            latitude2: second.centerLatitude, longitude2: second.centerLongitude)
        return centerDistance < sameFieldCenterDistanceMeters
            && abs(first.lengthMeters - second.lengthMeters) < sameFieldLengthToleranceMeters
            && abs(first.widthMeters - second.widthMeters) < sameFieldWidthToleranceMeters
            && headingsEquivalent(first.headingDegrees, second.headingDegrees)
    }

    // MARK: - Field-relative coordinates

    /// A point's position in the field frame.
    ///
    /// Sign conventions: `x` runs along the long axis, positive toward the
    /// rectangle's heading direction, in `[-length/2, +length/2]`; `y` runs
    /// across the field, positive to the left of the heading direction, in
    /// `[-width/2, +width/2]`.
    public static func fieldRelativeCoordinates(latitude: Double, longitude: Double,
                                                in rectangle: FieldRectangle) -> PlanarPoint {
        let cosReferenceLatitude = cos(rectangle.centerLatitude * .pi / 180)
        let eastMeters = (longitude - rectangle.centerLongitude) * .pi / 180
            * earthRadiusMeters * cosReferenceLatitude
        let northMeters = (latitude - rectangle.centerLatitude) * .pi / 180 * earthRadiusMeters

        // The long axis points along the compass heading: unit vector
        // (east, north) = (sin θ, cos θ). The across-field axis is its
        // left-hand perpendicular (-cos θ, sin θ).
        let headingRadians = rectangle.headingDegrees * .pi / 180
        let alongAxisEast = sin(headingRadians)
        let alongAxisNorth = cos(headingRadians)
        let acrossAxisEast = -alongAxisNorth
        let acrossAxisNorth = alongAxisEast

        return (
            x: eastMeters * alongAxisEast + northMeters * alongAxisNorth,
            y: eastMeters * acrossAxisEast + northMeters * acrossAxisNorth
        )
    }
}
