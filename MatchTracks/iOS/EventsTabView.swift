import SwiftUI
import MatchTracksKit

/// The event timeline: everything flagged during the match, with editable notes.
struct EventsTabView: View {
    @Environment(SessionStore.self) private var sessionStore

    let session: MatchSession
    @State private var editingNotes: [UUID: String] = [:]

    var body: some View {
        List {
            ForEach(session.events.sorted(by: { $0.date < $1.date })) { event in
                EventRowView(
                    event: event,
                    minute: minuteMark(for: event),
                    note: Binding(
                        get: { editingNotes[event.id] ?? event.note ?? "" },
                        set: { editingNotes[event.id] = $0 }
                    ),
                    commitNote: { saveNote(for: event) }
                )
            }
        }
    }

    private func minuteMark(for event: MatchEvent) -> String {
        let minutes = Int(event.date.timeIntervalSince(session.recordedAt) / 60)
        return "\(max(0, minutes))'"
    }

    private func saveNote(for event: MatchEvent) {
        guard let editedNote = editingNotes[event.id] else { return }
        var updatedSession = session
        guard let index = updatedSession.events.firstIndex(where: { $0.id == event.id }) else {
            return
        }
        updatedSession.events[index].note = editedNote.isEmpty ? nil : editedNote
        sessionStore.save(updatedSession)
    }
}

struct EventRowView: View {
    let event: MatchEvent
    let minute: String
    @Binding var note: String
    let commitNote: () -> Void

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            VStack(spacing: 2) {
                Image(systemName: iconName)
                    .foregroundStyle(iconColor)
                Text(minute)
                    .font(.caption2.monospacedDigit())
                    .foregroundStyle(.secondary)
            }
            .frame(width: 36)

            VStack(alignment: .leading, spacing: 4) {
                HStack {
                    Text(title)
                        .font(.subheadline.weight(.medium))
                    Spacer()
                    Text(event.date, style: .time)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                TextField("Add note…", text: $note)
                    .font(.caption)
                    .textFieldStyle(.plain)
                    .onSubmit(commitNote)
            }
        }
        .padding(.vertical, 2)
    }

    private var title: String {
        switch event.kind {
        case .matchStart: return "Match started"
        case .matchEnd: return "Match ended"
        case .periodStart: return "Period started"
        case .periodEnd: return "Period ended"
        case .subIn: return "Subbed in"
        case .subOut: return "Subbed out"
        case .goalFor: return "Goal — us"
        case .goalAgainst: return "Goal — them"
        case .goalMine: return "My goal"
        case .assist: return "Assist"
        case .flag: return "Flagged moment"
        }
    }

    private var iconName: String {
        switch event.kind {
        case .matchStart, .matchEnd: return "whistle"
        case .periodStart, .periodEnd: return "clock"
        case .subIn: return "arrow.up.left.circle"
        case .subOut: return "arrow.down.right.circle"
        case .goalFor, .goalMine: return "soccerball.inverse"
        case .goalAgainst: return "soccerball"
        case .assist: return "hands.clap"
        case .flag: return "flag.fill"
        }
    }

    private var iconColor: Color {
        switch event.kind {
        case .goalFor, .goalMine: return .green
        case .goalAgainst: return .red
        case .assist: return .teal
        case .flag: return .purple
        case .subIn, .subOut: return .orange
        default: return .secondary
        }
    }
}
