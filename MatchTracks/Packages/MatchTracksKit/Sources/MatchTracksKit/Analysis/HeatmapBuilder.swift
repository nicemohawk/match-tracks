import Foundation

/// A field-relative occupancy grid, row-major, normalized so the hottest cell
/// is 1.0 whenever any sample landed on the field.
public struct Heatmap: Sendable, Equatable {
    public var columns: Int
    public var rows: Int
    public var cells: [Double]

    public init(columns: Int, rows: Int, cells: [Double]) {
        self.columns = columns
        self.rows = rows
        self.cells = cells
    }

    /// Value at (column, row).
    public func value(column: Int, row: Int) -> Double {
        cells[row * columns + column]
    }
}

/// Bins location samples into a field-relative heatmap.
public enum HeatmapBuilder {

    /// Build a heatmap of where the player spent time on the field.
    ///
    /// Each sample maps into field-relative coordinates, clamps into the
    /// field extent, and lands in a grid cell (x along the field's long axis
    /// maps to columns, y across the field to rows). One 3×3 smoothing pass
    /// with the separable 1-2-1 kernel softens single-sample speckle, then
    /// the grid normalizes to a maximum of 1.
    public static func buildHeatmap(samples: [LocationSample], field: FieldRectangle,
                                    columns: Int = 30, rows: Int = 20) -> Heatmap {
        var grid = [Double](repeating: 0, count: columns * rows)

        let halfLength = field.lengthMeters / 2
        let halfWidth = field.widthMeters / 2

        for sample in samples {
            let position = FieldGeometry.fieldRelativeCoordinates(
                latitude: sample.latitude, longitude: sample.longitude, in: field)
            let clampedX = min(max(position.x, -halfLength), halfLength)
            let clampedY = min(max(position.y, -halfWidth), halfWidth)

            let columnFraction = (clampedX + halfLength) / field.lengthMeters
            let rowFraction = (clampedY + halfWidth) / field.widthMeters
            let column = min(Int(columnFraction * Double(columns)), columns - 1)
            let row = min(Int(rowFraction * Double(rows)), rows - 1)
            grid[row * columns + column] += 1
        }

        grid = smoothed(grid, columns: columns, rows: rows)

        let maximum = grid.max() ?? 0
        if maximum > 0 {
            grid = grid.map { $0 / maximum }
        }

        return Heatmap(columns: columns, rows: rows, cells: grid)
    }

    /// One separable 3×3 pass with the normalized 1-2-1 kernel.
    private static func smoothed(_ grid: [Double], columns: Int, rows: Int) -> [Double] {
        var horizontal = [Double](repeating: 0, count: grid.count)
        for row in 0..<rows {
            for column in 0..<columns {
                let center = grid[row * columns + column]
                let left = column > 0 ? grid[row * columns + column - 1] : 0
                let right = column < columns - 1 ? grid[row * columns + column + 1] : 0
                horizontal[row * columns + column] = (left + 2 * center + right) / 4
            }
        }

        var vertical = [Double](repeating: 0, count: grid.count)
        for row in 0..<rows {
            for column in 0..<columns {
                let center = horizontal[row * columns + column]
                let above = row > 0 ? horizontal[(row - 1) * columns + column] : 0
                let below = row < rows - 1 ? horizontal[(row + 1) * columns + column] : 0
                vertical[row * columns + column] = (above + 2 * center + below) / 4
            }
        }

        return vertical
    }
}
