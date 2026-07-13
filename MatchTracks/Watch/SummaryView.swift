import SwiftUI
import MatchTracksKit

/// Post-match summary shown after ending a match, native-workout style.
struct SummaryView: View {
    @Environment(WorkoutManager.self) private var workoutManager

    var body: some View {
        NavigationStack {
            ScrollView {
                if let session = workoutManager.completedSession {
                    VStack(alignment: .leading, spacing: 8) {
                        summaryRow(title: "Score",
                                   value: "\(score(of: session).us) — \(score(of: session).them)")
                        summaryRow(title: "Duration",
                                   value: formattedElapsedTime(session.durationSeconds ?? 0))
                        summaryRow(title: "Distance",
                                   value: String(format: "%.2f km",
                                                 (session.stats?.totalDistanceMeters ?? 0) / 1000))
                        summaryRow(title: "Time on Pitch",
                                   value: formattedElapsedTime(session.stats?.timeOnPitchSeconds ?? 0))
                        if let averageHeartRate = session.stats?.averageHeartRate {
                            summaryRow(title: "Avg Heart Rate",
                                       value: String(format: "%.0f bpm", averageHeartRate))
                        }
                        summaryRow(title: "Runs / Sprints",
                                   value: "\(session.stats?.runCount ?? 0) / \(session.stats?.sprintCount ?? 0)")
                        summaryRow(title: "Workrate",
                                   value: String(format: "%.0f", session.stats?.workrateScore ?? 0))
                        summaryRow(title: "Events", value: "\(session.events.count)")

                        Button("Done") {
                            workoutManager.dismissSummary()
                        }
                        .buttonStyle(.borderedProminent)
                        .tint(.green)
                        .padding(.top, 6)
                    }
                    .padding(.horizontal, 6)
                } else {
                    Button("Done") {
                        workoutManager.dismissSummary()
                    }
                }
            }
            .navigationTitle("Summary")
        }
    }

    private func score(of session: MatchSession) -> (us: Int, them: Int) {
        let us = session.events.filter { $0.kind == .goalFor || $0.kind == .goalMine }.count
        let them = session.events.filter { $0.kind == .goalAgainst }.count
        return (us, them)
    }

    private func summaryRow(title: String, value: String) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            Text(title)
                .font(.caption2)
                .foregroundStyle(.secondary)
            Text(value)
                .font(.system(.title3, design: .rounded, weight: .semibold))
                .monospacedDigit()
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}
