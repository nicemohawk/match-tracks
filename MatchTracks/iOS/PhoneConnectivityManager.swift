import Foundation
import WatchConnectivity
import MatchTracksKit

/// Live-match basics mirrored from the watch's application context.
struct LiveMatchState: Equatable {
    var elapsedSeconds: Double
    var scoreUs: Int
    var scoreThem: Int
}

/// Receives completed match sessions (file transfers) and live-match context
/// from the watch.
@Observable
final class PhoneConnectivityManager: NSObject, @unchecked Sendable {

    @MainActor private(set) var liveMatch: LiveMatchState?

    private var sessionStore: SessionStore?

    private static var stagingDirectory: URL {
        FileManager.default.temporaryDirectory
            .appendingPathComponent("IncomingSessions", isDirectory: true)
    }

    @MainActor
    func activate(sessionStore: SessionStore) {
        self.sessionStore = sessionStore
        guard WCSession.isSupported() else { return }
        WCSession.default.delegate = self
        WCSession.default.activate()
    }

    @MainActor
    private func handleReceivedSessionFile(at stagedFileURL: URL) {
        sessionStore?.ingest(fileURL: stagedFileURL)
        try? FileManager.default.removeItem(at: stagedFileURL)
    }

    @MainActor
    private func updateLiveMatch(from context: [String: Any]) {
        guard let liveMatchInfo = context["liveMatch"] as? [String: Any],
              let elapsed = liveMatchInfo["elapsed"] as? Double else {
            liveMatch = nil
            return
        }
        liveMatch = LiveMatchState(elapsedSeconds: elapsed,
                                   scoreUs: liveMatchInfo["scoreUs"] as? Int ?? 0,
                                   scoreThem: liveMatchInfo["scoreThem"] as? Int ?? 0)
    }
}

extension PhoneConnectivityManager: WCSessionDelegate {

    func session(_ session: WCSession, activationDidCompleteWith activationState: WCSessionActivationState,
                 error: Error?) {}

    func sessionDidBecomeInactive(_ session: WCSession) {}

    func sessionDidDeactivate(_ session: WCSession) {
        session.activate()
    }

    func session(_ session: WCSession, didReceive file: WCSessionFile) {
        guard file.metadata?["type"] as? String == "matchSession" else { return }

        // The system deletes the received file after this callback returns,
        // so stage a copy synchronously before hopping to the main actor.
        let stagedFileURL = Self.stagingDirectory
            .appendingPathComponent(file.fileURL.lastPathComponent)
        do {
            try FileManager.default.createDirectory(at: Self.stagingDirectory,
                                                    withIntermediateDirectories: true)
            try? FileManager.default.removeItem(at: stagedFileURL)
            try FileManager.default.copyItem(at: file.fileURL, to: stagedFileURL)
        } catch {
            return
        }

        Task { @MainActor [weak self] in
            self?.handleReceivedSessionFile(at: stagedFileURL)
        }
    }

    func session(_ session: WCSession, didReceiveApplicationContext applicationContext: [String: Any]) {
        // Dictionary values aren't Sendable; extract primitives before hopping actors.
        let liveMatchInfo = applicationContext["liveMatch"] as? [String: Any]
        let elapsed = liveMatchInfo?["elapsed"] as? Double
        let scoreUs = liveMatchInfo?["scoreUs"] as? Int ?? 0
        let scoreThem = liveMatchInfo?["scoreThem"] as? Int ?? 0
        Task { @MainActor [weak self] in
            if let elapsed {
                self?.liveMatch = LiveMatchState(elapsedSeconds: elapsed,
                                                 scoreUs: scoreUs, scoreThem: scoreThem)
            } else {
                self?.liveMatch = nil
            }
        }
    }
}
