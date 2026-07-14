import SwiftUI
import MatchTracksKit

/// Team-wide stats aggregated across every player's uploads.
struct TeamView: View {
    @AppStorage(SettingsKeys.teamCode) private var teamCode = ""

    @State private var teamStats: TeamStatsResponse?
    @State private var loadErrorDescription: String?
    @State private var isLoading = false

    var body: some View {
        NavigationStack {
            Group {
                if teamCode.isEmpty {
                    ContentUnavailableView(
                        "No Team Code",
                        systemImage: "person.3",
                        description: Text("Set your team code in Settings to see team stats.")
                    )
                } else if let teamStats {
                    teamTable(teamStats)
                } else if isLoading {
                    ProgressView("Loading team stats…")
                } else if let loadErrorDescription {
                    ContentUnavailableView(
                        "Couldn't Load Team",
                        systemImage: "exclamationmark.triangle",
                        description: Text(loadErrorDescription)
                    )
                } else {
                    Color.clear
                }
            }
            .navigationTitle(teamStats?.teamName ?? (teamCode.isEmpty ? "Team" : teamCode))
            .toolbar {
                Button {
                    Task { await loadTeamStats() }
                } label: {
                    Label("Refresh", systemImage: "arrow.clockwise")
                }
                .disabled(teamCode.isEmpty || isLoading)
            }
            .task(id: teamCode) {
                guard !teamCode.isEmpty else { return }
                await loadTeamStats()
            }
        }
    }

    private func teamTable(_ stats: TeamStatsResponse) -> some View {
        List {
            ForEach(stats.players) { player in
                VStack(alignment: .leading, spacing: 4) {
                    HStack {
                        Text(player.playerName ?? "Unknown player")
                            .font(.headline)
                        Spacer()
                        Text("\(player.goals) ⚽️  \(player.assists) 🅰️")
                            .font(.subheadline.monospacedDigit())
                    }
                    HStack(spacing: 12) {
                        statBadge("\(player.matchesPlayed)", "matches")
                        statBadge(String(format: "%.0f", player.totalMinutes), "min")
                        statBadge(String(format: "%.1f", player.totalDistanceMeters / 1000), "km")
                        statBadge("\(player.totalSprints)", "sprints")
                        if let workrate = player.averageWorkrateScore {
                            statBadge(String(format: "%.0f", workrate), "workrate")
                        }
                    }
                }
                .padding(.vertical, 2)
            }
        }
        .refreshable {
            await loadTeamStats()
        }
    }

    private func statBadge(_ value: String, _ label: String) -> some View {
        VStack(spacing: 0) {
            Text(value)
                .font(.subheadline.weight(.semibold).monospacedDigit())
            Text(label)
                .font(.caption2)
                .foregroundStyle(.secondary)
        }
    }

    private func loadTeamStats() async {
        guard let backendClient = configuredBackendClient() else {
            loadErrorDescription = "Set the backend API key in Settings."
            return
        }
        isLoading = true
        defer { isLoading = false }
        do {
            teamStats = try await backendClient.teamStats(code: teamCode)
            loadErrorDescription = nil
        } catch {
            teamStats = nil
            loadErrorDescription = error.localizedDescription
        }
    }
}
