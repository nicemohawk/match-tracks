import Foundation
import MatchTracksKit

/// The phone-side session library: persists sessions received from the watch,
/// runs the full analysis pipeline on ingest, and serves the UI.
@Observable
@MainActor
final class SessionStore {

    private(set) var sessions: [MatchSession] = []

    let fieldDatabase: FieldDatabase

    private static var sessionsDirectory: URL {
        let applicationSupport = FileManager.default.urls(for: .applicationSupportDirectory,
                                                          in: .userDomainMask)[0]
        return applicationSupport.appendingPathComponent("Sessions", isDirectory: true)
    }

    init(fieldDatabase: FieldDatabase) {
        self.fieldDatabase = fieldDatabase
        loadAll()
    }

    // MARK: - Ingest & enrichment

    /// Ingest a session file freshly received from the watch: decode, enrich
    /// with field detection + analytics, and persist.
    func ingest(fileURL: URL) {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        guard let data = try? Data(contentsOf: fileURL),
              let receivedSession = try? decoder.decode(MatchSession.self, from: data) else {
            return
        }
        let enrichedSession = enrich(receivedSession)
        save(enrichedSession)
    }

    /// Run the analysis pipeline: field detection, workrate, position, HR.
    func enrich(_ session: MatchSession) -> MatchSession {
        var enrichedSession = session

        // 1. Which field was this played on?
        let trackCoordinates = session.locationSamples.map { [$0.latitude, $0.longitude] }
        switch FieldDetector().detectField(fromTrack: trackCoordinates,
                                           knownFields: fieldDatabase.fields) {
        case .matched(let field):
            enrichedSession.fieldID = field.id
        case .inferred(let rectangle):
            let inferredField = DetectedField(rectangle: rectangle, source: "inferred")
            fieldDatabase.add(inferredField)
            enrichedSession.fieldID = inferredField.id
        case .none:
            break
        }

        // 2. Workrate, distance, zones, runs, playing time.
        var stats = WorkrateAnalyzer.analyzeWorkrate(samples: session.locationSamples,
                                                     events: session.events)

        // 3. Position, when a field is known.
        if let field = fieldDatabase.find(by: enrichedSession.fieldID) {
            let position = PositionClassifier.classifyPosition(
                samples: session.locationSamples,
                field: field.rectangle,
                periods: periodIntervals(from: session))
            stats.positionRole = position.role
            stats.positionSide = position.side
        }

        // 4. Average heart rate.
        if !session.heartRateSamples.isEmpty {
            stats.averageHeartRate = session.heartRateSamples
                .map(\.beatsPerMinute).reduce(0, +) / Double(session.heartRateSamples.count)
        }

        enrichedSession.stats = stats
        return enrichedSession
    }

    /// Match periods derived from periodStart/periodEnd events (with the match
    /// start opening the first period). Falls back to the whole session.
    private func periodIntervals(from session: MatchSession) -> [(start: Date, end: Date)] {
        let sessionEnd = session.recordedAt.addingTimeInterval(
            session.durationSeconds ?? session.locationSamples.last?.timestamp
                .timeIntervalSince(session.recordedAt) ?? 0)

        var intervals: [(start: Date, end: Date)] = []
        var currentPeriodStart: Date? = session.recordedAt
        for event in session.events.sorted(by: { $0.date < $1.date }) {
            switch event.kind {
            case .periodEnd:
                if let periodStart = currentPeriodStart {
                    intervals.append((start: periodStart, end: event.date))
                    currentPeriodStart = nil
                }
            case .periodStart:
                if currentPeriodStart == nil {
                    currentPeriodStart = event.date
                }
            default:
                break
            }
        }
        if let periodStart = currentPeriodStart, sessionEnd > periodStart {
            intervals.append((start: periodStart, end: sessionEnd))
        }
        return intervals
    }

    // MARK: - Persistence

    func save(_ session: MatchSession) {
        if let existingIndex = sessions.firstIndex(where: { $0.id == session.id }) {
            sessions[existingIndex] = session
        } else {
            sessions.append(session)
            sessions.sort { $0.recordedAt > $1.recordedAt }
        }
        persist(session)
    }

    func delete(sessionID: UUID) {
        sessions.removeAll { $0.id == sessionID }
        try? FileManager.default.removeItem(at: fileURL(for: sessionID))
    }

    func reload() {
        loadAll()
    }

    private func fileURL(for sessionID: UUID) -> URL {
        Self.sessionsDirectory.appendingPathComponent(
            "\(sessionID.uuidString.lowercased()).matchsession.json")
    }

    private func persist(_ session: MatchSession) {
        try? FileManager.default.createDirectory(at: Self.sessionsDirectory,
                                                 withIntermediateDirectories: true)
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        if let data = try? encoder.encode(session) {
            try? data.write(to: fileURL(for: session.id), options: .atomic)
        }
    }

    private func loadAll() {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        let fileURLs = (try? FileManager.default.contentsOfDirectory(
            at: Self.sessionsDirectory, includingPropertiesForKeys: nil)) ?? []
        sessions = fileURLs
            .filter { $0.lastPathComponent.hasSuffix(".matchsession.json") }
            .compactMap { url in
                guard let data = try? Data(contentsOf: url) else { return nil }
                return try? decoder.decode(MatchSession.self, from: data)
            }
            .sorted { $0.recordedAt > $1.recordedAt }
    }
}
