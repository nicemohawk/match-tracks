import XCTest
@testable import MatchTracksKit

final class AnalysisTests: XCTestCase {

    let baseLatitude = 39.33
    let baseLongitude = -82.10
    let sessionStart = Date(timeIntervalSince1970: 1_784_000_000)

    /// 1 Hz samples with the given speed profile, all at the same coordinate.
    private func samples(speeds: [Double]) -> [LocationSample] {
        speeds.enumerated().map { index, speed in
            LocationSample(timestamp: sessionStart.addingTimeInterval(Double(index)),
                           latitude: baseLatitude, longitude: baseLongitude,
                           speedMetersPerSecond: speed)
        }
    }

    // MARK: - Run segmentation

    func testRunSegmentationHysteresisAndSprintFlag() {
        var speeds = [Double]()
        speeds.append(contentsOf: Array(repeating: 1.0, count: 5))    // walk
        speeds.append(contentsOf: Array(repeating: 3.4, count: 3))    // run begins
        speeds.append(2.0)                                            // 1 s dip: must not split
        speeds.append(contentsOf: Array(repeating: 3.4, count: 3))    // run continues
        speeds.append(contentsOf: Array(repeating: 1.0, count: 5))    // lull ends run
        speeds.append(contentsOf: Array(repeating: 6.0, count: 5))    // sprint
        speeds.append(contentsOf: Array(repeating: 0.0, count: 3))    // stand

        let runs = RunSegmenter.segmentRuns(from: samples(speeds: speeds))

        XCTAssertEqual(runs.count, 2)
        XCTAssertFalse(runs[0].isSprint)
        XCTAssertEqual(runs[0].startIndex, 5)
        XCTAssertEqual(runs[0].endIndex, 11)  // last sample at/above 2.5 before the lull
        XCTAssertTrue(runs[1].isSprint)
        XCTAssertEqual(runs[1].peakSpeedMetersPerSecond, 6.0)
    }

    func testRunsShorterThanMinimumAreDiscarded() {
        var speeds = Array(repeating: 1.0, count: 5)
        speeds.append(3.5)                                            // 1 s burst
        speeds.append(contentsOf: Array(repeating: 1.0, count: 5))

        XCTAssertTrue(RunSegmenter.segmentRuns(from: samples(speeds: speeds)).isEmpty)
    }

    // MARK: - Speed zones

    func testSpeedZoneDurations() {
        let speeds = [0.1, 0.1, 1.0, 1.0, 2.0, 2.0, 4.0, 4.0, 6.0, 6.0]
        let zones = WorkrateAnalyzer.speedZones(samples(speeds: speeds))

        XCTAssertEqual(zones.standingSeconds, 2, accuracy: 0.001)
        XCTAssertEqual(zones.walkingSeconds, 2, accuracy: 0.001)
        XCTAssertEqual(zones.joggingSeconds, 2, accuracy: 0.001)
        XCTAssertEqual(zones.runningSeconds, 2, accuracy: 0.001)
        XCTAssertEqual(zones.sprintingSeconds, 1, accuracy: 0.001)
    }

    func testSpeedZonesIgnoreSignalGaps() {
        var gappedSamples = samples(speeds: [1.0, 1.0])
        gappedSamples.append(LocationSample(
            timestamp: sessionStart.addingTimeInterval(100),  // 99 s gap
            latitude: baseLatitude, longitude: baseLongitude, speedMetersPerSecond: 1.0))

        let zones = WorkrateAnalyzer.speedZones(gappedSamples)
        XCTAssertEqual(zones.walkingSeconds, 1, accuracy: 0.001)
    }

    // MARK: - Playing time

    private func substitution(_ kind: MatchEventKind, atOffset offset: TimeInterval) -> MatchEvent {
        MatchEvent(kind: kind, date: sessionStart.addingTimeInterval(offset))
    }

    func testPlayingTimeWithNoSubstitutionEvents() {
        let seconds = PlayingTimeCalculator.timeOnPitchSeconds(
            events: [MatchEvent(kind: .goalFor, date: sessionStart.addingTimeInterval(60))],
            sessionStart: sessionStart, sessionEnd: sessionStart.addingTimeInterval(1000))
        XCTAssertEqual(seconds, 1000, accuracy: 0.001)
    }

    func testPlayingTimeWithSubOut() {
        let seconds = PlayingTimeCalculator.timeOnPitchSeconds(
            events: [substitution(.subOut, atOffset: 600)],
            sessionStart: sessionStart, sessionEnd: sessionStart.addingTimeInterval(1000))
        XCTAssertEqual(seconds, 600, accuracy: 0.001)
    }

    func testPlayingTimeStartingOnTheBench() {
        let seconds = PlayingTimeCalculator.timeOnPitchSeconds(
            events: [substitution(.subIn, atOffset: 300), substitution(.subOut, atOffset: 900)],
            sessionStart: sessionStart, sessionEnd: sessionStart.addingTimeInterval(1000))
        XCTAssertEqual(seconds, 600, accuracy: 0.001)
    }

    func testPlayingTimeUnterminatedSubInClosesAtSessionEnd() {
        let seconds = PlayingTimeCalculator.timeOnPitchSeconds(
            events: [substitution(.subOut, atOffset: 200), substitution(.subIn, atOffset: 400)],
            sessionStart: sessionStart, sessionEnd: sessionStart.addingTimeInterval(1000))
        XCTAssertEqual(seconds, 200 + 600, accuracy: 0.001)
    }

    // MARK: - Position classification

    /// Samples clustered at a field-frame position during a time window.
    /// `alongMeters`/`acrossMeters` use first-period orientation; pass
    /// pre-flipped values for second-period data (the classifier un-flips).
    private func positionSamples(alongMeters: Double, acrossMeters: Double,
                                 startOffset: TimeInterval, count: Int,
                                 field: FieldRectangle) -> [LocationSample] {
        (0..<count).map { index in
            let offset = SyntheticField.eastNorth(alongMeters: alongMeters,
                                                  acrossMeters: acrossMeters,
                                                  headingDegrees: field.headingDegrees)
            let coordinate = SyntheticField.coordinate(
                centerLatitude: field.centerLatitude, centerLongitude: field.centerLongitude,
                eastMeters: offset.east, northMeters: offset.north)
            return LocationSample(
                timestamp: sessionStart.addingTimeInterval(startOffset + Double(index)),
                latitude: coordinate[0], longitude: coordinate[1])
        }
    }

    private var testField: FieldRectangle {
        FieldRectangle(centerLatitude: baseLatitude, centerLongitude: baseLongitude,
                       lengthMeters: 100, widthMeters: 64, headingDegrees: 0)
    }

    private var twoPeriods: [(start: Date, end: Date)] {
        [
            (start: sessionStart, end: sessionStart.addingTimeInterval(100)),
            (start: sessionStart.addingTimeInterval(200), end: sessionStart.addingTimeInterval(300)),
        ]
    }

    func testGoalkeeperClassification() {
        let field = testField
        // Own goal end both halves: -45 m in the first period, +45 m raw in
        // the second (the classifier's end-swap flip restores it to -45).
        let firstHalf = positionSamples(alongMeters: -45, acrossMeters: 0,
                                        startOffset: 0, count: 50, field: field)
        let secondHalf = positionSamples(alongMeters: 45, acrossMeters: 0,
                                         startOffset: 200, count: 50, field: field)

        let position = PositionClassifier.classifyPosition(
            samples: firstHalf + secondHalf, field: field, periods: twoPeriods)

        XCTAssertEqual(position.role, .goalkeeper)
        XCTAssertEqual(position.side, .center)
    }

    func testLeftForwardClassification() {
        let field = testField
        let firstHalf = positionSamples(alongMeters: 30, acrossMeters: -15,
                                        startOffset: 0, count: 50, field: field)
        let secondHalf = positionSamples(alongMeters: -30, acrossMeters: 15,
                                         startOffset: 200, count: 50, field: field)

        let position = PositionClassifier.classifyPosition(
            samples: firstHalf + secondHalf, field: field, periods: twoPeriods)

        XCTAssertEqual(position.role, .forward)
        XCTAssertEqual(position.side, .left)
    }

    func testMidfielderWithoutExplicitPeriods() {
        let field = testField
        let clusteredSamples = positionSamples(alongMeters: 0, acrossMeters: 0,
                                               startOffset: 0, count: 50, field: field)

        let position = PositionClassifier.classifyPosition(
            samples: clusteredSamples, field: field, periods: [])

        XCTAssertEqual(position.role, .midfielder)
        XCTAssertEqual(position.side, .center)
    }

    // MARK: - Heatmap

    func testHeatmapHottestCellAtSampleCluster() {
        let field = testField
        // Cluster near the (-length/2, -width/2) corner → column 0, row 0.
        let cornerSamples = positionSamples(alongMeters: -48, acrossMeters: -30,
                                            startOffset: 0, count: 40, field: field)

        let heatmap = HeatmapBuilder.buildHeatmap(samples: cornerSamples, field: field)

        XCTAssertEqual(heatmap.cells.max(), 1.0)
        let hottestIndex = heatmap.cells.firstIndex(of: 1.0) ?? -1
        XCTAssertEqual(hottestIndex % heatmap.columns, 0)
        XCTAssertEqual(hottestIndex / heatmap.columns, 0)
    }

    func testHeatmapWithNoSamplesIsAllZeros() {
        let heatmap = HeatmapBuilder.buildHeatmap(samples: [], field: testField)
        XCTAssertTrue(heatmap.cells.allSatisfy { $0 == 0 })
        XCTAssertEqual(heatmap.cells.count, heatmap.columns * heatmap.rows)
    }

    // MARK: - Workrate score

    func testWorkrateScoreReferenceValuesAndClamp() {
        // 110 m/min, 65% active, 3 sprints per 10 minutes → exactly 100.
        let referenceScore = WorkrateAnalyzer.workrateScore(
            distanceMeters: 5500, activeSeconds: 1950, totalSeconds: 3000, sprintCount: 15)
        XCTAssertEqual(referenceScore, 100, accuracy: 0.001)

        // Half of everything → 50.
        let halfScore = WorkrateAnalyzer.workrateScore(
            distanceMeters: 2750, activeSeconds: 975, totalSeconds: 3000, sprintCount: 7)
        XCTAssertEqual(halfScore, 20 + 20 + 20 * (1.4 / 3), accuracy: 0.01)

        // Everything doubled clamps at 100.
        let clampedScore = WorkrateAnalyzer.workrateScore(
            distanceMeters: 11000, activeSeconds: 3000, totalSeconds: 3000, sprintCount: 30)
        XCTAssertEqual(clampedScore, 100)

        XCTAssertEqual(WorkrateAnalyzer.workrateScore(
            distanceMeters: 0, activeSeconds: 0, totalSeconds: 0, sprintCount: 0), 0)
    }

    // MARK: - End-to-end workrate analysis

    func testAnalyzeWorkrateRespectsSubstitutions() {
        // 601 samples at 1 Hz moving north at 2 m/s; subbed out at +300 s.
        let movingSamples = (0..<601).map { index -> LocationSample in
            let coordinate = SyntheticField.coordinate(
                centerLatitude: baseLatitude, centerLongitude: baseLongitude,
                eastMeters: 0, northMeters: Double(index) * 2)
            return LocationSample(timestamp: sessionStart.addingTimeInterval(Double(index)),
                                  latitude: coordinate[0], longitude: coordinate[1],
                                  speedMetersPerSecond: 2.0)
        }
        let events = [substitution(.subOut, atOffset: 300)]

        let stats = WorkrateAnalyzer.analyzeWorkrate(samples: movingSamples, events: events)

        XCTAssertEqual(stats.timeOnPitchSeconds ?? 0, 300, accuracy: 0.001)
        // 300 on-pitch intervals × 2 m each.
        XCTAssertEqual(stats.totalDistanceMeters ?? 0, 600, accuracy: 5)
        XCTAssertEqual(stats.speedZones?.joggingSeconds ?? 0, 300, accuracy: 1)
        XCTAssertEqual(stats.runCount, 0)
        XCTAssertNil(stats.positionRole)
    }

    func testAnalyzeWorkrateWithNoSamples() {
        let stats = WorkrateAnalyzer.analyzeWorkrate(samples: [], events: [])
        XCTAssertNil(stats.totalDistanceMeters)
        XCTAssertNil(stats.workrateScore)
    }
}
