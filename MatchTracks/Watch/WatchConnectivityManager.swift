import Foundation
import WatchConnectivity

/// Sends completed match sessions to the paired iPhone and mirrors a small
/// live-match summary into the application context.
///
/// Contract with the iOS app: completed sessions travel as a file transfer of
/// "<uuid>.matchsession.json" with metadata ["type": "matchSession"]. Files
/// stay on disk until the transfer succeeds, so retries survive restarts.
final class WatchConnectivityManager: NSObject, @unchecked Sendable {

    override init() {
        super.init()
        guard WCSession.isSupported() else { return }
        WCSession.default.delegate = self
        WCSession.default.activate()
    }

    /// Queue a session file for transfer to the phone.
    func transferSessionFile(at fileURL: URL) {
        guard WCSession.isSupported() else { return }
        WCSession.default.transferFile(fileURL, metadata: ["type": "matchSession"])
    }

    /// Re-queue any persisted session that has no in-flight transfer.
    func retryPendingTransfers() {
        guard WCSession.isSupported() else { return }
        let inFlightFileNames = Set(WCSession.default.outstandingFileTransfers
            .map { $0.file.fileURL.lastPathComponent })
        let pendingFileURLs = (try? FileManager.default.contentsOfDirectory(
            at: SessionFileStore.sessionsDirectory, includingPropertiesForKeys: nil)) ?? []
        for fileURL in pendingFileURLs
        where fileURL.lastPathComponent.hasSuffix(".matchsession.json")
            && !inFlightFileNames.contains(fileURL.lastPathComponent) {
            transferSessionFile(at: fileURL)
        }
    }

    /// Best-effort mirror of live match basics for the phone's UI.
    func mirrorLiveMatch(elapsedSeconds: Double, scoreUs: Int, scoreThem: Int) {
        guard WCSession.isSupported(), WCSession.default.activationState == .activated else {
            return
        }
        try? WCSession.default.updateApplicationContext([
            "liveMatch": [
                "elapsed": elapsedSeconds,
                "scoreUs": scoreUs,
                "scoreThem": scoreThem,
            ],
        ])
    }
}

extension WatchConnectivityManager: WCSessionDelegate {
    func session(_ session: WCSession, activationDidCompleteWith activationState: WCSessionActivationState,
                 error: Error?) {
        if activationState == .activated {
            retryPendingTransfers()
        }
    }

    func session(_ session: WCSession, didFinish fileTransfer: WCSessionFileTransfer,
                 error: Error?) {
        guard error == nil else { return }
        // Delivered: the file's uuid name maps back to the stored session.
        let fileName = fileTransfer.file.fileURL.lastPathComponent
        let uuidString = fileName.replacingOccurrences(of: ".matchsession.json", with: "")
        if let sessionID = UUID(uuidString: uuidString) {
            SessionFileStore.delete(sessionID: sessionID)
        }
    }
}
