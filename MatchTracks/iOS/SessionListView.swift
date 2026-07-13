import SwiftUI
import MatchTracksKit

/// The match library: newest first, with a live banner while a match runs.
struct SessionListView: View {
    @Environment(SessionStore.self) private var sessionStore
    @Environment(PhoneConnectivityManager.self) private var connectivityManager

    var body: some View {
        NavigationStack {
            Group {
                if sessionStore.sessions.isEmpty {
                    ContentUnavailableView(
                        "No Matches Yet",
                        systemImage: "figure.soccer",
                        description: Text("Record a match on your Apple Watch and it will appear here automatically.")
                    )
                } else {
                    List {
                        if let liveMatch = connectivityManager.liveMatch {
                            liveBanner(liveMatch)
                        }
                        ForEach(sessionStore.sessions) { session in
                            NavigationLink(value: session.id) {
                                SessionRowView(session: session)
                            }
                        }
                        .onDelete { indexSet in
                            for index in indexSet {
                                sessionStore.delete(sessionID: sessionStore.sessions[index].id)
                            }
                        }
                    }
                }
            }
            .navigationTitle("Matches")
            .navigationDestination(for: UUID.self) { sessionID in
                if let session = sessionStore.sessions.first(where: { $0.id == sessionID }) {
                    SessionDetailView(session: session)
                }
            }
            .refreshable {
                sessionStore.reload()
            }
        }
    }

    private func liveBanner(_ liveMatch: LiveMatchState) -> some View {
        HStack {
            Circle()
                .fill(.red)
                .frame(width: 8, height: 8)
            Text("Live: \(formattedDuration(liveMatch.elapsedSeconds))")
                .font(.subheadline.weight(.semibold))
            Spacer()
            Text("\(liveMatch.scoreUs) — \(liveMatch.scoreThem)")
                .font(.subheadline.monospacedDigit())
        }
        .listRowBackground(Color.red.opacity(0.1))
    }
}

struct SessionRowView: View {
    let session: MatchSession

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack {
                Text(session.recordedAt, format: .dateTime.weekday().month().day())
                    .font(.headline)
                Spacer()
                let sessionScore = score(of: session)
                Text("\(sessionScore.us) — \(sessionScore.them)")
                    .font(.headline.monospacedDigit())
            }
            HStack(spacing: 12) {
                Label(formattedDuration(session.durationSeconds ?? 0), systemImage: "clock")
                Label(String(format: "%.1f km",
                             (session.stats?.totalDistanceMeters ?? 0) / 1000),
                      systemImage: "point.bottomleft.forward.to.point.topright.scurvepath")
                if let workrate = session.stats?.workrateScore {
                    Label(String(format: "%.0f", workrate), systemImage: "bolt.fill")
                }
            }
            .font(.caption)
            .foregroundStyle(.secondary)
        }
        .padding(.vertical, 2)
    }
}
