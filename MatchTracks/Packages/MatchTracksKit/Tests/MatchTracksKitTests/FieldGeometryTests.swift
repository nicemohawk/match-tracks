import XCTest
@testable import MatchTracksKit

/// Shared synthetic-data helpers for geometry and analysis tests.
enum SyntheticField {

    static let metersPerDegreeLatitude = FieldGeometry.earthRadiusMeters * .pi / 180

    static func metersPerDegreeLongitude(atLatitude latitude: Double) -> Double {
        metersPerDegreeLatitude * cos(latitude * .pi / 180)
    }

    /// Convert local (east, north) meter offsets from a center into [lat, lon].
    static func coordinate(centerLatitude: Double, centerLongitude: Double,
                           eastMeters: Double, northMeters: Double) -> [Double] {
        [
            centerLatitude + northMeters / metersPerDegreeLatitude,
            centerLongitude + eastMeters / metersPerDegreeLongitude(atLatitude: centerLatitude),
        ]
    }

    /// Convert field-frame (alongAxis, acrossAxis) offsets into (east, north)
    /// using the same conventions as `FieldGeometry.fieldRelativeCoordinates`:
    /// the along axis points toward the compass heading, the across axis is
    /// its left-hand perpendicular.
    static func eastNorth(alongMeters: Double, acrossMeters: Double,
                          headingDegrees: Double) -> (east: Double, north: Double) {
        let headingRadians = headingDegrees * .pi / 180
        let alongEast = sin(headingRadians)
        let alongNorth = cos(headingRadians)
        let acrossEast = -alongNorth
        let acrossNorth = alongEast
        return (
            east: alongMeters * alongEast + acrossMeters * acrossEast,
            north: alongMeters * alongNorth + acrossMeters * acrossNorth
        )
    }

    /// Perimeter points of an oriented rectangle, like a walked touchline.
    static func outline(centerLatitude: Double, centerLongitude: Double,
                        lengthMeters: Double = 100, widthMeters: Double = 64,
                        headingDegrees: Double = 0, pointsPerSide: Int = 12) -> [[Double]] {
        let halfLength = lengthMeters / 2
        let halfWidth = widthMeters / 2
        let corners: [(along: Double, across: Double)] = [
            (-halfLength, -halfWidth), (halfLength, -halfWidth),
            (halfLength, halfWidth), (-halfLength, halfWidth),
        ]

        var points: [[Double]] = []
        for cornerIndex in 0..<4 {
            let start = corners[cornerIndex]
            let end = corners[(cornerIndex + 1) % 4]
            for step in 0..<pointsPerSide {
                let fraction = Double(step) / Double(pointsPerSide)
                let along = start.along + (end.along - start.along) * fraction
                let across = start.across + (end.across - start.across) * fraction
                let offset = eastNorth(alongMeters: along, acrossMeters: across,
                                       headingDegrees: headingDegrees)
                points.append(coordinate(centerLatitude: centerLatitude,
                                         centerLongitude: centerLongitude,
                                         eastMeters: offset.east, northMeters: offset.north))
            }
        }
        return points
    }

    /// A deterministic grid of points covering a rectangle's interior, like a
    /// match track sweeping the pitch nearly edge-to-edge.
    static func interiorCloud(centerLatitude: Double, centerLongitude: Double,
                              lengthMeters: Double = 100, widthMeters: Double = 64,
                              headingDegrees: Double = 0,
                              columns: Int = 20, rows: Int = 15) -> [[Double]] {
        let coverage = 0.97
        var points: [[Double]] = []
        for column in 0..<columns {
            for row in 0..<rows {
                let alongFraction = columns > 1 ? Double(column) / Double(columns - 1) : 0.5
                let acrossFraction = rows > 1 ? Double(row) / Double(rows - 1) : 0.5
                let along = (alongFraction - 0.5) * lengthMeters * coverage
                let across = (acrossFraction - 0.5) * widthMeters * coverage
                let offset = eastNorth(alongMeters: along, acrossMeters: across,
                                       headingDegrees: headingDegrees)
                points.append(coordinate(centerLatitude: centerLatitude,
                                         centerLongitude: centerLongitude,
                                         eastMeters: offset.east, northMeters: offset.north))
            }
        }
        return points
    }
}

final class FieldGeometryTests: XCTestCase {

    let baseLatitude = 39.33
    let baseLongitude = -82.10

    func testHaversineOneDegreeOfLatitude() {
        let distance = FieldGeometry.haversineDistanceMeters(
            latitude1: 39.0, longitude1: -82.0, latitude2: 40.0, longitude2: -82.0)
        XCTAssertEqual(distance, 111_195, accuracy: 111_195 * 0.005)
    }

    func testFoldHeading() {
        XCTAssertEqual(FieldGeometry.foldHeadingDegrees(0), 0)
        XCTAssertEqual(FieldGeometry.foldHeadingDegrees(180), 0)
        XCTAssertEqual(FieldGeometry.foldHeadingDegrees(190), 10)
        XCTAssertEqual(FieldGeometry.foldHeadingDegrees(359), 179)
        XCTAssertEqual(FieldGeometry.foldHeadingDegrees(-10), 170)
    }

    func testHeadingEquivalence() {
        XCTAssertTrue(FieldGeometry.headingsEquivalent(179, 1))
        XCTAssertTrue(FieldGeometry.headingsEquivalent(5, 185))
        XCTAssertFalse(FieldGeometry.headingsEquivalent(95, 80))
        XCTAssertTrue(FieldGeometry.headingsEquivalent(95, 88))
    }

    func testWeightedHeadingAverage() {
        let nineToOne = FieldGeometry.averageHeadingsDegrees(10, 9, 20, 1)
        XCTAssertEqual(nineToOne, 11, accuracy: 0.5)

        let acrossTheSeam = FieldGeometry.averageHeadingsDegrees(179, 1, 1, 1)
        let distanceFromZero = min(acrossTheSeam, 180 - acrossTheSeam)
        XCTAssertLessThan(distanceFromZero, 1)
    }

    func testMinimumAreaRectAxisAlignedAndRotated() throws {
        let axisAlignedPoints: [FieldGeometry.PlanarPoint] = [
            (-50, -32), (50, -32), (50, 32), (-50, 32), (0, -32), (0, 32), (-50, 0), (50, 0),
        ]
        let axisAligned = try XCTUnwrap(FieldGeometry.minimumAreaRect(axisAlignedPoints))
        XCTAssertEqual(axisAligned.longSide, 100, accuracy: 0.001)
        XCTAssertEqual(axisAligned.shortSide, 64, accuracy: 0.001)
        XCTAssertEqual(axisAligned.center.x, 0, accuracy: 0.001)
        XCTAssertEqual(axisAligned.center.y, 0, accuracy: 0.001)

        let rotationRadians = 30.0 * .pi / 180
        let rotatedPoints = axisAlignedPoints.map { point -> FieldGeometry.PlanarPoint in
            (x: point.x * cos(rotationRadians) - point.y * sin(rotationRadians),
             y: point.x * sin(rotationRadians) + point.y * cos(rotationRadians))
        }
        let rotated = try XCTUnwrap(FieldGeometry.minimumAreaRect(rotatedPoints))
        XCTAssertEqual(rotated.longSide, 100, accuracy: 0.01)
        XCTAssertEqual(rotated.shortSide, 64, accuracy: 0.01)
        let axisDegrees = FieldGeometry.foldHeadingDegrees(rotated.axisAngleRadians * 180 / .pi)
        XCTAssertEqual(axisDegrees, 30, accuracy: 0.5)
    }

    func testFitRectangleRecoversFieldAtSeveralHeadings() throws {
        for headingDegrees in [0.0, 25.0, 110.0, 179.0] {
            let outline = SyntheticField.outline(
                centerLatitude: baseLatitude, centerLongitude: baseLongitude,
                headingDegrees: headingDegrees)
            let fit = try XCTUnwrap(FieldGeometry.fitRectangle(toCoordinates: outline),
                                    "fit failed at heading \(headingDegrees)")
            XCTAssertEqual(fit.lengthMeters, 100, accuracy: 1.0)
            XCTAssertEqual(fit.widthMeters, 64, accuracy: 1.0)
            let headingError = abs(fit.headingDegrees - FieldGeometry.foldHeadingDegrees(headingDegrees))
            XCTAssertLessThan(min(headingError, 180 - headingError), 2.0,
                              "heading off at \(headingDegrees): \(fit.headingDegrees)")
            XCTAssertEqual(fit.centerLatitude, baseLatitude, accuracy: 0.0001)
            XCTAssertEqual(fit.centerLongitude, baseLongitude, accuracy: 0.0001)
        }
    }

    func testFitRectangleRejectsDegenerateInput() {
        XCTAssertNil(FieldGeometry.fitRectangle(toCoordinates: [[39.33, -82.10], [39.34, -82.10]]))

        let collinear = (0..<10).map { index in
            [39.33 + Double(index) * 0.0001, -82.10]
        }
        XCTAssertNil(FieldGeometry.fitRectangle(toCoordinates: collinear))
    }

    func testTrackObservationRequiresEnoughPoints() {
        let smallCloud = SyntheticField.interiorCloud(
            centerLatitude: baseLatitude, centerLongitude: baseLongitude,
            lengthMeters: 95, widthMeters: 60, columns: 14, rows: 14)  // 196 < 200
        XCTAssertNil(FieldGeometry.fitTrackAsFieldObservation(smallCloud))
    }

    func testTrackObservationFitsPlausiblePitch() throws {
        let cloud = SyntheticField.interiorCloud(
            centerLatitude: baseLatitude, centerLongitude: baseLongitude,
            lengthMeters: 95, widthMeters: 60)  // 300 points
        let observation = try XCTUnwrap(FieldGeometry.fitTrackAsFieldObservation(cloud))
        XCTAssertTrue(FieldGeometry.rectangleWithinSanityBounds(observation))
        // 97% coverage × 1.05 expansion ≈ 1.019 of true size.
        XCTAssertEqual(observation.lengthMeters, 95 * 0.97 * 1.05, accuracy: 1.0)
        XCTAssertEqual(observation.widthMeters, 60 * 0.97 * 1.05, accuracy: 1.0)
    }

    func testTrackObservationRejectsWarmupBox() {
        let warmupBox = SyntheticField.interiorCloud(
            centerLatitude: baseLatitude, centerLongitude: baseLongitude,
            lengthMeters: 20, widthMeters: 10, columns: 20, rows: 20)  // 400 points
        XCTAssertNil(FieldGeometry.fitTrackAsFieldObservation(warmupBox))
    }

    func testSameFieldPredicate() {
        let field = FieldRectangle(centerLatitude: baseLatitude, centerLongitude: baseLongitude,
                                   lengthMeters: 100, widthMeters: 64, headingDegrees: 5)

        var flipped = field
        flipped.headingDegrees = FieldGeometry.foldHeadingDegrees(185)
        XCTAssertTrue(FieldGeometry.rectanglesDescribeSameField(field, flipped))

        var farAway = field
        farAway.centerLatitude += 100 / SyntheticField.metersPerDegreeLatitude
        XCTAssertFalse(FieldGeometry.rectanglesDescribeSameField(field, farAway))

        var differentSize = field
        differentSize.lengthMeters += 20
        XCTAssertFalse(FieldGeometry.rectanglesDescribeSameField(field, differentSize))

        var rotated = field
        rotated.headingDegrees = 50
        XCTAssertFalse(FieldGeometry.rectanglesDescribeSameField(field, rotated))
    }

    func testFieldRelativeCoordinates() {
        // Long axis points east (heading 90): a point 10 m east of center
        // lies at +10 along the long axis, 0 across.
        let eastFacingField = FieldRectangle(
            centerLatitude: baseLatitude, centerLongitude: baseLongitude,
            lengthMeters: 100, widthMeters: 64, headingDegrees: 90)
        let tenMetersEast = SyntheticField.coordinate(
            centerLatitude: baseLatitude, centerLongitude: baseLongitude,
            eastMeters: 10, northMeters: 0)
        let position = FieldGeometry.fieldRelativeCoordinates(
            latitude: tenMetersEast[0], longitude: tenMetersEast[1], in: eastFacingField)
        XCTAssertEqual(position.x, 10, accuracy: 0.05)
        XCTAssertEqual(position.y, 0, accuracy: 0.05)

        // Round trip through the synthetic generator's conventions.
        let heading = 25.0
        let field = FieldRectangle(centerLatitude: baseLatitude, centerLongitude: baseLongitude,
                                   lengthMeters: 100, widthMeters: 64, headingDegrees: heading)
        let offset = SyntheticField.eastNorth(alongMeters: 30, acrossMeters: -12,
                                              headingDegrees: heading)
        let coordinate = SyntheticField.coordinate(
            centerLatitude: baseLatitude, centerLongitude: baseLongitude,
            eastMeters: offset.east, northMeters: offset.north)
        let roundTripped = FieldGeometry.fieldRelativeCoordinates(
            latitude: coordinate[0], longitude: coordinate[1], in: field)
        XCTAssertEqual(roundTripped.x, 30, accuracy: 0.05)
        XCTAssertEqual(roundTripped.y, -12, accuracy: 0.05)
    }

    func testDensityFilterDropsIsolatedPoints() {
        let clusterCoordinate = [baseLatitude, baseLongitude]
        let cluster = Array(repeating: clusterCoordinate, count: 30)
        let isolated = [
            SyntheticField.coordinate(centerLatitude: baseLatitude, centerLongitude: baseLongitude,
                                      eastMeters: 100, northMeters: 0),
            SyntheticField.coordinate(centerLatitude: baseLatitude, centerLongitude: baseLongitude,
                                      eastMeters: -120, northMeters: 40),
            SyntheticField.coordinate(centerLatitude: baseLatitude, centerLongitude: baseLongitude,
                                      eastMeters: 0, northMeters: 150),
        ]

        let filtered = FieldDetector.densityFilteredCoordinates(cluster + isolated)

        XCTAssertEqual(filtered.count, 30)
        XCTAssertTrue(filtered.allSatisfy { $0 == clusterCoordinate })
    }

    func testFieldDetectorMatchesKnownFieldAndInfersNewOnes() throws {
        let knownField = DetectedField(
            rectangle: FieldRectangle(centerLatitude: baseLatitude, centerLongitude: baseLongitude,
                                      lengthMeters: 100, widthMeters: 64, headingDegrees: 0),
            source: "trained")
        // Dense like a real 1 Hz match track, so the detector's density
        // filter (which drops single-occupant 5 m cells) keeps the cloud.
        let track = SyntheticField.interiorCloud(
            centerLatitude: baseLatitude, centerLongitude: baseLongitude,
            lengthMeters: 100, widthMeters: 64, columns: 40, rows: 30)

        let detector = FieldDetector()

        switch detector.detectField(fromTrack: track, knownFields: [knownField]) {
        case .matched(let field):
            XCTAssertEqual(field.id, knownField.id)
        default:
            XCTFail("expected a match against the known field")
        }

        let freshVenueTrack = SyntheticField.interiorCloud(
            centerLatitude: baseLatitude + 0.5, centerLongitude: baseLongitude + 0.5,
            lengthMeters: 100, widthMeters: 64, columns: 40, rows: 30)
        switch detector.detectField(fromTrack: freshVenueTrack, knownFields: [knownField]) {
        case .inferred(let rectangle):
            XCTAssertTrue(FieldGeometry.rectangleWithinSanityBounds(rectangle))
        default:
            XCTFail("expected an inferred field at the fresh venue")
        }

        switch detector.detectField(fromTrack: Array(track.prefix(50)), knownFields: [knownField]) {
        case .none:
            break
        default:
            XCTFail("expected no field from a 50-point track")
        }
    }
}
