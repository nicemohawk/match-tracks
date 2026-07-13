import SwiftUI
import MatchTracksKit

/// The center metrics page: elapsed time, distance, heart rate, speed — plus
/// the marquee interaction, a Flag button wired to the system Double Tap
/// gesture for one-handed moment marking mid-game.
struct MetricsView: View {
    @Environment(WorkoutManager.self) private var workoutManager
    @Environment(\.isLuminanceReduced) private var isLuminanceReduced

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(formattedElapsedTime(workoutManager.elapsedSeconds))
                .font(.system(size: 40, weight: .semibold, design: .rounded))
                .monospacedDigit()
                .foregroundStyle(.yellow)

            metricRow(value: distanceText, unit: "KM")
            metricRow(value: heartRateText, unit: "BPM",
                      symbol: "heart.fill", symbolColor: .red)
            metricRow(value: speedText, unit: "KM/H")

            Spacer(minLength: 4)

            Button {
                workoutManager.addEvent(.flag)
            } label: {
                Label("Flag Moment", systemImage: "flag.fill")
                    .font(.headline)
                    .frame(maxWidth: .infinity, minHeight: 36)
            }
            .buttonStyle(.borderedProminent)
            .tint(isLuminanceReduced ? .gray : .purple)
            .handGestureShortcut(.primaryAction)
        }
        .padding(.horizontal, 6)
        .navigationTitle(workoutManager.phase == .paused ? "Paused" : "Match")
    }

    private var distanceText: String {
        String(format: "%.2f", workoutManager.distanceMeters / 1000)
    }

    private var heartRateText: String {
        workoutManager.heartRate > 0 ? String(format: "%.0f", workoutManager.heartRate) : "--"
    }

    private var speedText: String {
        String(format: "%.1f", workoutManager.currentSpeedMetersPerSecond * 3.6)
    }

    private func metricRow(value: String, unit: String,
                           symbol: String? = nil, symbolColor: Color = .primary) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 4) {
            Text(value)
                .font(.system(size: 28, weight: .medium, design: .rounded))
                .monospacedDigit()
            Text(unit)
                .font(.caption)
                .foregroundStyle(.secondary)
            if let symbol {
                Image(systemName: symbol)
                    .font(.caption)
                    .foregroundStyle(isLuminanceReduced ? AnyShapeStyle(.secondary)
                                                        : AnyShapeStyle(symbolColor))
            }
        }
    }
}
