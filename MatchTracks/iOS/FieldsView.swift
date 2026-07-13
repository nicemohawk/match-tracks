import SwiftUI
import MatchTracksKit

/// The local field database, plus fetching community fields from the backend.
struct FieldsView: View {
    @Environment(FieldDatabase.self) private var fieldDatabase
    @Environment(SessionStore.self) private var sessionStore

    @State private var renamingField: DetectedField?
    @State private var newFieldName = ""
    @State private var fetchStatus: String?
    @State private var isFetching = false

    var body: some View {
        NavigationStack {
            Group {
                if fieldDatabase.fields.isEmpty {
                    ContentUnavailableView(
                        "No Fields Yet",
                        systemImage: "sportscourt",
                        description: Text("Fields appear automatically when matches are analyzed, or fetch community fields trained by other players.")
                    )
                } else {
                    List {
                        ForEach(fieldDatabase.fields) { field in
                            fieldRow(field)
                        }
                    }
                }
            }
            .navigationTitle("Fields")
            .toolbar {
                Button {
                    Task { await fetchNearbyFields() }
                } label: {
                    if isFetching {
                        ProgressView()
                    } else {
                        Label("Fetch Nearby", systemImage: "icloud.and.arrow.down")
                    }
                }
                .disabled(isFetching || referenceCoordinate == nil)
            }
            .alert("Rename Field", isPresented: Binding(
                get: { renamingField != nil },
                set: { if !$0 { renamingField = nil } }
            )) {
                TextField("Field name", text: $newFieldName)
                Button("Save") {
                    if let field = renamingField {
                        fieldDatabase.rename(fieldID: field.id, to: newFieldName)
                    }
                    renamingField = nil
                }
                Button("Cancel", role: .cancel) { renamingField = nil }
            }
            .overlay(alignment: .bottom) {
                if let fetchStatus {
                    Text(fetchStatus)
                        .font(.caption)
                        .padding(8)
                        .background(.thinMaterial, in: Capsule())
                        .padding(.bottom, 8)
                }
            }
        }
    }

    private func fieldRow(_ field: DetectedField) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(field.name ?? "Unnamed field")
                .font(.headline)
            HStack(spacing: 8) {
                Text(field.source.capitalized)
                    .font(.caption2.weight(.semibold))
                    .padding(.horizontal, 6)
                    .padding(.vertical, 1)
                    .background(sourceColor(field.source).opacity(0.2), in: Capsule())
                    .foregroundStyle(sourceColor(field.source))
                if let confidence = field.confidence {
                    Text(String(format: "%.0f%% confidence", confidence * 100))
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Text(String(format: "%.0f × %.0f m",
                            field.rectangle.lengthMeters, field.rectangle.widthMeters))
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .swipeActions {
            Button(role: .destructive) {
                fieldDatabase.delete(fieldID: field.id)
            } label: {
                Label("Delete", systemImage: "trash")
            }
            Button {
                newFieldName = field.name ?? ""
                renamingField = field
            } label: {
                Label("Rename", systemImage: "pencil")
            }
        }
    }

    private func sourceColor(_ source: String) -> Color {
        switch source {
        case "trained": return .blue
        case "community": return .purple
        default: return .gray
        }
    }

    /// Where to search for nearby fields: the most recent session's start.
    private var referenceCoordinate: (latitude: Double, longitude: Double)? {
        guard let sample = sessionStore.sessions.first?.locationSamples.first else {
            return nil
        }
        return (sample.latitude, sample.longitude)
    }

    private func fetchNearbyFields() async {
        guard let coordinate = referenceCoordinate else { return }
        guard let backendClient = configuredBackendClient() else {
            fetchStatus = "Set an API key in Settings first."
            return
        }
        isFetching = true
        defer { isFetching = false }
        do {
            let fetchedFields = try await backendClient.nearbyFields(
                latitude: coordinate.latitude, longitude: coordinate.longitude,
                radiusMeters: 5000)
            let addedCount = fieldDatabase.merge(newFields: fetchedFields)
            fetchStatus = addedCount > 0
                ? "Added \(addedCount) community field\(addedCount == 1 ? "" : "s")."
                : "No new fields nearby."
        } catch {
            fetchStatus = "Fetch failed: \(error.localizedDescription)"
        }
    }
}
