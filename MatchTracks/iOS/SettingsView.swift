import SwiftUI
import MatchTracksKit

/// Player identity, backend connection, and bulk upload.
struct SettingsView: View {
    @Environment(SessionStore.self) private var sessionStore

    @AppStorage(SettingsKeys.playerName) private var playerName = ""
    @AppStorage(SettingsKeys.teamCode) private var teamCode = ""
    @AppStorage(SettingsKeys.backendURL) private var backendURL = SettingsKeys.defaultBackendURL
    @AppStorage(SettingsKeys.apiKey) private var apiKey = ""

    @State private var uploadStatus: String?
    @State private var isUploading = false

    var body: some View {
        NavigationStack {
            Form {
                Section("Player") {
                    TextField("Player name", text: $playerName)
                    TextField("Team code", text: $teamCode)
                        .textInputAutocapitalization(.characters)
                        .autocorrectionDisabled()
                }

                Section("Backend") {
                    TextField("Service URL", text: $backendURL)
                        .keyboardType(.URL)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                    SecureField("API key", text: $apiKey)
                }

                Section {
                    Button {
                        Task { await uploadAllSessions() }
                    } label: {
                        if isUploading {
                            HStack {
                                ProgressView()
                                Text("Uploading…")
                                    .padding(.leading, 6)
                            }
                        } else {
                            Label("Upload All Matches", systemImage: "icloud.and.arrow.up")
                        }
                    }
                    .disabled(isUploading || sessionStore.sessions.isEmpty || apiKey.isEmpty)

                    if let uploadStatus {
                        Text(uploadStatus)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                } header: {
                    Text("Sync")
                } footer: {
                    Text("Uploads every match — including events and stats — to the team stats service.")
                }
            }
            .navigationTitle("Settings")
        }
    }

    private func uploadAllSessions() async {
        guard let backendClient = configuredBackendClient() else {
            uploadStatus = "Enter a service URL and API key first."
            return
        }
        isUploading = true
        defer { isUploading = false }

        // Stamp current identity onto sessions that lack it before upload.
        let sessionsToUpload = sessionStore.sessions.map { session -> MatchSession in
            var stampedSession = session
            if stampedSession.playerName == nil, !playerName.isEmpty {
                stampedSession.playerName = playerName
            }
            if stampedSession.teamCode == nil, !teamCode.isEmpty {
                stampedSession.teamCode = teamCode
            }
            return stampedSession
        }

        do {
            try await backendClient.uploadSessions(sessionsToUpload)
            uploadStatus = "Uploaded \(sessionsToUpload.count) match\(sessionsToUpload.count == 1 ? "" : "es")."
        } catch {
            uploadStatus = "Upload failed: \(error.localizedDescription)"
        }
    }
}
