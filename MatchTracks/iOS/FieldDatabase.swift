import Foundation
import MatchTracksKit

/// The local field database: every field this phone knows about, whether
/// detected from a match track, trained on the watch, or fetched from the
/// community field service.
@Observable
@MainActor
final class FieldDatabase {

    private(set) var fields: [DetectedField] = []

    private static var fieldsDirectory: URL {
        let applicationSupport = FileManager.default.urls(for: .applicationSupportDirectory,
                                                          in: .userDomainMask)[0]
        return applicationSupport.appendingPathComponent("Fields", isDirectory: true)
    }

    init() {
        loadAll()
    }

    func find(by id: UUID?) -> DetectedField? {
        guard let id else { return nil }
        return fields.first { $0.id == id }
    }

    func add(_ field: DetectedField) {
        guard find(by: field.id) == nil else { return }
        fields.append(field)
        persist(field)
    }

    /// Merge fetched fields, skipping uuids already known locally.
    func merge(newFields: [DetectedField]) -> Int {
        let unknownFields = newFields.filter { find(by: $0.id) == nil }
        for field in unknownFields {
            add(field)
        }
        return unknownFields.count
    }

    func rename(fieldID: UUID, to newName: String) {
        guard let index = fields.firstIndex(where: { $0.id == fieldID }) else { return }
        fields[index].name = newName
        persist(fields[index])
    }

    func delete(fieldID: UUID) {
        fields.removeAll { $0.id == fieldID }
        try? FileManager.default.removeItem(at: fileURL(for: fieldID))
    }

    // MARK: - Persistence

    private func fileURL(for fieldID: UUID) -> URL {
        Self.fieldsDirectory.appendingPathComponent(
            "\(fieldID.uuidString.lowercased()).field.json")
    }

    private func persist(_ field: DetectedField) {
        try? FileManager.default.createDirectory(at: Self.fieldsDirectory,
                                                 withIntermediateDirectories: true)
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        if let data = try? encoder.encode(field) {
            try? data.write(to: fileURL(for: field.id), options: .atomic)
        }
    }

    private func loadAll() {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        let fileURLs = (try? FileManager.default.contentsOfDirectory(
            at: Self.fieldsDirectory, includingPropertiesForKeys: nil)) ?? []
        fields = fileURLs
            .filter { $0.lastPathComponent.hasSuffix(".field.json") }
            .compactMap { url in
                guard let data = try? Data(contentsOf: url) else { return nil }
                return try? decoder.decode(DetectedField.self, from: data)
            }
    }
}
