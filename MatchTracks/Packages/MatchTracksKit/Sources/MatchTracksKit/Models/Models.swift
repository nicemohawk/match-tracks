import Foundation

/// The kind of a match event recorded during play.
///
/// Raw values are the wire strings understood by the match-tracks backend —
/// do not rename cases without a coordinated server change.
public enum MatchEventKind: String, Codable, CaseIterable, Sendable {
    case matchStart
    case matchEnd
    case periodStart
    case periodEnd
    case subIn
    case subOut
    case goalFor
    case goalAgainst
    case goalMine
    case assist
    case flag
}

/// A single timestamped event flagged during a match (goal, substitution,
/// generic flag for post-game review, and so on).
public struct MatchEvent: Codable, Identifiable, Hashable, Sendable {
    public var id: UUID
    public var kind: MatchEventKind
    public var date: Date
    public var note: String?

    public init(id: UUID = UUID(), kind: MatchEventKind, date: Date, note: String? = nil) {
        self.id = id
        self.kind = kind
        self.date = date
        self.note = note
    }
}

/// One GPS breadcrumb recorded during a session.
public struct LocationSample: Codable, Hashable, Sendable {
    public var timestamp: Date
    public var latitude: Double
    public var longitude: Double
    public var horizontalAccuracyMeters: Double
    public var speedMetersPerSecond: Double
    public var courseDegrees: Double

    public init(timestamp: Date, latitude: Double, longitude: Double,
                horizontalAccuracyMeters: Double = 5.0,
                speedMetersPerSecond: Double = 0.0,
                courseDegrees: Double = 0.0) {
        self.timestamp = timestamp
        self.latitude = latitude
        self.longitude = longitude
        self.horizontalAccuracyMeters = horizontalAccuracyMeters
        self.speedMetersPerSecond = speedMetersPerSecond
        self.courseDegrees = courseDegrees
    }
}

/// One heart-rate reading recorded during a session.
public struct HeartRateSample: Codable, Hashable, Sendable {
    public var timestamp: Date
    public var beatsPerMinute: Double

    public init(timestamp: Date, beatsPerMinute: Double) {
        self.timestamp = timestamp
        self.beatsPerMinute = beatsPerMinute
    }
}

/// Cumulative time spent in each speed band during a match.
public struct SpeedZoneDurations: Codable, Hashable, Sendable {
    public var standingSeconds: Double
    public var walkingSeconds: Double
    public var joggingSeconds: Double
    public var runningSeconds: Double
    public var sprintingSeconds: Double

    public init(standingSeconds: Double = 0, walkingSeconds: Double = 0,
                joggingSeconds: Double = 0, runningSeconds: Double = 0,
                sprintingSeconds: Double = 0) {
        self.standingSeconds = standingSeconds
        self.walkingSeconds = walkingSeconds
        self.joggingSeconds = joggingSeconds
        self.runningSeconds = runningSeconds
        self.sprintingSeconds = sprintingSeconds
    }
}

/// Broad positional role inferred from where the player spent the match.
public enum PositionRole: String, Codable, Sendable {
    case goalkeeper
    case defender
    case midfielder
    case forward
}

/// Lateral side of the pitch the player favored.
public enum PositionSide: String, Codable, Sendable {
    case left
    case center
    case right
}

/// Post-game summary statistics for one player's match.
/// Every field is optional: partial data (for example, no heart rate) is normal.
public struct MatchStats: Codable, Hashable, Sendable {
    public var totalDistanceMeters: Double?
    public var timeOnPitchSeconds: Double?
    public var sprintCount: Int?
    public var runCount: Int?
    public var workrateScore: Double?
    public var speedZones: SpeedZoneDurations?
    public var averageHeartRate: Double?
    public var positionRole: PositionRole?
    public var positionSide: PositionSide?

    public init(totalDistanceMeters: Double? = nil, timeOnPitchSeconds: Double? = nil,
                sprintCount: Int? = nil, runCount: Int? = nil,
                workrateScore: Double? = nil, speedZones: SpeedZoneDurations? = nil,
                averageHeartRate: Double? = nil, positionRole: PositionRole? = nil,
                positionSide: PositionSide? = nil) {
        self.totalDistanceMeters = totalDistanceMeters
        self.timeOnPitchSeconds = timeOnPitchSeconds
        self.sprintCount = sprintCount
        self.runCount = runCount
        self.workrateScore = workrateScore
        self.speedZones = speedZones
        self.averageHeartRate = averageHeartRate
        self.positionRole = positionRole
        self.positionSide = positionSide
    }
}

/// The oriented rectangle describing a field's touchlines.
///
/// `headingDegrees` is the compass bearing of the long axis folded into
/// [0, 180) — a pitch reads the same walked in either direction.
public struct FieldRectangle: Codable, Hashable, Sendable {
    public var centerLatitude: Double
    public var centerLongitude: Double
    public var lengthMeters: Double
    public var widthMeters: Double
    public var headingDegrees: Double

    public init(centerLatitude: Double, centerLongitude: Double,
                lengthMeters: Double, widthMeters: Double, headingDegrees: Double) {
        self.centerLatitude = centerLatitude
        self.centerLongitude = centerLongitude
        self.lengthMeters = lengthMeters
        self.widthMeters = widthMeters
        self.headingDegrees = headingDegrees
    }
}

/// A field known to the local database (trained by walking the touchlines,
/// inferred from match tracks, or fetched from the community field service).
public struct DetectedField: Codable, Identifiable, Hashable, Sendable {
    public var id: UUID
    public var name: String?
    /// Raw walked outline as `[latitude, longitude]` pairs; nil for fields
    /// inferred purely from match tracks.
    public var outline: [[Double]]?
    public var rectangle: FieldRectangle
    /// Provenance: "trained", "inferred", or "community".
    public var source: String
    public var confidence: Double?

    public init(id: UUID = UUID(), name: String? = nil, outline: [[Double]]? = nil,
                rectangle: FieldRectangle, source: String, confidence: Double? = nil) {
        self.id = id
        self.name = name
        self.outline = outline
        self.rectangle = rectangle
        self.source = source
        self.confidence = confidence
    }
}

/// A complete recorded match session: the GPS track, heart rate trail,
/// flagged events, and (once computed) summary statistics.
public struct MatchSession: Codable, Identifiable, Sendable {
    public var id: UUID
    public var recordedAt: Date
    public var durationSeconds: Double?
    public var locationSamples: [LocationSample]
    public var heartRateSamples: [HeartRateSample]
    public var events: [MatchEvent]
    public var fieldID: UUID?
    public var teamCode: String?
    public var playerName: String?
    public var stats: MatchStats?

    public init(id: UUID = UUID(), recordedAt: Date, durationSeconds: Double? = nil,
                locationSamples: [LocationSample] = [],
                heartRateSamples: [HeartRateSample] = [],
                events: [MatchEvent] = [], fieldID: UUID? = nil,
                teamCode: String? = nil, playerName: String? = nil,
                stats: MatchStats? = nil) {
        self.id = id
        self.recordedAt = recordedAt
        self.durationSeconds = durationSeconds
        self.locationSamples = locationSamples
        self.heartRateSamples = heartRateSamples
        self.events = events
        self.fieldID = fieldID
        self.teamCode = teamCode
        self.playerName = playerName
        self.stats = stats
    }
}
