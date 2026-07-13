import XCTest
@testable import MatchTracksKit

final class ModelsTests: XCTestCase {

    func testMatchEventKindRawValuesMatchWireStrings() {
        XCTAssertEqual(MatchEventKind.matchStart.rawValue, "matchStart")
        XCTAssertEqual(MatchEventKind.matchEnd.rawValue, "matchEnd")
        XCTAssertEqual(MatchEventKind.periodStart.rawValue, "periodStart")
        XCTAssertEqual(MatchEventKind.periodEnd.rawValue, "periodEnd")
        XCTAssertEqual(MatchEventKind.subIn.rawValue, "subIn")
        XCTAssertEqual(MatchEventKind.subOut.rawValue, "subOut")
        XCTAssertEqual(MatchEventKind.goalFor.rawValue, "goalFor")
        XCTAssertEqual(MatchEventKind.goalAgainst.rawValue, "goalAgainst")
        XCTAssertEqual(MatchEventKind.goalMine.rawValue, "goalMine")
        XCTAssertEqual(MatchEventKind.assist.rawValue, "assist")
        XCTAssertEqual(MatchEventKind.flag.rawValue, "flag")
    }

    func testMatchSessionRoundTripsThroughCodable() throws {
        let recordedAt = Date(timeIntervalSince1970: 1_784_000_000)
        let session = MatchSession(
            recordedAt: recordedAt,
            durationSeconds: 5400,
            locationSamples: [
                LocationSample(timestamp: recordedAt, latitude: 39.33, longitude: -82.10,
                               horizontalAccuracyMeters: 4.2, speedMetersPerSecond: 3.1,
                               courseDegrees: 88.0),
            ],
            heartRateSamples: [
                HeartRateSample(timestamp: recordedAt, beatsPerMinute: 152),
            ],
            events: [
                MatchEvent(kind: .goalMine, date: recordedAt.addingTimeInterval(600), note: "volley"),
                MatchEvent(kind: .subOut, date: recordedAt.addingTimeInterval(1200)),
            ],
            fieldID: UUID(),
            teamCode: "U14-RED",
            playerName: "Ben",
            stats: MatchStats(
                totalDistanceMeters: 6423.5,
                timeOnPitchSeconds: 3120,
                sprintCount: 14,
                runCount: 37,
                workrateScore: 72.4,
                speedZones: SpeedZoneDurations(standingSeconds: 210, walkingSeconds: 890,
                                               joggingSeconds: 1340, runningSeconds: 520,
                                               sprintingSeconds: 160),
                averageHeartRate: 152.3,
                positionRole: .midfielder,
                positionSide: .left
            )
        )

        let encoded = try JSONEncoder().encode(session)
        let decoded = try JSONDecoder().decode(MatchSession.self, from: encoded)

        XCTAssertEqual(decoded.id, session.id)
        XCTAssertEqual(decoded.recordedAt, session.recordedAt)
        XCTAssertEqual(decoded.durationSeconds, session.durationSeconds)
        XCTAssertEqual(decoded.locationSamples, session.locationSamples)
        XCTAssertEqual(decoded.heartRateSamples, session.heartRateSamples)
        XCTAssertEqual(decoded.events, session.events)
        XCTAssertEqual(decoded.fieldID, session.fieldID)
        XCTAssertEqual(decoded.teamCode, session.teamCode)
        XCTAssertEqual(decoded.playerName, session.playerName)
        XCTAssertEqual(decoded.stats, session.stats)
    }

    func testDetectedFieldRoundTripsWithNilOutline() throws {
        let field = DetectedField(
            rectangle: FieldRectangle(centerLatitude: 39.33, centerLongitude: -82.10,
                                      lengthMeters: 100.5, widthMeters: 64.2,
                                      headingDegrees: 12.3),
            source: "inferred",
            confidence: 0.3
        )

        let decoded = try JSONDecoder().decode(
            DetectedField.self, from: JSONEncoder().encode(field))

        XCTAssertEqual(decoded, field)
        XCTAssertNil(decoded.outline)
    }
}
