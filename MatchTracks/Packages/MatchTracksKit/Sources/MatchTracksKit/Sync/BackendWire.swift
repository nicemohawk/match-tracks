import Foundation

/// Wire formats for the match-tracks backend API (snake_case JSON).
public enum BackendWire {

    /// ISO-8601 with a literal Z suffix at second precision — the timestamp
    /// format the backend stores and echoes.
    public static let dateFormatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyy-MM-dd'T'HH:mm:ss'Z'"
        formatter.timeZone = TimeZone(identifier: "UTC")
        formatter.locale = Locale(identifier: "en_US_POSIX")
        return formatter
    }()
}

/// One session object inside `POST /devices/{id}/sessions/` — the extended
/// payload the upgraded backend understands.
public struct MatchSessionWirePayload: Encodable {

    public struct Track: Encodable {
        public let coordinates: [[Double]]
    }

    public struct Event: Encodable {
        public let uuid: String
        public let kind: String
        public let date: String
        public let note: String?
    }

    public struct SpeedZones: Encodable {
        enum CodingKeys: String, CodingKey {
            case standingSeconds = "standing_s"
            case walkingSeconds = "walking_s"
            case joggingSeconds = "jogging_s"
            case runningSeconds = "running_s"
            case sprintingSeconds = "sprinting_s"
        }

        public let standingSeconds: Double
        public let walkingSeconds: Double
        public let joggingSeconds: Double
        public let runningSeconds: Double
        public let sprintingSeconds: Double
    }

    public struct Stats: Encodable {
        enum CodingKeys: String, CodingKey {
            case totalDistanceMeters = "total_distance_m"
            case timeOnPitchSeconds = "time_on_pitch_s"
            case sprintCount = "sprint_count"
            case runCount = "run_count"
            case workrateScore = "workrate_score"
            case speedZones = "speed_zones"
            case averageHeartRate = "avg_hr"
            case positionRole = "position_role"
            case positionSide = "position_side"
        }

        public let totalDistanceMeters: Double?
        public let timeOnPitchSeconds: Double?
        public let sprintCount: Int?
        public let runCount: Int?
        public let workrateScore: Double?
        public let speedZones: SpeedZones?
        public let averageHeartRate: Double?
        public let positionRole: String?
        public let positionSide: String?
    }

    enum CodingKeys: String, CodingKey {
        case uuid
        case recordedAt = "recorded_at"
        case track
        case durationSeconds = "duration_s"
        case fieldUUID = "field_uuid"
        case teamCode = "team_code"
        case playerName = "player_name"
        case events
        case stats
    }

    public let uuid: String
    public let recordedAt: String
    public let track: Track
    public let durationSeconds: Double?
    public let fieldUUID: String?
    public let teamCode: String?
    public let playerName: String?
    public let events: [Event]
    public let stats: Stats?

    public init(session: MatchSession) {
        uuid = session.id.uuidString.lowercased()
        recordedAt = BackendWire.dateFormatter.string(from: session.recordedAt)
        track = Track(coordinates: session.locationSamples.map { [$0.latitude, $0.longitude] })
        durationSeconds = session.durationSeconds
        fieldUUID = session.fieldID?.uuidString.lowercased()
        teamCode = session.teamCode
        playerName = session.playerName
        events = session.events.map { event in
            Event(uuid: event.id.uuidString.lowercased(),
                  kind: event.kind.rawValue,
                  date: BackendWire.dateFormatter.string(from: event.date),
                  note: event.note)
        }
        stats = session.stats.map { sessionStats in
            Stats(totalDistanceMeters: sessionStats.totalDistanceMeters,
                  timeOnPitchSeconds: sessionStats.timeOnPitchSeconds,
                  sprintCount: sessionStats.sprintCount,
                  runCount: sessionStats.runCount,
                  workrateScore: sessionStats.workrateScore,
                  speedZones: sessionStats.speedZones.map {
                      SpeedZones(standingSeconds: $0.standingSeconds,
                                 walkingSeconds: $0.walkingSeconds,
                                 joggingSeconds: $0.joggingSeconds,
                                 runningSeconds: $0.runningSeconds,
                                 sprintingSeconds: $0.sprintingSeconds)
                  },
                  averageHeartRate: sessionStats.averageHeartRate,
                  positionRole: sessionStats.positionRole?.rawValue,
                  positionSide: sessionStats.positionSide?.rawValue)
        }
    }
}

/// Response of `GET /fields/nearby`.
public struct NearbyFieldsResponse: Decodable {

    public struct Field: Decodable {
        public struct Rectangle: Decodable {
            enum CodingKeys: String, CodingKey {
                case centerLatitude = "center_lat"
                case centerLongitude = "center_lon"
                case lengthMeters = "length_m"
                case widthMeters = "width_m"
                case headingDegrees = "heading_deg"
            }

            public let centerLatitude: Double
            public let centerLongitude: Double
            public let lengthMeters: Double
            public let widthMeters: Double
            public let headingDegrees: Double
        }

        enum CodingKeys: String, CodingKey {
            case uuid
            case name
            case source
            case observationCount = "observation_count"
            case confidence
            case distanceMeters = "distance_m"
            case outline
            case rectangle
        }

        public let uuid: String
        public let name: String?
        public let source: String
        public let observationCount: Int?
        public let confidence: Double?
        public let distanceMeters: Double?
        public let outline: [[Double]]?
        public let rectangle: Rectangle
    }

    public let fields: [Field]

    /// Map the wire fields into local `DetectedField` values, skipping any
    /// whose uuid does not parse.
    public func detectedFields() -> [DetectedField] {
        fields.compactMap { field in
            guard let id = UUID(uuidString: field.uuid) else { return nil }
            return DetectedField(
                id: id,
                name: field.name,
                outline: field.outline,
                rectangle: FieldRectangle(centerLatitude: field.rectangle.centerLatitude,
                                          centerLongitude: field.rectangle.centerLongitude,
                                          lengthMeters: field.rectangle.lengthMeters,
                                          widthMeters: field.rectangle.widthMeters,
                                          headingDegrees: field.rectangle.headingDegrees),
                source: field.source,
                confidence: field.confidence)
        }
    }
}

/// Response of `GET /teams/{code}/stats`.
public struct TeamStatsResponse: Decodable {

    public struct Player: Decodable, Identifiable {
        enum CodingKeys: String, CodingKey {
            case deviceID = "device_id"
            case playerName = "player_name"
            case matchesPlayed = "matches_played"
            case totalMinutes = "total_minutes"
            case totalDistanceMeters = "total_distance_m"
            case totalSprints = "total_sprints"
            case averageWorkrateScore = "avg_workrate_score"
            case goals
            case assists
        }

        public var id: String { deviceID }

        public let deviceID: String
        public let playerName: String?
        public let matchesPlayed: Int
        public let totalMinutes: Double
        public let totalDistanceMeters: Double
        public let totalSprints: Int
        public let averageWorkrateScore: Double?
        public let goals: Int
        public let assists: Int
    }

    enum CodingKeys: String, CodingKey {
        case teamCode = "team_code"
        case teamName = "team_name"
        case players
    }

    public let teamCode: String
    public let teamName: String?
    public let players: [Player]
}
