import SwiftUI
import MatchTracksKit

/// The live match experience: vertical pages mirroring the native Workout
/// app — controls above, metrics front and center, game actions below.
struct InGameView: View {
    @Environment(WorkoutManager.self) private var workoutManager
    @State private var selectedPage = Page.metrics

    enum Page {
        case controls
        case metrics
        case game
    }

    var body: some View {
        TabView(selection: $selectedPage) {
            ControlsView()
                .tag(Page.controls)
            MetricsView()
                .tag(Page.metrics)
            GamePageView()
                .tag(Page.game)
        }
        .tabViewStyle(.verticalPage)
    }
}

/// Formats an elapsed interval like the native workout timer (1:23:45 / 23:45).
func formattedElapsedTime(_ interval: TimeInterval) -> String {
    let totalSeconds = Int(interval.rounded(.down))
    let hours = totalSeconds / 3600
    let minutes = (totalSeconds % 3600) / 60
    let seconds = totalSeconds % 60
    if hours > 0 {
        return String(format: "%d:%02d:%02d", hours, minutes, seconds)
    }
    return String(format: "%d:%02d", minutes, seconds)
}
