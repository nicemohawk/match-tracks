import SwiftUI
import Charts
import MatchTracksKit

/// Workrate, speed zones, playing time, and position for one match.
struct StatsTabView: View {
    let session: MatchSession

    private var stats: MatchStats? { session.stats }

    var body: some View {
        List {
            if let stats {
                workrateSection(stats)
                speedZoneSection(stats)
                numbersSection(stats)
            } else {
                ContentUnavailableView("No Stats", systemImage: "chart.bar",
                                       description: Text("This session has not been analyzed yet."))
            }
        }
    }

    private func workrateSection(_ stats: MatchStats) -> some View {
        Section("Workrate") {
            HStack {
                Gauge(value: stats.workrateScore ?? 0, in: 0...100) {
                    Text("Workrate")
                } currentValueLabel: {
                    Text(String(format: "%.0f", stats.workrateScore ?? 0))
                        .font(.title2.weight(.bold))
                }
                .gaugeStyle(.accessoryCircular)
                .tint(workrateTint(stats.workrateScore ?? 0))
                .frame(width: 80, height: 80)

                VStack(alignment: .leading, spacing: 4) {
                    if let role = stats.positionRole {
                        Text(positionDescription(role: role, side: stats.positionSide))
                            .font(.headline)
                    }
                    Text(workrateDescription(stats.workrateScore ?? 0))
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                .padding(.leading, 8)
            }
            .padding(.vertical, 4)
        }
    }

    private func speedZoneSection(_ stats: MatchStats) -> some View {
        Section("Speed Zones") {
            if let zones = stats.speedZones {
                Chart {
                    ForEach(zoneEntries(zones), id: \.name) { entry in
                        BarMark(x: .value("Minutes", entry.minutes),
                                y: .value("Zone", entry.name))
                        .foregroundStyle(entry.color)
                    }
                }
                .chartXAxisLabel("minutes")
                .frame(height: 180)
                .padding(.vertical, 4)
            }
        }
    }

    private func numbersSection(_ stats: MatchStats) -> some View {
        Section("Numbers") {
            statRow("Distance", String(format: "%.2f km", (stats.totalDistanceMeters ?? 0) / 1000))
            statRow("Time on Pitch", formattedDuration(stats.timeOnPitchSeconds ?? 0))
            if let duration = session.durationSeconds {
                let benchSeconds = max(0, duration - (stats.timeOnPitchSeconds ?? duration))
                statRow("On Bench", formattedDuration(benchSeconds))
            }
            statRow("Runs", "\(stats.runCount ?? 0)")
            statRow("Sprints", "\(stats.sprintCount ?? 0)")
            if let averageHeartRate = stats.averageHeartRate {
                statRow("Avg Heart Rate", String(format: "%.0f bpm", averageHeartRate))
            }
        }
    }

    private func statRow(_ title: String, _ value: String) -> some View {
        HStack {
            Text(title)
            Spacer()
            Text(value)
                .foregroundStyle(.secondary)
                .monospacedDigit()
        }
    }

    private struct ZoneEntry {
        let name: String
        let minutes: Double
        let color: Color
    }

    private func zoneEntries(_ zones: SpeedZoneDurations) -> [ZoneEntry] {
        [
            ZoneEntry(name: "Sprinting", minutes: zones.sprintingSeconds / 60, color: .red),
            ZoneEntry(name: "Running", minutes: zones.runningSeconds / 60, color: .orange),
            ZoneEntry(name: "Jogging", minutes: zones.joggingSeconds / 60, color: .yellow),
            ZoneEntry(name: "Walking", minutes: zones.walkingSeconds / 60, color: .green),
            ZoneEntry(name: "Standing", minutes: zones.standingSeconds / 60, color: .gray),
        ]
    }

    private func workrateTint(_ score: Double) -> Color {
        switch score {
        case ..<40: return .gray
        case ..<60: return .yellow
        case ..<80: return .orange
        default: return .red
        }
    }

    private func workrateDescription(_ score: Double) -> String {
        switch score {
        case ..<40: return "Light session — mostly walking."
        case ..<60: return "Moderate work — steady movement with some running."
        case ..<80: return "Strong work rate — sustained running and sprints."
        default: return "Elite engine — relentless running all match."
        }
    }

    private func positionDescription(role: PositionRole, side: PositionSide?) -> String {
        let sidePrefix: String
        switch side {
        case .left: sidePrefix = "Left "
        case .right: sidePrefix = "Right "
        case .center, .none: sidePrefix = ""
        }
        let roleName: String
        switch role {
        case .goalkeeper: roleName = "Goalkeeper"
        case .defender: roleName = "Defender"
        case .midfielder: roleName = "Midfielder"
        case .forward: roleName = "Forward"
        }
        return sidePrefix + roleName
    }
}
