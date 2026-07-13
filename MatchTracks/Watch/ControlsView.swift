import SwiftUI

/// Workout-style control grid: end, pause, water lock, substitution, period.
struct ControlsView: View {
    @Environment(WorkoutManager.self) private var workoutManager

    var body: some View {
        ScrollView {
            VStack(spacing: 10) {
                HStack(spacing: 10) {
                    controlButton(title: "End", systemImage: "xmark", tint: .red) {
                        Task { await workoutManager.endMatch() }
                    }
                    controlButton(
                        title: workoutManager.phase == .paused ? "Resume" : "Pause",
                        systemImage: workoutManager.phase == .paused ? "play.fill" : "pause",
                        tint: .yellow
                    ) {
                        workoutManager.togglePause()
                    }
                }
                HStack(spacing: 10) {
                    controlButton(title: "Lock", systemImage: "drop.fill", tint: .cyan) {
                        workoutManager.lockScreenForWater()
                    }
                    controlButton(
                        title: workoutManager.isOnPitch ? "Sub Out" : "Sub In",
                        systemImage: workoutManager.isOnPitch
                            ? "arrow.down.right.circle" : "arrow.up.left.circle",
                        tint: workoutManager.isOnPitch ? .orange : .green
                    ) {
                        workoutManager.toggleSubstitution()
                    }
                }
                HStack(spacing: 10) {
                    controlButton(
                        title: workoutManager.isInActivePeriod ? "End Half" : "Start Half",
                        systemImage: "clock.badge.checkmark",
                        tint: .blue
                    ) {
                        workoutManager.togglePeriod()
                    }
                    Spacer()
                        .frame(maxWidth: .infinity)
                }
            }
            .padding(.horizontal, 4)
        }
        .navigationTitle("Controls")
    }

    private func controlButton(title: String, systemImage: String, tint: Color,
                               action: @escaping () -> Void) -> some View {
        VStack(spacing: 4) {
            Button(action: action) {
                Image(systemName: systemImage)
                    .font(.title3)
                    .frame(maxWidth: .infinity, minHeight: 40)
            }
            .buttonStyle(.borderedProminent)
            .tint(tint.opacity(0.85))
            Text(title)
                .font(.caption2)
                .foregroundStyle(.secondary)
        }
    }
}
