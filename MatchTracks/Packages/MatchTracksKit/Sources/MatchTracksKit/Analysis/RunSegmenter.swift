import Foundation

/// One contiguous stretch of running within a match track.
public struct Run: Sendable, Equatable {
    public var startIndex: Int
    public var endIndex: Int
    public var startDate: Date
    public var endDate: Date
    public var distanceMeters: Double
    public var peakSpeedMetersPerSecond: Double
    public var isSprint: Bool

    public init(startIndex: Int, endIndex: Int, startDate: Date, endDate: Date,
                distanceMeters: Double, peakSpeedMetersPerSecond: Double, isSprint: Bool) {
        self.startIndex = startIndex
        self.endIndex = endIndex
        self.startDate = startDate
        self.endDate = endDate
        self.distanceMeters = distanceMeters
        self.peakSpeedMetersPerSecond = peakSpeedMetersPerSecond
        self.isSprint = isSprint
    }
}

/// Splits a GPS track into individual runs using speed hysteresis.
public enum RunSegmenter {

    /// A run starts at this speed or above.
    public static let runStartSpeedMetersPerSecond = 3.3
    /// A run continues until speed stays below this...
    public static let runContinueSpeedMetersPerSecond = 2.5
    /// ...for longer than this.
    public static let runEndLullSeconds = 1.5
    /// Runs shorter than this are discarded.
    public static let minimumRunDurationSeconds = 2.0
    /// A run whose peak reaches this speed is a sprint.
    public static let sprintPeakSpeedMetersPerSecond = 5.5

    /// Segment a sample stream into runs.
    ///
    /// Hysteresis: a run begins at a sample with speed >=
    /// `runStartSpeedMetersPerSecond` and ends once speed has stayed below
    /// `runContinueSpeedMetersPerSecond` for more than `runEndLullSeconds`
    /// (the run's `endIndex` is the last fast sample before the lull). Runs
    /// shorter than `minimumRunDurationSeconds` are dropped.
    public static func segmentRuns(from samples: [LocationSample]) -> [Run] {
        var runs: [Run] = []
        var runStartIndex: Int?
        var lastFastIndex = 0
        var lullStart: Date?

        func closeRun(endingAt endIndex: Int) {
            guard let startIndex = runStartIndex else { return }
            runStartIndex = nil
            lullStart = nil

            let startDate = samples[startIndex].timestamp
            let endDate = samples[endIndex].timestamp
            guard endDate.timeIntervalSince(startDate) >= minimumRunDurationSeconds else { return }

            var distanceMeters = 0.0
            var peakSpeed = 0.0
            for index in startIndex...endIndex {
                peakSpeed = max(peakSpeed, samples[index].speedMetersPerSecond)
                if index > startIndex {
                    distanceMeters += FieldGeometry.haversineDistanceMeters(
                        latitude1: samples[index - 1].latitude,
                        longitude1: samples[index - 1].longitude,
                        latitude2: samples[index].latitude,
                        longitude2: samples[index].longitude)
                }
            }

            runs.append(Run(startIndex: startIndex, endIndex: endIndex,
                            startDate: startDate, endDate: endDate,
                            distanceMeters: distanceMeters,
                            peakSpeedMetersPerSecond: peakSpeed,
                            isSprint: peakSpeed >= sprintPeakSpeedMetersPerSecond))
        }

        for (index, sample) in samples.enumerated() {
            if runStartIndex == nil {
                if sample.speedMetersPerSecond >= runStartSpeedMetersPerSecond {
                    runStartIndex = index
                    lastFastIndex = index
                    lullStart = nil
                }
                continue
            }

            if sample.speedMetersPerSecond >= runContinueSpeedMetersPerSecond {
                lastFastIndex = index
                lullStart = nil
            } else {
                let lullBegan = lullStart ?? sample.timestamp
                lullStart = lullBegan
                if sample.timestamp.timeIntervalSince(lullBegan) > runEndLullSeconds {
                    closeRun(endingAt: lastFastIndex)
                }
            }
        }
        closeRun(endingAt: lastFastIndex)

        return runs
    }
}
