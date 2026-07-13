import SwiftUI
import MatchTracksKit

@main
struct MatchTracksApp: App {
    @State private var fieldDatabase: FieldDatabase
    @State private var sessionStore: SessionStore
    @State private var connectivityManager: PhoneConnectivityManager

    init() {
        let fields = FieldDatabase()
        let sessions = SessionStore(fieldDatabase: fields)
        let connectivity = PhoneConnectivityManager()
        connectivity.activate(sessionStore: sessions)
        _fieldDatabase = State(initialValue: fields)
        _sessionStore = State(initialValue: sessions)
        _connectivityManager = State(initialValue: connectivity)
    }

    var body: some Scene {
        WindowGroup {
            MainTabView()
                .environment(fieldDatabase)
                .environment(sessionStore)
                .environment(connectivityManager)
        }
    }
}

struct MainTabView: View {
    var body: some View {
        TabView {
            Tab("Matches", systemImage: "figure.soccer") {
                SessionListView()
            }
            Tab("Fields", systemImage: "sportscourt") {
                FieldsView()
            }
            Tab("Team", systemImage: "person.3") {
                TeamView()
            }
            Tab("Settings", systemImage: "gearshape") {
                SettingsView()
            }
        }
    }
}

// MARK: - Shared helpers

/// Settings keys shared across views.
enum SettingsKeys {
    static let playerName = "playerName"
    static let teamCode = "teamCode"
    static let backendURL = "backendURL"
    static let apiKey = "apiKey"
    static let deviceIdentifier = "deviceIdentifier"

    static let defaultBackendURL = "https://match-tracks.service.nicemohawk.com"
}

/// The configured backend client, or nil when settings are incomplete.
@MainActor
func configuredBackendClient() -> BackendClient? {
    let defaults = UserDefaults.standard
    let urlString = defaults.string(forKey: SettingsKeys.backendURL)
        ?? SettingsKeys.defaultBackendURL
    guard let baseURL = URL(string: urlString),
          let apiKey = defaults.string(forKey: SettingsKeys.apiKey), !apiKey.isEmpty else {
        return nil
    }
    var deviceIdentifier = defaults.string(forKey: SettingsKeys.deviceIdentifier) ?? ""
    if deviceIdentifier.isEmpty {
        deviceIdentifier = UUID().uuidString.lowercased()
        defaults.set(deviceIdentifier, forKey: SettingsKeys.deviceIdentifier)
    }
    return BackendClient(baseURL: baseURL, apiKey: apiKey, deviceIdentifier: deviceIdentifier)
}

/// "1:23:45" / "23:45" style formatting shared by several views.
func formattedDuration(_ interval: TimeInterval) -> String {
    let totalSeconds = Int(interval.rounded(.down))
    let hours = totalSeconds / 3600
    let minutes = (totalSeconds % 3600) / 60
    let seconds = totalSeconds % 60
    if hours > 0 {
        return String(format: "%d:%02d:%02d", hours, minutes, seconds)
    }
    return String(format: "%d:%02d", minutes, seconds)
}

/// Score derived from a session's goal events.
func score(of session: MatchSession) -> (us: Int, them: Int) {
    let us = session.events.filter { $0.kind == .goalFor || $0.kind == .goalMine }.count
    let them = session.events.filter { $0.kind == .goalAgainst }.count
    return (us, them)
}
