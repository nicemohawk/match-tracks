import XCTest
@testable import MatchTracksKit

final class SyncTests: XCTestCase {

    func testSessionWirePayloadUsesBackendJSONShape() throws {
        let recordedAt = BackendWire.dateFormatter.date(from: "2026-07-13T18:04:00Z")!
        var session = MatchSession(
            recordedAt: recordedAt,
            durationSeconds: 3120,
            locationSamples: [
                LocationSample(timestamp: recordedAt, latitude: 39.33, longitude: -82.10),
                LocationSample(timestamp: recordedAt.addingTimeInterval(1),
                               latitude: 39.331, longitude: -82.101),
            ],
            events: [
                MatchEvent(kind: .goalMine, date: recordedAt.addingTimeInterval(600),
                           note: "volley"),
            ],
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
        session.fieldID = UUID()

        let payloadData = try JSONEncoder().encode(MatchSessionWirePayload(session: session))
        let json = try XCTUnwrap(
            try JSONSerialization.jsonObject(with: payloadData) as? [String: Any])

        XCTAssertEqual(json["uuid"] as? String, session.id.uuidString.lowercased())
        XCTAssertEqual(json["recorded_at"] as? String, "2026-07-13T18:04:00Z")
        XCTAssertEqual(json["duration_s"] as? Double, 3120)
        XCTAssertEqual(json["field_uuid"] as? String, session.fieldID?.uuidString.lowercased())
        XCTAssertEqual(json["team_code"] as? String, "U14-RED")
        XCTAssertEqual(json["player_name"] as? String, "Ben")

        let track = try XCTUnwrap(json["track"] as? [String: Any])
        let coordinates = try XCTUnwrap(track["coordinates"] as? [[Double]])
        XCTAssertEqual(coordinates.count, 2)
        XCTAssertEqual(coordinates[0], [39.33, -82.10])

        let events = try XCTUnwrap(json["events"] as? [[String: Any]])
        XCTAssertEqual(events.count, 1)
        XCTAssertEqual(events[0]["kind"] as? String, "goalMine")
        XCTAssertEqual(events[0]["date"] as? String, "2026-07-13T18:14:00Z")
        XCTAssertEqual(events[0]["note"] as? String, "volley")

        let stats = try XCTUnwrap(json["stats"] as? [String: Any])
        XCTAssertEqual(stats["total_distance_m"] as? Double, 6423.5)
        XCTAssertEqual(stats["time_on_pitch_s"] as? Double, 3120)
        XCTAssertEqual(stats["sprint_count"] as? Int, 14)
        XCTAssertEqual(stats["workrate_score"] as? Double, 72.4)
        XCTAssertEqual(stats["avg_hr"] as? Double, 152.3)
        XCTAssertEqual(stats["position_role"] as? String, "midfielder")
        XCTAssertEqual(stats["position_side"] as? String, "left")

        let speedZones = try XCTUnwrap(stats["speed_zones"] as? [String: Any])
        XCTAssertEqual(speedZones["standing_s"] as? Double, 210)
        XCTAssertEqual(speedZones["sprinting_s"] as? Double, 160)
    }

    func testNearbyFieldsResponseDecodesIntoDetectedFields() throws {
        let fixture = """
        {"fields": [{
            "uuid": "3f2504e0-4f89-11d3-9a0c-0305e82c3301",
            "name": "West Side Park #2",
            "source": "community",
            "observation_count": 23,
            "confidence": 0.94,
            "distance_m": 412.7,
            "outline": [[39.3291, -82.1015], [39.3299, -82.1013]],
            "rectangle": {
                "center_lat": 39.32945,
                "center_lon": -82.10085,
                "length_m": 100.5,
                "width_m": 64.2,
                "heading_deg": 12.3
            },
            "created_at": "2026-05-02T14:11:00Z"
        }]}
        """

        let response = try JSONDecoder().decode(NearbyFieldsResponse.self,
                                                from: Data(fixture.utf8))
        let detectedFields = response.detectedFields()

        XCTAssertEqual(detectedFields.count, 1)
        let field = try XCTUnwrap(detectedFields.first)
        XCTAssertEqual(field.id, UUID(uuidString: "3f2504e0-4f89-11d3-9a0c-0305e82c3301"))
        XCTAssertEqual(field.name, "West Side Park #2")
        XCTAssertEqual(field.source, "community")
        XCTAssertEqual(field.confidence ?? 0, 0.94, accuracy: 0.0001)
        XCTAssertEqual(field.rectangle.lengthMeters, 100.5, accuracy: 0.0001)
        XCTAssertEqual(field.rectangle.headingDegrees, 12.3, accuracy: 0.0001)
        XCTAssertEqual(field.outline?.count, 2)
    }

    func testTeamStatsResponseDecodes() throws {
        let fixture = """
        {"team_code": "U14-RED", "team_name": "Red Dragons", "players": [{
            "device_id": "abc-123",
            "player_name": "Ben Lachman",
            "matches_played": 12,
            "total_minutes": 640.5,
            "total_distance_m": 74210.0,
            "total_sprints": 168,
            "avg_workrate_score": 68.9,
            "goals": 5,
            "assists": 3
        }]}
        """

        let response = try JSONDecoder().decode(TeamStatsResponse.self, from: Data(fixture.utf8))

        XCTAssertEqual(response.teamCode, "U14-RED")
        XCTAssertEqual(response.teamName, "Red Dragons")
        XCTAssertEqual(response.players.count, 1)
        let player = try XCTUnwrap(response.players.first)
        XCTAssertEqual(player.playerName, "Ben Lachman")
        XCTAssertEqual(player.matchesPlayed, 12)
        XCTAssertEqual(player.goals, 5)
        XCTAssertEqual(player.assists, 3)
        XCTAssertEqual(player.averageWorkrateScore ?? 0, 68.9, accuracy: 0.0001)
    }
}
