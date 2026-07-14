import Foundation

/// Derives on-pitch playing time from substitution events.
public enum PlayingTimeCalculator {

    /// The chronological intervals during which the player was on the pitch.
    ///
    /// Rule: if the first substitution event is `subIn`, the player started
    /// the session off the pitch; if it is `subOut`, or there are no
    /// substitution events at all, the player started on. Substitution events
    /// then toggle the state chronologically; an interval still open at the
    /// end closes at `sessionEnd`. Non-substitution events are ignored.
    public static func onPitchIntervals(events: [MatchEvent], sessionStart: Date,
                                        sessionEnd: Date) -> [(start: Date, end: Date)] {
        let substitutionEvents = events
            .filter { $0.kind == .subIn || $0.kind == .subOut }
            .sorted { $0.date < $1.date }

        var intervals: [(start: Date, end: Date)] = []
        var currentIntervalStart: Date?

        if substitutionEvents.first?.kind != .subIn {
            currentIntervalStart = sessionStart
        }

        for event in substitutionEvents {
            switch event.kind {
            case .subIn:
                if currentIntervalStart == nil {
                    currentIntervalStart = event.date
                }
            case .subOut:
                if let intervalStart = currentIntervalStart {
                    intervals.append((start: intervalStart, end: event.date))
                    currentIntervalStart = nil
                }
            default:
                break
            }
        }

        if let intervalStart = currentIntervalStart {
            intervals.append((start: intervalStart, end: sessionEnd))
        }

        return intervals.filter { $0.end > $0.start }
    }

    /// Total seconds spent on the pitch.
    public static func timeOnPitchSeconds(events: [MatchEvent], sessionStart: Date,
                                          sessionEnd: Date) -> Double {
        onPitchIntervals(events: events, sessionStart: sessionStart, sessionEnd: sessionEnd)
            .reduce(0) { $0 + $1.end.timeIntervalSince($1.start) }
    }
}
