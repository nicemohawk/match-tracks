import SwiftUI
import MatchTracksKit

/// The match split into individual runs, sprints highlighted.
struct RunsTabView: View {
    let session: MatchSession

    private var runs: [Run] {
        RunSegmenter.segmentRuns(from: session.locationSamples)
    }

    var body: some View {
        let segmentedRuns = runs
        if segmentedRuns.isEmpty {
            ContentUnavailableView(
                "No Runs Detected",
                systemImage: "figure.run",
                description: Text("Runs appear when the match track contains stretches faster than a jog.")
            )
        } else {
            List {
                Section {
                    ForEach(Array(segmentedRuns.enumerated()), id: \.offset) { index, run in
                        RunRowView(index: index + 1, run: run)
                    }
                } header: {
                    let sprintCount = segmentedRuns.filter(\.isSprint).count
                    Text("\(segmentedRuns.count) runs · \(sprintCount) sprints")
                }
            }
        }
    }
}

struct RunRowView: View {
    let index: Int
    let run: Run

    var body: some View {
        HStack {
            Text("\(index)")
                .font(.headline.monospacedDigit())
                .foregroundStyle(.secondary)
                .frame(width: 28)
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 6) {
                    Text(run.startDate, style: .time)
                        .font(.subheadline.weight(.medium))
                    if run.isSprint {
                        Label("Sprint", systemImage: "bolt.fill")
                            .font(.caption2.weight(.semibold))
                            .padding(.horizontal, 6)
                            .padding(.vertical, 1)
                            .background(.orange.opacity(0.2), in: Capsule())
                            .foregroundStyle(.orange)
                    }
                }
                Text("\(formattedDuration(run.endDate.timeIntervalSince(run.startDate))) · "
                     + String(format: "%.0f m", run.distanceMeters))
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            VStack(alignment: .trailing) {
                Text(String(format: "%.1f", run.peakSpeedMetersPerSecond * 3.6))
                    .font(.headline.monospacedDigit())
                Text("km/h peak")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
        }
    }
}
