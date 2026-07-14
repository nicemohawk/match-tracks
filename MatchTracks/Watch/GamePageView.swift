import SwiftUI
import MatchTracksKit

/// The game page: score keeping and personal stat flagging with undo.
struct GamePageView: View {
    @Environment(WorkoutManager.self) private var workoutManager

    var body: some View {
        ScrollView {
            VStack(spacing: 10) {
                scoreBoard

                HStack(spacing: 8) {
                    scoreButton(label: "Us +1", kind: .goalFor, tint: .green)
                    scoreButton(label: "Them +1", kind: .goalAgainst, tint: .red)
                }

                HStack(spacing: 8) {
                    scoreButton(label: "My Goal", kind: .goalMine, tint: .mint)
                    scoreButton(label: "Assist", kind: .assist, tint: .teal)
                }

                if let lastEvent = workoutManager.lastEvent {
                    HStack {
                        VStack(alignment: .leading) {
                            Text(eventLabel(for: lastEvent.kind))
                                .font(.footnote)
                            Text(lastEvent.date, style: .time)
                                .font(.caption2)
                                .foregroundStyle(.secondary)
                        }
                        Spacer()
                        if lastEvent.kind != .matchStart {
                            Button("Undo") {
                                workoutManager.undoLastEvent()
                            }
                            .buttonStyle(.bordered)
                            .font(.caption)
                            .frame(width: 60)
                        }
                    }
                    .padding(.top, 2)
                }
            }
            .padding(.horizontal, 4)
        }
        .navigationTitle("Game")
    }

    private var scoreBoard: some View {
        HStack {
            VStack {
                Text("US")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                Text("\(workoutManager.scoreUs)")
                    .font(.system(size: 34, weight: .bold, design: .rounded))
                    .monospacedDigit()
            }
            .frame(maxWidth: .infinity)
            Text("—")
                .font(.title3)
                .foregroundStyle(.secondary)
            VStack {
                Text("THEM")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                Text("\(workoutManager.scoreThem)")
                    .font(.system(size: 34, weight: .bold, design: .rounded))
                    .monospacedDigit()
            }
            .frame(maxWidth: .infinity)
        }
    }

    private func scoreButton(label: String, kind: MatchEventKind, tint: Color) -> some View {
        Button(label) {
            workoutManager.addEvent(kind)
        }
        .buttonStyle(.borderedProminent)
        .tint(tint.opacity(0.85))
        .font(.footnote)
    }

    private func eventLabel(for kind: MatchEventKind) -> String {
        switch kind {
        case .matchStart: return "Match started"
        case .matchEnd: return "Match ended"
        case .periodStart: return "Half started"
        case .periodEnd: return "Half ended"
        case .subIn: return "Subbed in"
        case .subOut: return "Subbed out"
        case .goalFor: return "Goal — us"
        case .goalAgainst: return "Goal — them"
        case .goalMine: return "My goal!"
        case .assist: return "Assist"
        case .flag: return "Flagged moment"
        }
    }
}
