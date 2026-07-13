import Foundation

/// Computes work-rate metrics (distance, speed zones, composite score) from a
/// session's samples and substitution events.
public enum WorkrateAnalyzer {

    /// Speed zone thresholds, meters per second.
    public static let standingUpperBound = 0.3
    public static let walkingUpperBound = 1.8
    public static let joggingUpperBound = 3.3
    public static let runningUpperBound = 5.0

    /// Inter-sample gaps longer than this are treated as signal loss.
    public static let signalGapSeconds = 10.0
    /// Samples with worse horizontal accuracy than this don't count toward distance.
    public static let maximumUsableAccuracyMeters = 30.0

    /// Time spent in each speed band.
    ///
    /// Each inter-sample interval is attributed to the earlier sample's speed;
    /// intervals longer than `signalGapSeconds` are ignored.
    public static func speedZones(_ samples: [LocationSample]) -> SpeedZoneDurations {
        var zones = SpeedZoneDurations()

        for index in 1..<max(samples.count, 1) {
            let interval = samples[index].timestamp.timeIntervalSince(samples[index - 1].timestamp)
            guard interval > 0, interval <= signalGapSeconds else { continue }

            let speed = samples[index - 1].speedMetersPerSecond
            if speed < standingUpperBound {
                zones.standingSeconds += interval
            } else if speed < walkingUpperBound {
                zones.walkingSeconds += interval
            } else if speed < joggingUpperBound {
                zones.joggingSeconds += interval
            } else if speed < runningUpperBound {
                zones.runningSeconds += interval
            } else {
                zones.sprintingSeconds += interval
            }
        }

        return zones
    }

    /// Total distance covered, skipping signal gaps and low-accuracy fixes.
    public static func totalDistanceMeters(_ samples: [LocationSample]) -> Double {
        var distance = 0.0
        for index in 1..<max(samples.count, 1) {
            let previous = samples[index - 1]
            let current = samples[index]
            let interval = current.timestamp.timeIntervalSince(previous.timestamp)
            guard interval > 0, interval <= signalGapSeconds,
                  previous.horizontalAccuracyMeters <= maximumUsableAccuracyMeters,
                  current.horizontalAccuracyMeters <= maximumUsableAccuracyMeters else {
                continue
            }
            distance += FieldGeometry.haversineDistanceMeters(
                latitude1: previous.latitude, longitude1: previous.longitude,
                latitude2: current.latitude, longitude2: current.longitude)
        }
        return distance
    }

    /// Composite 0–100 work-rate score.
    ///
    /// `min(100, 40·(distancePerMinute/110) + 40·(activeFraction/0.65) +
    /// 20·(sprintsPerTenMinutes/3))` — distance per minute is measured
    /// against a 110 m/min reference, active fraction (time not standing)
    /// against 65%, and sprint frequency against 3 per 10 minutes.
    public static func workrateScore(distanceMeters: Double, activeSeconds: Double,
                                     totalSeconds: Double, sprintCount: Int) -> Double {
        guard totalSeconds > 0 else { return 0 }

        let distancePerMinute = distanceMeters / (totalSeconds / 60)
        let activeFraction = activeSeconds / totalSeconds
        let sprintsPerTenMinutes = Double(sprintCount) / (totalSeconds / 600)

        let score = 40 * (distancePerMinute / 110)
            + 40 * (activeFraction / 0.65)
            + 20 * (sprintsPerTenMinutes / 3)
        return min(100, score)
    }

    /// Full work-rate analysis of a session.
    ///
    /// Filters samples to on-pitch intervals (from substitution events), then
    /// populates distance, playing time, run/sprint counts, speed zones, and
    /// the composite score. Position and heart rate are left nil — callers
    /// fill those from `PositionClassifier` and the heart-rate trail.
    public static func analyzeWorkrate(samples: [LocationSample],
                                       events: [MatchEvent]) -> MatchStats {
        guard let firstSample = samples.first, let lastSample = samples.last else {
            return MatchStats()
        }

        let sessionStart = firstSample.timestamp
        let sessionEnd = lastSample.timestamp
        let intervals = PlayingTimeCalculator.onPitchIntervals(
            events: events, sessionStart: sessionStart, sessionEnd: sessionEnd)

        let onPitchSamples = samples.filter { sample in
            intervals.contains { sample.timestamp >= $0.start && sample.timestamp <= $0.end }
        }

        let zones = speedZones(onPitchSamples)
        let timeOnPitch = PlayingTimeCalculator.timeOnPitchSeconds(
            events: events, sessionStart: sessionStart, sessionEnd: sessionEnd)
        let distance = totalDistanceMeters(onPitchSamples)
        let runs = RunSegmenter.segmentRuns(from: onPitchSamples)
        let sprintCount = runs.filter(\.isSprint).count

        let activeSeconds = zones.walkingSeconds + zones.joggingSeconds
            + zones.runningSeconds + zones.sprintingSeconds

        return MatchStats(
            totalDistanceMeters: distance,
            timeOnPitchSeconds: timeOnPitch,
            sprintCount: sprintCount,
            runCount: runs.count,
            workrateScore: workrateScore(distanceMeters: distance,
                                         activeSeconds: activeSeconds,
                                         totalSeconds: timeOnPitch,
                                         sprintCount: sprintCount),
            speedZones: zones
        )
    }
}
