"""Formation detection (V2 §3).

``GET /teams/<code>/formation?window_days=30`` looks at every match a team has
recorded within the window, reduces each player (device) to the mean of its
per-match average pitch position (``stats.mean_x`` / ``stats.mean_y``), and fits
the resulting point cloud to the closest of several named formation templates.

Coordinate convention (normalized 0–1):

* ``x``: 0 = the team's own goal line, 1 = the opponent's goal line.
* ``y``: 0 = the left touchline, 1 = the right touchline.

Because a team plays toward either goal depending on the half, the raw point
cloud may be mirrored end-for-end and side-for-side relative to a template. We
recover the orientation per player: each player's point is compared against both
``(x, y)`` and its 180° rotation ``(1 − x, 1 − y)`` and the orientation nearer
the player's currently assigned template slot is kept, with the assignment
re-run between refinement passes.

Players are matched to template slots with a pure-Python Hungarian algorithm
(the classic O(n^3) potentials / augmenting-path formulation) minimizing the
squared-Euclidean assignment cost. The winning template is the one with the
lowest MEAN EUCLIDEAN distance between real players and their assigned slots.
"""

from datetime import datetime, timedelta

from flask import Blueprint, jsonify, request

from match_tracks import memberships, privacy
from match_tracks.auth import auth
from match_tracks.models import Match, Player, Team
from match_tracks.rate_limit import rate_limited

formation_blueprint = Blueprint('formation', __name__)

DEFAULT_WINDOW_DAYS = 30
MINIMUM_PLAYERS = 5
TEMPLATE_SLOT_COUNT = 11

# Confidence falls to 0 once the mean assignment cost reaches this many
# normalized field-lengths; a perfect fit (cost 0) scores 1.0.
CONFIDENCE_COST_SCALE = 0.35


# Formation templates.
#
# Each template is a list of exactly 11 slots, one goalkeeper plus ten outfield
# players, expressed in the normalized coordinate system documented above
# (x: own goal 0 → opponent 1; y: left 0 → right 1). Outfield lines sit at
# roughly x = 0.22 (defenders), x = 0.5 (midfield) and x = 0.82 (forwards); the
# goalkeeper sits deep at x = 0.06. Within a line the players are spread evenly
# across the width using y = (index + 0.5) / line_size, so a line of N players
# is centered on the pitch.
FORMATION_TEMPLATES = {
    # 4-4-2: flat back four, flat midfield four, two strikers.
    #   line     | x    | y positions            | roles (left → right)
    #   ---------|------|------------------------|-------------------------------
    #   keeper   | 0.06 | 0.50                   | goalkeeper
    #   defense  | 0.22 | 0.125 0.375 0.625 0.875| left/left-center/right-center/right back
    #   midfield | 0.50 | 0.125 0.375 0.625 0.875| left/left-center/right-center/right midfielder
    #   attack   | 0.82 | 0.25 0.75              | left/right forward
    '4-4-2': [
        {'x': 0.06, 'y': 0.50, 'role': 'goalkeeper'},
        {'x': 0.22, 'y': 0.125, 'role': 'left back'},
        {'x': 0.22, 'y': 0.375, 'role': 'left center back'},
        {'x': 0.22, 'y': 0.625, 'role': 'right center back'},
        {'x': 0.22, 'y': 0.875, 'role': 'right back'},
        {'x': 0.50, 'y': 0.125, 'role': 'left midfielder'},
        {'x': 0.50, 'y': 0.375, 'role': 'left center midfielder'},
        {'x': 0.50, 'y': 0.625, 'role': 'right center midfielder'},
        {'x': 0.50, 'y': 0.875, 'role': 'right midfielder'},
        {'x': 0.82, 'y': 0.25, 'role': 'left forward'},
        {'x': 0.82, 'y': 0.75, 'role': 'right forward'},
    ],
    # 4-3-3: back four, midfield three, front three.
    #   defense  | 0.22 | 0.125 0.375 0.625 0.875
    #   midfield | 0.50 | 0.167 0.500 0.833
    #   attack   | 0.82 | 0.167 0.500 0.833
    '4-3-3': [
        {'x': 0.06, 'y': 0.50, 'role': 'goalkeeper'},
        {'x': 0.22, 'y': 0.125, 'role': 'left back'},
        {'x': 0.22, 'y': 0.375, 'role': 'left center back'},
        {'x': 0.22, 'y': 0.625, 'role': 'right center back'},
        {'x': 0.22, 'y': 0.875, 'role': 'right back'},
        {'x': 0.50, 'y': 0.167, 'role': 'left midfielder'},
        {'x': 0.50, 'y': 0.500, 'role': 'center midfielder'},
        {'x': 0.50, 'y': 0.833, 'role': 'right midfielder'},
        {'x': 0.82, 'y': 0.167, 'role': 'left winger'},
        {'x': 0.82, 'y': 0.500, 'role': 'center forward'},
        {'x': 0.82, 'y': 0.833, 'role': 'right winger'},
    ],
    # 3-5-2: back three, packed midfield five, two strikers.
    #   defense  | 0.22 | 0.167 0.500 0.833
    #   midfield | 0.50 | 0.1 0.3 0.5 0.7 0.9
    #   attack   | 0.82 | 0.25 0.75
    '3-5-2': [
        {'x': 0.06, 'y': 0.50, 'role': 'goalkeeper'},
        {'x': 0.22, 'y': 0.167, 'role': 'left center back'},
        {'x': 0.22, 'y': 0.500, 'role': 'center back'},
        {'x': 0.22, 'y': 0.833, 'role': 'right center back'},
        {'x': 0.50, 'y': 0.10, 'role': 'left wing back'},
        {'x': 0.50, 'y': 0.30, 'role': 'left center midfielder'},
        {'x': 0.50, 'y': 0.50, 'role': 'center midfielder'},
        {'x': 0.50, 'y': 0.70, 'role': 'right center midfielder'},
        {'x': 0.50, 'y': 0.90, 'role': 'right wing back'},
        {'x': 0.82, 'y': 0.25, 'role': 'left forward'},
        {'x': 0.82, 'y': 0.75, 'role': 'right forward'},
    ],
    # 4-2-3-1: back four, double pivot, attacking three, lone striker.
    #   defense   | 0.22 | 0.125 0.375 0.625 0.875
    #   pivot     | 0.44 | 0.30 0.70
    #   attack mid| 0.62 | 0.167 0.500 0.833
    #   striker   | 0.82 | 0.50
    '4-2-3-1': [
        {'x': 0.06, 'y': 0.50, 'role': 'goalkeeper'},
        {'x': 0.22, 'y': 0.125, 'role': 'left back'},
        {'x': 0.22, 'y': 0.375, 'role': 'left center back'},
        {'x': 0.22, 'y': 0.625, 'role': 'right center back'},
        {'x': 0.22, 'y': 0.875, 'role': 'right back'},
        {'x': 0.44, 'y': 0.30, 'role': 'left defensive midfielder'},
        {'x': 0.44, 'y': 0.70, 'role': 'right defensive midfielder'},
        {'x': 0.62, 'y': 0.167, 'role': 'left attacking midfielder'},
        {'x': 0.62, 'y': 0.500, 'role': 'center attacking midfielder'},
        {'x': 0.62, 'y': 0.833, 'role': 'right attacking midfielder'},
        {'x': 0.82, 'y': 0.50, 'role': 'center forward'},
    ],
    # 3-4-3: back three, midfield four, front three.
    #   defense  | 0.22 | 0.167 0.500 0.833
    #   midfield | 0.50 | 0.125 0.375 0.625 0.875
    #   attack   | 0.82 | 0.167 0.500 0.833
    '3-4-3': [
        {'x': 0.06, 'y': 0.50, 'role': 'goalkeeper'},
        {'x': 0.22, 'y': 0.167, 'role': 'left center back'},
        {'x': 0.22, 'y': 0.500, 'role': 'center back'},
        {'x': 0.22, 'y': 0.833, 'role': 'right center back'},
        {'x': 0.50, 'y': 0.125, 'role': 'left midfielder'},
        {'x': 0.50, 'y': 0.375, 'role': 'left center midfielder'},
        {'x': 0.50, 'y': 0.625, 'role': 'right center midfielder'},
        {'x': 0.50, 'y': 0.875, 'role': 'right midfielder'},
        {'x': 0.82, 'y': 0.167, 'role': 'left winger'},
        {'x': 0.82, 'y': 0.500, 'role': 'center forward'},
        {'x': 0.82, 'y': 0.833, 'role': 'right winger'},
    ],
}


def _squared_distance(first_point, second_point):
    """Squared Euclidean distance between two (x, y) points."""
    delta_x = first_point[0] - second_point[0]
    delta_y = first_point[1] - second_point[1]
    return delta_x * delta_x + delta_y * delta_y


def solve_assignment(cost_matrix):
    """Minimum-cost assignment via the classic O(n^3) Hungarian algorithm.

    ``cost_matrix`` must be square (n × n). Returns a list ``assignment`` where
    ``assignment[row] = column`` is the column matched to each row in the
    optimal (minimum total cost) one-to-one assignment.

    This is the standard potentials / augmenting-path formulation using 1-based
    internal indexing with a phantom column 0.
    """
    size = len(cost_matrix)
    if size == 0:
        return []
    infinity = float('inf')

    row_potential = [0.0] * (size + 1)
    column_potential = [0.0] * (size + 1)
    # column_match[column] = row currently matched to that column (0 = none).
    column_match = [0] * (size + 1)
    # previous_column[column] = column we arrived from along the augmenting path.
    previous_column = [0] * (size + 1)

    for row in range(1, size + 1):
        column_match[0] = row
        current_column = 0
        minimum_slack = [infinity] * (size + 1)
        column_used = [False] * (size + 1)

        while True:
            column_used[current_column] = True
            matched_row = column_match[current_column]
            slack_delta = infinity
            next_column = -1

            for column in range(1, size + 1):
                if column_used[column]:
                    continue
                reduced_cost = (cost_matrix[matched_row - 1][column - 1]
                                - row_potential[matched_row]
                                - column_potential[column])
                if reduced_cost < minimum_slack[column]:
                    minimum_slack[column] = reduced_cost
                    previous_column[column] = current_column
                if minimum_slack[column] < slack_delta:
                    slack_delta = minimum_slack[column]
                    next_column = column

            for column in range(size + 1):
                if column_used[column]:
                    row_potential[column_match[column]] += slack_delta
                    column_potential[column] -= slack_delta
                else:
                    minimum_slack[column] -= slack_delta

            current_column = next_column
            if column_match[current_column] == 0:
                break

        # Walk the augmenting path back, flipping matches.
        while current_column:
            source_column = previous_column[current_column]
            column_match[current_column] = column_match[source_column]
            current_column = source_column

    assignment = [0] * size
    for column in range(1, size + 1):
        matched_row = column_match[column]
        if matched_row != 0:
            assignment[matched_row - 1] = column - 1
    return assignment


def _assign_players_to_slots(player_points, template):
    """Assign each player to a template slot, minimizing squared-distance cost.

    Returns a list ``slot_index_for_player`` (one entry per player) naming the
    template slot each real player is matched to. When there are fewer players
    than slots the cost matrix is padded with dummy zero-cost rows so the
    Hungarian solver stays square; the dummies absorb the leftover slots. When
    there are more players than slots the matrix is padded with dummy zero-cost
    slots, and any player landing on one is folded onto its geometrically
    nearest real slot.
    """
    player_count = len(player_points)
    slot_count = len(template)
    square_size = max(player_count, slot_count)

    cost_matrix = [[0.0] * square_size for _ in range(square_size)]
    for player_index in range(player_count):
        for slot_index in range(slot_count):
            cost_matrix[player_index][slot_index] = _squared_distance(
                player_points[player_index],
                (template[slot_index]['x'], template[slot_index]['y']),
            )
        # Columns >= slot_count are dummy slots: leave their cost at 0.

    assignment = solve_assignment(cost_matrix)

    slot_index_for_player = []
    for player_index in range(player_count):
        assigned_column = assignment[player_index]
        if assigned_column < slot_count:
            slot_index_for_player.append(assigned_column)
        else:
            slot_index_for_player.append(
                _nearest_slot_index(player_points[player_index], template))
    return slot_index_for_player


def _nearest_slot_index(point, template):
    """Index of the template slot geometrically closest to ``point``."""
    best_index = 0
    best_distance = None
    for slot_index, slot in enumerate(template):
        distance = _squared_distance(point, (slot['x'], slot['y']))
        if best_distance is None or distance < best_distance:
            best_distance = distance
            best_index = slot_index
    return best_index


def _align_and_assign(raw_points, template):
    """Mirror-align players to a template and return (aligned_points, assignment).

    Starts from the raw player means, runs an initial assignment, then refines
    twice: each pass re-orients every player to whichever of ``(x, y)`` or
    ``(1 − x, 1 − y)`` sits closer to that player's currently assigned slot, then
    re-runs the assignment on the re-oriented points.
    """
    aligned_points = list(raw_points)
    assignment = _assign_players_to_slots(aligned_points, template)

    for _ in range(2):
        for player_index, raw_point in enumerate(raw_points):
            assigned_slot = template[assignment[player_index]]
            slot_point = (assigned_slot['x'], assigned_slot['y'])

            normal_orientation = (raw_point[0], raw_point[1])
            mirrored_orientation = (1.0 - raw_point[0], 1.0 - raw_point[1])

            if (_squared_distance(mirrored_orientation, slot_point)
                    < _squared_distance(normal_orientation, slot_point)):
                aligned_points[player_index] = mirrored_orientation
            else:
                aligned_points[player_index] = normal_orientation

        assignment = _assign_players_to_slots(aligned_points, template)

    return aligned_points, assignment


def _score_template(raw_points, template):
    """Fit one template; return (mean_euclidean_cost, aligned_points, assignment)."""
    aligned_points, assignment = _align_and_assign(raw_points, template)

    total_distance = 0.0
    for player_index, point in enumerate(aligned_points):
        assigned_slot = template[assignment[player_index]]
        total_distance += _squared_distance(
            point, (assigned_slot['x'], assigned_slot['y'])) ** 0.5

    mean_cost = total_distance / len(aligned_points) if aligned_points else 0.0
    return mean_cost, aligned_points, assignment


def _collect_player_means(team_code, window_days):
    """Mean pitch position per device for a team's in-window matches.

    Returns a dict ``device_id -> (mean_x, mean_y)`` averaging each device's
    per-match ``stats.mean_x`` / ``stats.mean_y`` across every qualifying match.
    Matches lacking numeric mean coordinates are skipped.
    """
    cutoff = datetime.utcnow() - timedelta(days=window_days)
    accumulator = {}  # device_id -> [sum_x, sum_y, count]

    for match in Match.objects(team_code=team_code, recorded_at__gte=cutoff):
        stats = match.stats or {}
        mean_x = stats.get('mean_x')
        mean_y = stats.get('mean_y')
        if not _is_number(mean_x) or not _is_number(mean_y):
            continue
        entry = accumulator.setdefault(match.device_id, [0.0, 0.0, 0])
        entry[0] += float(mean_x)
        entry[1] += float(mean_y)
        entry[2] += 1

    return {
        device_id: (sum_x / count, sum_y / count)
        for device_id, (sum_x, sum_y, count) in accumulator.items()
    }


def _is_number(value):
    """True for real numeric values (bools, which are ints in Python, excluded)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


@formation_blueprint.route('/teams/<code>/formation', methods=['GET'])
@auth.login_required
@rate_limited('read')
def get_formation(code):
    denial = memberships.require_member(code)
    if denial:
        return denial

    try:
        window_days = int(request.args.get('window_days', DEFAULT_WINDOW_DAYS))
    except (TypeError, ValueError):
        window_days = DEFAULT_WINDOW_DAYS
    if window_days <= 0:
        window_days = DEFAULT_WINDOW_DAYS

    device_means = _collect_player_means(code, window_days)

    # Minors privacy: players on consent-gated teams stay out of the formation
    # (name AND position) until a guardian has acknowledged consent.
    team = Team.objects(code=code).first()
    if team is not None and team.requires_consent:
        device_means = {
            device_id: mean_point
            for device_id, mean_point in device_means.items()
            if not privacy.consent_blocked(
                Player.objects(device_id=device_id).first(), team)
        }

    if len(device_means) < MINIMUM_PLAYERS:
        return jsonify({'reason': 'insufficient_data'}), 404

    # Deterministic ordering keeps assignments and output stable across runs.
    ordered_device_ids = sorted(device_means)
    raw_points = [device_means[device_id] for device_id in ordered_device_ids]

    best_name = None
    best_mean_cost = None
    best_aligned_points = None
    best_assignment = None
    best_template = None

    for template_name, template in FORMATION_TEMPLATES.items():
        mean_cost, aligned_points, assignment = _score_template(raw_points, template)
        if best_mean_cost is None or mean_cost < best_mean_cost:
            best_name = template_name
            best_mean_cost = mean_cost
            best_aligned_points = aligned_points
            best_assignment = assignment
            best_template = template

    confidence = round(max(0.0, 1.0 - best_mean_cost / CONFIDENCE_COST_SCALE), 2)

    slots = []
    for player_index, device_id in enumerate(ordered_device_ids):
        player = Player.objects(device_id=device_id).first()
        # No fabricated fallback: leaking a device-id fragment would bypass
        # the display-name privacy rules; clients render anonymous slots.
        player_name = privacy.player_display_name(player)
        aligned_x, aligned_y = best_aligned_points[player_index]
        assigned_slot = best_template[best_assignment[player_index]]
        slots.append({
            'player_name': player_name,
            'x': round(aligned_x, 3),
            'y': round(aligned_y, 3),
            'role': assigned_slot['role'],
        })

    return jsonify({
        'name': best_name,
        'confidence': confidence,
        'slots': slots,
    })
