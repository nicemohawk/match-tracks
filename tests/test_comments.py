"""Acceptance tests for V2 §2 match comments (match_tracks/comments.py)."""

from datetime import datetime, timedelta, timezone

from match_tracks.models import Match, MatchComment, Player


def _make_match(uuid, team_code=None):
    Match(uuid=uuid.lower(), device_id='dev-owner', team_code=team_code,
          recorded_at=datetime.now(timezone.utc)).save()


def test_create_then_get_shows_comment(app, client, admin_headers):
    _make_match('match-create')
    created = client.post('/matches/match-create/comments', headers=admin_headers,
                          json={'id': 'comment-1', 'body': 'great goal', 'author': 'Coach'})
    assert created.status_code == 201
    body = created.get_json()['comment']
    assert body['id'] == 'comment-1'
    assert body['body'] == 'great goal'

    listing = client.get('/matches/match-create/comments', headers=admin_headers)
    assert listing.status_code == 200
    comments = listing.get_json()['comments']
    assert len(comments) == 1
    assert comments[0]['id'] == 'comment-1'


def test_replay_same_id_is_idempotent(app, client, admin_headers):
    _make_match('match-replay')
    first = client.post('/matches/match-replay/comments', headers=admin_headers,
                        json={'id': 'dup', 'body': 'original body'})
    assert first.status_code == 201

    replay = client.post('/matches/match-replay/comments', headers=admin_headers,
                         json={'id': 'dup', 'body': 'a different body'})
    assert replay.status_code == 200
    assert replay.get_json()['comment']['body'] == 'original body'

    stored = MatchComment.objects(match_uuid='match-replay')
    assert stored.count() == 1
    assert stored.first().body == 'original body'


def test_ordering_oldest_first_and_after_pagination(app, client, admin_headers):
    _make_match('match-order')
    base = datetime.now(timezone.utc)
    MatchComment(uuid='c1', match_uuid='match-order', body='first',
                 posted_at=base).save()
    MatchComment(uuid='c2', match_uuid='match-order', body='second',
                 posted_at=base + timedelta(seconds=10)).save()
    MatchComment(uuid='c3', match_uuid='match-order', body='third',
                 posted_at=base + timedelta(seconds=20)).save()

    listing = client.get('/matches/match-order/comments', headers=admin_headers)
    ids = [comment['id'] for comment in listing.get_json()['comments']]
    assert ids == ['c1', 'c2', 'c3']

    after = client.get('/matches/match-order/comments?after=c1', headers=admin_headers)
    after_ids = [comment['id'] for comment in after.get_json()['comments']]
    assert after_ids == ['c2', 'c3']


def test_limit_is_respected(app, client, admin_headers):
    _make_match('match-limit')
    base = datetime.now(timezone.utc)
    for index in range(5):
        MatchComment(uuid=f'lc{index}', match_uuid='match-limit', body=str(index),
                     posted_at=base + timedelta(seconds=index)).save()

    listing = client.get('/matches/match-limit/comments?limit=2', headers=admin_headers)
    ids = [comment['id'] for comment in listing.get_json()['comments']]
    assert ids == ['lc0', 'lc1']


def test_unknown_after_id_is_400(app, client, admin_headers):
    _make_match('match-badafter')
    listing = client.get('/matches/match-badafter/comments?after=nope', headers=admin_headers)
    assert listing.status_code == 400


def test_non_member_denied_on_get_and_post(app, client, device_key):
    _make_match('match-private', team_code='CMT-PRIVATE')
    device_headers = device_key('key-cmt-out', 'dev-cmt-out')

    get_response = client.get('/matches/match-private/comments', headers=device_headers)
    assert get_response.status_code == 403
    assert get_response.get_json() == {'reason': 'not_a_member'}

    post_response = client.post('/matches/match-private/comments', headers=device_headers,
                                json={'id': 'x', 'body': 'hi'})
    assert post_response.status_code == 403
    assert post_response.get_json() == {'reason': 'not_a_member'}


def test_unknown_match_is_404(app, client, admin_headers):
    get_response = client.get('/matches/does-not-exist/comments', headers=admin_headers)
    assert get_response.status_code == 404
    assert get_response.get_json() == {'reason': 'unknown_match'}

    post_response = client.post('/matches/does-not-exist/comments', headers=admin_headers,
                                json={'id': 'x', 'body': 'hi'})
    assert post_response.status_code == 404


def test_body_too_long_is_400(app, client, admin_headers):
    _make_match('match-long')
    response = client.post('/matches/match-long/comments', headers=admin_headers,
                           json={'id': 'long-1', 'body': 'a' * 1001})
    assert response.status_code == 400


def test_missing_id_is_400(app, client, admin_headers):
    _make_match('match-noid')
    response = client.post('/matches/match-noid/comments', headers=admin_headers,
                           json={'body': 'no id here'})
    assert response.status_code == 400


def test_author_resolved_from_player_row_ignoring_client_author(app, client, device_key,
                                                                membership):
    _make_match('match-author', team_code='CMT-AUTHOR')
    Player(device_id='dev-author', name='Ben Lachman', initials_only=True,
           team_code='CMT-AUTHOR').save()
    device_headers = device_key('key-author', 'dev-author')
    membership('dev-author', 'CMT-AUTHOR')

    response = client.post('/matches/match-author/comments', headers=device_headers,
                           json={'id': 'auth-1', 'body': 'nice', 'author': 'Ignored Name'})
    assert response.status_code == 201
    assert response.get_json()['comment']['author'] == 'B. L.'


def test_author_falls_back_to_client_string_without_player(app, client, device_key,
                                                           membership):
    _make_match('match-fallback', team_code='CMT-FALLBACK')
    device_headers = device_key('key-fallback', 'dev-fallback')
    membership('dev-fallback', 'CMT-FALLBACK')

    response = client.post('/matches/match-fallback/comments', headers=device_headers,
                           json={'id': 'fb-1', 'body': 'hi', 'author': 'Guest Fan'})
    assert response.status_code == 201
    assert response.get_json()['comment']['author'] == 'Guest Fan'


def test_tombstoned_comment_renders_deleted_author(app, client, admin_headers):
    _make_match('match-tomb')
    MatchComment(uuid='tomb-1', match_uuid='match-tomb', body='was here',
                 author_name='[deleted]', author_tombstoned=True,
                 posted_at=datetime.now(timezone.utc)).save()

    listing = client.get('/matches/match-tomb/comments', headers=admin_headers)
    comments = listing.get_json()['comments']
    assert len(comments) == 1
    assert comments[0]['author'] == '[deleted]'


def test_soft_deleted_comments_are_excluded(app, client, admin_headers):
    _make_match('match-del')
    MatchComment(uuid='visible', match_uuid='match-del', body='keep',
                 posted_at=datetime.now(timezone.utc)).save()
    MatchComment(uuid='hidden', match_uuid='match-del', body='gone', deleted=True,
                 posted_at=datetime.now(timezone.utc)).save()

    listing = client.get('/matches/match-del/comments', headers=admin_headers)
    ids = [comment['id'] for comment in listing.get_json()['comments']]
    assert ids == ['visible']


def test_entitlement_gate_on_post_only(app, client, device_key, membership,
                                       grant_entitlement):
    app.config['ENTITLEMENTS_ENFORCED'] = True
    _make_match('match-entitle', team_code='CMT-ENTITLE')
    device_headers = device_key('key-cmt-entitle', 'dev-cmt-entitle')
    membership('dev-cmt-entitle', 'CMT-ENTITLE')

    # Reads never require an entitlement.
    read_without = client.get('/matches/match-entitle/comments', headers=device_headers)
    assert read_without.status_code == 200

    post_without = client.post('/matches/match-entitle/comments', headers=device_headers,
                               json={'id': 'e-1', 'body': 'hi'})
    assert post_without.status_code == 402
    assert post_without.get_json() == {'reason': 'entitlement_required'}

    grant_entitlement('dev-cmt-entitle', datetime.now(timezone.utc) + timedelta(days=30))
    post_with = client.post('/matches/match-entitle/comments', headers=device_headers,
                            json={'id': 'e-1', 'body': 'hi'})
    assert post_with.status_code == 201
