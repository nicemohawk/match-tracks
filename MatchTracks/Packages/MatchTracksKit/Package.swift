// swift-tools-version:6.0
import PackageDescription

let package = Package(
    name: "MatchTracksKit",
    platforms: [
        .iOS("18.0"),
        .watchOS("11.0"),
        .macOS("14.0"),
    ],
    products: [
        .library(name: "MatchTracksKit", targets: ["MatchTracksKit"]),
    ],
    targets: [
        .target(name: "MatchTracksKit"),
        .testTarget(name: "MatchTracksKitTests", dependencies: ["MatchTracksKit"]),
    ]
)
