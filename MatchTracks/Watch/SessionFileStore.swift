import Foundation
import MatchTracksKit

/// Persists completed match sessions as JSON files in Documents/Sessions/.
///
/// Files use ISO-8601 dates and are named "<uuid>.matchsession.json" — the
/// same file is what gets transferred to the phone, so a failed transfer can
/// always be retried from disk.
enum SessionFileStore {

    static var sessionsDirectory: URL {
        let documents = FileManager.default.urls(for: .documentDirectory,
                                                 in: .userDomainMask)[0]
        return documents.appendingPathComponent("Sessions", isDirectory: true)
    }

    static func fileURL(for sessionID: UUID) -> URL {
        sessionsDirectory.appendingPathComponent(
            "\(sessionID.uuidString.lowercased()).matchsession.json")
    }

    @discardableResult
    static func save(_ session: MatchSession) throws -> URL {
        try FileManager.default.createDirectory(at: sessionsDirectory,
                                                withIntermediateDirectories: true)
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        let data = try encoder.encode(session)
        let url = fileURL(for: session.id)
        try data.write(to: url, options: .atomic)
        return url
    }

    static func loadAll() -> [MatchSession] {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        let fileURLs = (try? FileManager.default.contentsOfDirectory(
            at: sessionsDirectory, includingPropertiesForKeys: nil)) ?? []
        return fileURLs
            .filter { $0.lastPathComponent.hasSuffix(".matchsession.json") }
            .compactMap { url in
                guard let data = try? Data(contentsOf: url) else { return nil }
                return try? decoder.decode(MatchSession.self, from: data)
            }
            .sorted { $0.recordedAt > $1.recordedAt }
    }

    static func delete(sessionID: UUID) {
        try? FileManager.default.removeItem(at: fileURL(for: sessionID))
    }
}
