import SwiftUI
import MatchTracksKit

/// Full match breakdown: map, heatmap, runs, stats, and events.
struct SessionDetailView: View {
    @Environment(SessionStore.self) private var sessionStore
    @Environment(FieldDatabase.self) private var fieldDatabase

    let session: MatchSession
    @State private var selectedTab = DetailTab.map

    enum DetailTab: String, CaseIterable, Identifiable {
        case map = "Map"
        case heatmap = "Heatmap"
        case runs = "Runs"
        case stats = "Stats"
        case events = "Events"

        var id: String { rawValue }
    }

    private var field: DetectedField? {
        fieldDatabase.find(by: session.fieldID)
    }

    var body: some View {
        VStack(spacing: 0) {
            Picker("View", selection: $selectedTab) {
                ForEach(DetailTab.allCases) { tab in
                    Text(tab.rawValue).tag(tab)
                }
            }
            .pickerStyle(.segmented)
            .padding(.horizontal)

            switch selectedTab {
            case .map:
                MapTabView(session: session, field: field)
            case .heatmap:
                HeatmapTabView(session: session, field: field)
            case .runs:
                RunsTabView(session: session)
            case .stats:
                StatsTabView(session: session)
            case .events:
                EventsTabView(session: session)
            }
        }
        .navigationTitle(session.recordedAt.formatted(date: .abbreviated, time: .shortened))
        .navigationBarTitleDisplayMode(.inline)
    }
}
