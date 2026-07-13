import Foundation

/// Infers the position a player occupied from where they spent the match.
public enum PositionClassifier {

    /// Classify the player's role and side.
    ///
    /// Conventions: field-relative `x` runs along the field's long axis with
    /// `-length/2` at the player's own goal in the FIRST period; `y` runs
    /// across the field with negative values on the player's left in the
    /// first period. Teams swap ends each period, so samples in odd-indexed
    /// periods (the second half, second overtime, ...) have both axes negated
    /// before averaging. Period means combine weighted by sample count.
    ///
    /// Role thresholds on the combined mean x (L = field length): goalkeeper
    /// below -0.38·L, defender below -0.12·L, midfielder below +0.15·L,
    /// otherwise forward. Side thresholds on the combined mean y: left below
    /// -width/6, right above +width/6, otherwise center.
    public static func classifyPosition(samples: [LocationSample], field: FieldRectangle,
                                        periods: [(start: Date, end: Date)])
        -> (role: PositionRole, side: PositionSide) {
        let effectivePeriods: [(start: Date, end: Date)]
        if periods.isEmpty {
            let start = samples.first?.timestamp ?? Date(timeIntervalSince1970: 0)
            let end = samples.last?.timestamp ?? start
            effectivePeriods = [(start: start, end: end)]
        } else {
            effectivePeriods = periods
        }

        var weightedXSum = 0.0
        var weightedYSum = 0.0
        var totalSampleCount = 0.0

        for (periodIndex, period) in effectivePeriods.enumerated() {
            let periodSamples = samples.filter {
                $0.timestamp >= period.start && $0.timestamp <= period.end
            }
            guard !periodSamples.isEmpty else { continue }

            var xSum = 0.0
            var ySum = 0.0
            for sample in periodSamples {
                let position = FieldGeometry.fieldRelativeCoordinates(
                    latitude: sample.latitude, longitude: sample.longitude, in: field)
                xSum += position.x
                ySum += position.y
            }

            let count = Double(periodSamples.count)
            // Teams swap ends each period: flip odd-indexed period axes so
            // every period is expressed in first-period orientation.
            let endSwapSign: Double = periodIndex % 2 == 0 ? 1 : -1
            weightedXSum += endSwapSign * xSum
            weightedYSum += endSwapSign * ySum
            totalSampleCount += count
        }

        guard totalSampleCount > 0 else { return (.midfielder, .center) }

        let meanX = weightedXSum / totalSampleCount
        let meanY = weightedYSum / totalSampleCount

        let role: PositionRole
        if meanX < -0.38 * field.lengthMeters {
            role = .goalkeeper
        } else if meanX < -0.12 * field.lengthMeters {
            role = .defender
        } else if meanX < 0.15 * field.lengthMeters {
            role = .midfielder
        } else {
            role = .forward
        }

        let side: PositionSide
        if meanY < -field.widthMeters / 6 {
            side = .left
        } else if meanY > field.widthMeters / 6 {
            side = .right
        } else {
            side = .center
        }

        return (role, side)
    }
}
