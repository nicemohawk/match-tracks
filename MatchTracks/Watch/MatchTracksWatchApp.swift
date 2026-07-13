import SwiftUI
import MatchTracksKit

@main
struct MatchTracksWatchApp: App {
    @State private var workoutManager = WorkoutManager()

    var body: some Scene {
        WindowGroup {
            RootView()
                .environment(workoutManager)
        }
    }
}

/// Routes between the start screen, the live in-game experience, and the
/// post-match summary.
struct RootView: View {
    @Environment(WorkoutManager.self) private var workoutManager

    var body: some View {
        switch workoutManager.phase {
        case .idle, .requestingPermissions:
            StartView()
        case .active, .paused:
            InGameView()
        case .summary:
            SummaryView()
        }
    }
}
