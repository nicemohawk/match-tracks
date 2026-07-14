import Foundation
import HealthKit
import CoreLocation
import WatchKit
import MatchTracksKit

/// Drives a live match: the HealthKit workout session, GPS stream, event log,
/// score, substitution state, and end-of-match persistence + handoff.
@Observable
@MainActor
final class WorkoutManager: NSObject {

    enum Phase {
        case idle
        case requestingPermissions
        case active
        case paused
        case summary
    }

    private(set) var phase: Phase = .idle

    // Live metrics.
    private(set) var elapsedSeconds: TimeInterval = 0
    private(set) var heartRate: Double = 0
    private(set) var activeCalories: Double = 0
    private(set) var distanceMeters: Double = 0
    private(set) var currentSpeedMetersPerSecond: Double = 0

    // Game state.
    private(set) var session: MatchSession?
    private(set) var isOnPitch = true
    private(set) var isInActivePeriod = true
    private(set) var completedSession: MatchSession?

    var scoreUs: Int {
        session?.events.filter { $0.kind == .goalFor || $0.kind == .goalMine }.count ?? 0
    }

    var scoreThem: Int {
        session?.events.filter { $0.kind == .goalAgainst }.count ?? 0
    }

    var lastEvent: MatchEvent? {
        session?.events.last
    }

    private let healthStore = HKHealthStore()
    private var workoutSession: HKWorkoutSession?
    private var workoutBuilder: HKLiveWorkoutBuilder?
    private let locationManager = CLLocationManager()
    private var sessionStartDate: Date?
    private var elapsedTimer: Timer?

    let connectivity: WatchConnectivityManager

    init(connectivity: WatchConnectivityManager = WatchConnectivityManager()) {
        self.connectivity = connectivity
        super.init()
        locationManager.delegate = self
        locationManager.desiredAccuracy = kCLLocationAccuracyBest
    }

    // MARK: - Lifecycle

    func startMatch(playerName: String?, teamCode: String?) async {
        guard phase == .idle || phase == .summary else { return }
        phase = .requestingPermissions

        do {
            try await requestPermissions()
        } catch {
            phase = .idle
            return
        }

        let configuration = HKWorkoutConfiguration()
        configuration.activityType = .soccer
        configuration.locationType = .outdoor

        do {
            let workoutSession = try HKWorkoutSession(healthStore: healthStore,
                                                      configuration: configuration)
            let builder = workoutSession.associatedWorkoutBuilder()
            builder.dataSource = HKLiveWorkoutDataSource(healthStore: healthStore,
                                                         workoutConfiguration: configuration)
            workoutSession.delegate = self
            builder.delegate = self

            let startDate = Date()
            workoutSession.startActivity(with: startDate)
            try await builder.beginCollection(at: startDate)

            self.workoutSession = workoutSession
            self.workoutBuilder = builder
            self.sessionStartDate = startDate

            var newSession = MatchSession(recordedAt: startDate)
            newSession.playerName = playerName?.isEmpty == false ? playerName : nil
            newSession.teamCode = teamCode?.isEmpty == false ? teamCode : nil
            newSession.events.append(MatchEvent(kind: .matchStart, date: startDate))
            session = newSession

            isOnPitch = true
            isInActivePeriod = true
            completedSession = nil
            elapsedSeconds = 0
            distanceMeters = 0

            locationManager.allowsBackgroundLocationUpdates = true
            locationManager.startUpdatingLocation()
            startElapsedTimer()
            phase = .active
        } catch {
            phase = .idle
        }
    }

    func togglePause() {
        guard let workoutSession else { return }
        if phase == .active {
            workoutSession.pause()
            phase = .paused
        } else if phase == .paused {
            workoutSession.resume()
            phase = .active
        }
    }

    func endMatch() async {
        guard var endingSession = session, let workoutSession, let workoutBuilder else { return }

        let endDate = Date()
        endingSession.events.append(MatchEvent(kind: .matchEnd, date: endDate))
        endingSession.durationSeconds = endDate.timeIntervalSince(endingSession.recordedAt)

        locationManager.stopUpdatingLocation()
        stopElapsedTimer()
        workoutSession.end()

        do {
            try await workoutBuilder.endCollection(at: endDate)
            _ = try await workoutBuilder.finishWorkout()
        } catch {
            // The session data still gets persisted and transferred below.
        }

        var stats = WorkrateAnalyzer.analyzeWorkrate(samples: endingSession.locationSamples,
                                                     events: endingSession.events)
        if !endingSession.heartRateSamples.isEmpty {
            stats.averageHeartRate = endingSession.heartRateSamples
                .map(\.beatsPerMinute)
                .reduce(0, +) / Double(endingSession.heartRateSamples.count)
        }
        endingSession.stats = stats

        session = nil
        completedSession = endingSession
        self.workoutSession = nil
        self.workoutBuilder = nil

        do {
            let fileURL = try SessionFileStore.save(endingSession)
            connectivity.transferSessionFile(at: fileURL)
        } catch {
            // Persisting failed; the summary still shows and HealthKit has the workout.
        }

        phase = .summary
    }

    func dismissSummary() {
        completedSession = nil
        phase = .idle
    }

    func lockScreenForWater() {
        WKInterfaceDevice.current().enableWaterLock()
    }

    // MARK: - Events

    func addEvent(_ kind: MatchEventKind, note: String? = nil) {
        guard session != nil else { return }
        session?.events.append(MatchEvent(kind: kind, date: Date(), note: note))
        WKInterfaceDevice.current().play(kind == .flag ? .notification : .success)
        mirrorLiveState()
    }

    func undoLastEvent() {
        guard let removableEvent = session?.events.last,
              removableEvent.kind != .matchStart else { return }
        session?.events.removeLast()

        switch removableEvent.kind {
        case .subIn: isOnPitch = false
        case .subOut: isOnPitch = true
        case .periodStart: isInActivePeriod = false
        case .periodEnd: isInActivePeriod = true
        default: break
        }
        WKInterfaceDevice.current().play(.retry)
    }

    func toggleSubstitution() {
        addEvent(isOnPitch ? .subOut : .subIn)
        isOnPitch.toggle()
    }

    func togglePeriod() {
        addEvent(isInActivePeriod ? .periodEnd : .periodStart)
        isInActivePeriod.toggle()
    }

    // MARK: - Internals

    private func requestPermissions() async throws {
        let shareTypes: Set<HKSampleType> = [HKQuantityType.workoutType()]
        let readTypes: Set<HKObjectType> = [
            HKQuantityType(.heartRate),
            HKQuantityType(.distanceWalkingRunning),
            HKQuantityType(.activeEnergyBurned),
        ]
        try await healthStore.requestAuthorization(toShare: shareTypes, read: readTypes)
        locationManager.requestWhenInUseAuthorization()
    }

    private func startElapsedTimer() {
        elapsedTimer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { _ in
            Task { @MainActor [weak self] in
                guard let self, let builder = self.workoutBuilder else { return }
                self.elapsedSeconds = builder.elapsedTime
                self.mirrorLiveState()
            }
        }
    }

    private func stopElapsedTimer() {
        elapsedTimer?.invalidate()
        elapsedTimer = nil
    }

    private func mirrorLiveState() {
        connectivity.mirrorLiveMatch(elapsedSeconds: elapsedSeconds,
                                     scoreUs: scoreUs, scoreThem: scoreThem)
    }

    fileprivate func handleBuilderStatistics(_ statistics: HKStatistics) {
        switch statistics.quantityType {
        case HKQuantityType(.heartRate):
            let unit = HKUnit.count().unitDivided(by: .minute())
            if let value = statistics.mostRecentQuantity()?.doubleValue(for: unit) {
                heartRate = value
                session?.heartRateSamples.append(
                    HeartRateSample(timestamp: Date(), beatsPerMinute: value))
            }
        case HKQuantityType(.activeEnergyBurned):
            if let value = statistics.sumQuantity()?.doubleValue(for: .kilocalorie()) {
                activeCalories = value
            }
        case HKQuantityType(.distanceWalkingRunning):
            if let value = statistics.sumQuantity()?.doubleValue(for: .meter()) {
                distanceMeters = value
            }
        default:
            break
        }
    }

    fileprivate func handleLocations(_ locations: [CLLocation]) {
        guard phase == .active else { return }
        for location in locations {
            session?.locationSamples.append(LocationSample(
                timestamp: location.timestamp,
                latitude: location.coordinate.latitude,
                longitude: location.coordinate.longitude,
                horizontalAccuracyMeters: location.horizontalAccuracy,
                speedMetersPerSecond: max(0, location.speed),
                courseDegrees: max(0, location.course)))
        }
        if let newestLocation = locations.last, newestLocation.speed >= 0 {
            currentSpeedMetersPerSecond = newestLocation.speed
        }
    }
}

// MARK: - HealthKit delegates

extension WorkoutManager: HKWorkoutSessionDelegate {
    nonisolated func workoutSession(_ workoutSession: HKWorkoutSession,
                                    didChangeTo toState: HKWorkoutSessionState,
                                    from fromState: HKWorkoutSessionState, date: Date) {}

    nonisolated func workoutSession(_ workoutSession: HKWorkoutSession,
                                    didFailWithError error: Error) {}
}

extension WorkoutManager: HKLiveWorkoutBuilderDelegate {
    nonisolated func workoutBuilder(_ workoutBuilder: HKLiveWorkoutBuilder,
                                    didCollectDataOf collectedTypes: Set<HKSampleType>) {
        let statisticsList: [HKStatistics] = collectedTypes.compactMap { sampleType in
            guard let quantityType = sampleType as? HKQuantityType else { return nil }
            return workoutBuilder.statistics(for: quantityType)
        }
        Task { @MainActor [weak self] in
            for statistics in statisticsList {
                self?.handleBuilderStatistics(statistics)
            }
        }
    }

    nonisolated func workoutBuilderDidCollectEvent(_ workoutBuilder: HKLiveWorkoutBuilder) {}
}

// MARK: - Location delegate

extension WorkoutManager: CLLocationManagerDelegate {
    nonisolated func locationManager(_ manager: CLLocationManager,
                                     didUpdateLocations locations: [CLLocation]) {
        Task { @MainActor [weak self] in
            self?.handleLocations(locations)
        }
    }

    nonisolated func locationManager(_ manager: CLLocationManager,
                                     didFailWithError error: Error) {}
}
