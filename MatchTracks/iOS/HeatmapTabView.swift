import SwiftUI
import MatchTracksKit

/// The player's positional heatmap drawn over a stylized pitch.
struct HeatmapTabView: View {
    let session: MatchSession
    let field: DetectedField?

    var body: some View {
        if let field {
            GeometryReader { proxy in
                let heatmap = HeatmapBuilder.buildHeatmap(samples: session.locationSamples,
                                                          field: field.rectangle)
                Canvas { context, size in
                    drawPitch(in: &context, size: size)
                    drawHeatmap(heatmap, in: &context, size: size)
                }
                .aspectRatio(field.rectangle.lengthMeters / field.rectangle.widthMeters,
                             contentMode: .fit)
                .frame(maxWidth: proxy.size.width, maxHeight: proxy.size.height)
            }
            .padding()
        } else {
            ContentUnavailableView(
                "No Field Detected",
                systemImage: "sportscourt",
                description: Text("A heatmap needs a detected field. Play more of the match on one pitch, or fetch nearby community fields in the Fields tab.")
            )
        }
    }

    private func drawPitch(in context: inout GraphicsContext, size: CGSize) {
        let pitchRect = CGRect(origin: .zero, size: size)
        context.fill(Path(roundedRect: pitchRect, cornerRadius: 4),
                     with: .color(Color(red: 0.13, green: 0.45, blue: 0.2)))

        var lines = Path()
        lines.addRect(pitchRect.insetBy(dx: 2, dy: 2))
        lines.move(to: CGPoint(x: size.width / 2, y: 2))
        lines.addLine(to: CGPoint(x: size.width / 2, y: size.height - 2))
        lines.addEllipse(in: CGRect(x: size.width / 2 - size.height * 0.15,
                                    y: size.height / 2 - size.height * 0.15,
                                    width: size.height * 0.3,
                                    height: size.height * 0.3))
        context.stroke(lines, with: .color(.white.opacity(0.8)), lineWidth: 1.5)
    }

    private func drawHeatmap(_ heatmap: Heatmap, in context: inout GraphicsContext,
                             size: CGSize) {
        let cellWidth = size.width / CGFloat(heatmap.columns)
        let cellHeight = size.height / CGFloat(heatmap.rows)

        for row in 0..<heatmap.rows {
            for column in 0..<heatmap.columns {
                let value = heatmap.value(column: column, row: row)
                guard value > 0.02 else { continue }
                let cellRect = CGRect(x: CGFloat(column) * cellWidth,
                                      y: CGFloat(row) * cellHeight,
                                      width: cellWidth, height: cellHeight)
                context.fill(Path(roundedRect: cellRect.insetBy(dx: 0.5, dy: 0.5),
                                  cornerRadius: 2),
                             with: .color(heatColor(for: value)))
            }
        }
    }

    /// Transparent → yellow → red color ramp.
    private func heatColor(for value: Double) -> Color {
        if value < 0.5 {
            return Color(red: 1, green: 0.9, blue: 0.1, opacity: 0.25 + value)
        }
        return Color(red: 1, green: 0.9 - (value - 0.5) * 1.4, blue: 0.05,
                     opacity: 0.55 + value * 0.4)
    }
}
