import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

/// Async client for the match-tracks backend service.
public struct BackendClient: Sendable {

    public enum BackendError: Error, LocalizedError {
        case httpFailure(statusCode: Int)

        public var errorDescription: String? {
            switch self {
            case .httpFailure(let statusCode):
                return "The server responded with status \(statusCode)."
            }
        }
    }

    public let baseURL: URL
    public let apiKey: String
    public let deviceIdentifier: String

    private let urlSession: URLSession

    public init(baseURL: URL, apiKey: String, deviceIdentifier: String,
                urlSession: URLSession = .shared) {
        self.baseURL = baseURL
        self.apiKey = apiKey
        self.deviceIdentifier = deviceIdentifier.lowercased()
        self.urlSession = urlSession
    }

    /// Upload sessions (with events/stats/field linkage) to the backend.
    public func uploadSessions(_ sessions: [MatchSession]) async throws {
        let url = baseURL.appendingPathComponent("devices/\(deviceIdentifier)/sessions/")
        var request = authorizedRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let payloads = sessions.map(MatchSessionWirePayload.init(session:))
        request.httpBody = try JSONEncoder().encode(["sessions": payloads])

        let (_, response) = try await urlSession.data(for: request)
        try validate(response)
    }

    /// Community fields near a location.
    public func nearbyFields(latitude: Double, longitude: Double,
                             radiusMeters: Double = 2000) async throws -> [DetectedField] {
        var components = URLComponents(url: baseURL.appendingPathComponent("fields/nearby"),
                                       resolvingAgainstBaseURL: false)
        components?.queryItems = [
            URLQueryItem(name: "lat", value: String(latitude)),
            URLQueryItem(name: "lon", value: String(longitude)),
            URLQueryItem(name: "radius_m", value: String(radiusMeters)),
        ]
        guard let url = components?.url else { return [] }

        let (data, response) = try await urlSession.data(for: authorizedRequest(url: url))
        try validate(response)
        return try JSONDecoder().decode(NearbyFieldsResponse.self, from: data).detectedFields()
    }

    /// Per-player aggregate stats for a team.
    public func teamStats(code: String) async throws -> TeamStatsResponse {
        let url = baseURL.appendingPathComponent("teams/\(code)/stats")
        let (data, response) = try await urlSession.data(for: authorizedRequest(url: url))
        try validate(response)
        return try JSONDecoder().decode(TeamStatsResponse.self, from: data)
    }

    private func authorizedRequest(url: URL) -> URLRequest {
        var request = URLRequest(url: url)
        request.setValue("APIKey \(apiKey)", forHTTPHeaderField: "Authorization")
        return request
    }

    private func validate(_ response: URLResponse) throws {
        guard let httpResponse = response as? HTTPURLResponse else { return }
        guard (200..<300).contains(httpResponse.statusCode) else {
            throw BackendError.httpFailure(statusCode: httpResponse.statusCode)
        }
    }
}
