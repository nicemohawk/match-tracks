import SwiftUI
import MatchTracksKit

/// The pre-match screen: start a match, see unsynced sessions, set identity.
struct StartView: View {
    @Environment(WorkoutManager.self) private var workoutManager
    @AppStorage("playerName") private var playerName = ""
    @AppStorage("teamCode") private var teamCode = ""
    @State private var unsyncedSessions: [MatchSession] = []

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(spacing: 12) {
                    Button {
                        Task {
                            await workoutManager.startMatch(playerName: playerName,
                                                            teamCode: teamCode)
                        }
                    } label: {
                        Label("Start Match", systemImage: "figure.soccer")
                            .font(.headline)
                            .frame(maxWidth: .infinity, minHeight: 44)
                    }
                    .buttonStyle(.borderedProminent)
                    .tint(.green)
                    .disabled(workoutManager.phase == .requestingPermissions)

                    NavigationLink {
                        identityForm
                    } label: {
                        Label(playerName.isEmpty ? "Set Player" : playerName,
                              systemImage: "person")
                    }

                    if !unsyncedSessions.isEmpty {
                        Section {
                            ForEach(unsyncedSessions) { session in
                                unsyncedRow(for: session)
                            }
                        } header: {
                            Text("Waiting to Sync")
                                .font(.footnote)
                                .foregroundStyle(.secondary)
                                .frame(maxWidth: .infinity, alignment: .leading)
                        }
                    }
                }
            }
            .navigationTitle("Match Tracks")
            .onAppear {
                unsyncedSessions = SessionFileStore.loadAll()
                workoutManager.connectivity.retryPendingTransfers()
            }
        }
    }

    private var identityForm: some View {
        Form {
            TextField("Player name", text: $playerName)
            TextField("Team code", text: $teamCode)
        }
        .navigationTitle("Player")
    }

    private func unsyncedRow(for session: MatchSession) -> some View {
        HStack {
            VStack(alignment: .leading) {
                Text(session.recordedAt, style: .date)
                    .font(.footnote)
                Text(session.recordedAt, style: .time)
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            Button {
                workoutManager.connectivity.transferSessionFile(
                    at: SessionFileStore.fileURL(for: session.id))
            } label: {
                Image(systemName: "arrow.triangle.2.circlepath")
            }
            .buttonStyle(.bordered)
            .frame(width: 44)
        }
    }
}
