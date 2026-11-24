"""StoreKit 2 team-features entitlements (V2 §7).

Receipts arrive as StoreKit 2 signed transactions (JWS). Verification is
pluggable via ``app.config['ENTITLEMENT_VERIFIER']`` (tests inject fakes);
the default verifier decodes the JWS payload and, when the ``cryptography``
package is available, verifies the ES256 signature against the x5c leaf
certificate. Production deployments should additionally pin Apple's root
certificates (App Store Server API validation is the gold standard).
"""

import base64
import json
from datetime import timezone

from flask import Blueprint, current_app, jsonify, request

from match_tracks.auth import auth, current_principal, effective_device_id
from match_tracks.memberships import actor_denial
from match_tracks.models import Entitlement
from match_tracks.timeutils import utcfromtimestamp, utcnow

entitlements_blueprint = Blueprint('entitlements', __name__)

TIMESTAMP_FORMAT = '%Y-%m-%dT%H:%M:%SZ'

# Only OUR products grant features: a valid Apple-signed receipt for another
# app's ".team." product must not unlock anything here.
DEFAULT_TEAM_PRODUCT_IDS = frozenset({'com.nicemohawk.MatchTracker.team.monthly'})
DEFAULT_BUNDLE_ID = 'com.nicemohawk.MatchTracker'


def allowed_team_product_ids():
    """The exact product ids that grant the team entitlement."""
    configured = current_app.config.get('ENTITLEMENT_ALLOWED_TEAM_PRODUCT_IDS')
    return set(configured) if configured else set(DEFAULT_TEAM_PRODUCT_IDS)


def expected_bundle_id():
    return current_app.config.get('ENTITLEMENT_BUNDLE_ID', DEFAULT_BUNDLE_ID)


class VerifierUnavailable(Exception):
    """No way to verify signatures in this deployment."""


class InvalidReceipt(Exception):
    """The JWS is structurally invalid or fails verification."""


def _base64url_decode(segment):
    padding = '=' * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def default_jws_verifier(jws_string):
    """Decode and verify a StoreKit 2 signed transaction.

    Returns ``{'product_id', 'expires_at' (datetime), 'environment'}``.

    Security model: a JWS is only trusted when its x5c certificate chain
    terminates at a PINNED root certificate loaded from the PEM file(s) at
    ``ENTITLEMENT_APPLE_ROOT_CERTS_PATH`` (deployments bundle Apple's root
    CAs there). Verifying against the leaf certificate alone would trust
    whatever certificate the sender minted, so without pinned roots (or the
    ``cryptography`` package) strict mode refuses with VerifierUnavailable
    rather than pretending to verify. ``ENTITLEMENT_ALLOW_UNVERIFIED=True``
    (development only) skips signature checking entirely.
    """
    try:
        header_segment, payload_segment, signature_segment = jws_string.split('.')
        header = json.loads(_base64url_decode(header_segment))
        payload = json.loads(_base64url_decode(payload_segment))
        product_id = payload['productId']
        expires_ms = payload['expiresDate']
        environment = payload.get('environment')
    except (ValueError, KeyError, TypeError) as parse_error:
        raise InvalidReceipt(str(parse_error))

    # Bind the transaction to THIS app: an Apple-signed receipt for another
    # bundle must not be replayable here. (StoreKit 2 transactions carry
    # bundleId; tolerate its absence only for injected test verifiers.)
    payload_bundle_id = payload.get('bundleId')
    if payload_bundle_id is not None and payload_bundle_id != expected_bundle_id():
        raise InvalidReceipt(
            f'receipt is for bundle {payload_bundle_id!r}, not this app')

    allow_unverified = current_app.config.get('ENTITLEMENT_ALLOW_UNVERIFIED', False)

    if not allow_unverified:
        try:
            from cryptography import x509  # noqa: F401
        except ImportError:
            raise VerifierUnavailable(
                'cryptography package unavailable and unverified receipts are disabled')

        trusted_roots = _load_pinned_root_certificates()
        if not trusted_roots:
            raise VerifierUnavailable(
                'no pinned Apple root certificates configured '
                '(set ENTITLEMENT_APPLE_ROOT_CERTS_PATH) and unverified receipts '
                'are disabled')

        _verify_jws_signature(header, header_segment, payload_segment,
                              signature_segment, trusted_roots)

    return {
        'product_id': product_id,
        'expires_at': utcfromtimestamp(expires_ms / 1000.0),
        'environment': environment,
    }


def _load_pinned_root_certificates():
    """Load trusted root certificates from ENTITLEMENT_APPLE_ROOT_CERTS_PATH."""
    import os

    from cryptography import x509

    certs_path = current_app.config.get('ENTITLEMENT_APPLE_ROOT_CERTS_PATH')
    if not certs_path or not os.path.exists(certs_path):
        return []

    pem_paths = []
    if os.path.isdir(certs_path):
        pem_paths = [os.path.join(certs_path, name)
                     for name in sorted(os.listdir(certs_path))
                     if name.endswith(('.pem', '.crt', '.cer'))]
    else:
        pem_paths = [certs_path]

    roots = []
    for pem_path in pem_paths:
        with open(pem_path, 'rb') as pem_file:
            data = pem_file.read()
        try:
            roots.extend(x509.load_pem_x509_certificates(data))
        except ValueError:
            try:
                roots.append(x509.load_der_x509_certificate(data))
            except ValueError:
                continue
    return roots


def _verify_jws_signature(header, header_segment, payload_segment,
                          signature_segment, trusted_roots):
    """Verify the x5c chain to a pinned root, then the ES256 JWS signature.

    Beyond raw signatures, every certificate must be inside its validity
    window and every issuer must be a CA (basicConstraints), and the JWS
    ``alg`` is pinned to ES256 — StoreKit 2's only algorithm — to prevent
    algorithm-confusion tricks.
    """
    import base64 as base64_module

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

    if header.get('alg') != 'ES256':
        raise InvalidReceipt(f"unsupported JWS alg {header.get('alg')!r}")

    try:
        chain = [x509.load_der_x509_certificate(base64_module.b64decode(entry))
                 for entry in header['x5c']]
        if not chain:
            raise InvalidReceipt('empty x5c chain')
    except InvalidReceipt:
        raise
    except Exception as parse_error:
        raise InvalidReceipt(f'malformed x5c chain: {parse_error}')

    for certificate in chain:
        _require_certificate_currently_valid(certificate)

    try:
        # Each certificate must be signed by the next one up (which must be a
        # CA); the last link must be signed by (or be) a pinned root.
        for child, issuer in zip(chain, chain[1:]):
            _require_certificate_authority(issuer)
            _verify_certificate_signature(child, issuer)
        top_of_chain = chain[-1]
        trusted = False
        for root in trusted_roots:
            if top_of_chain.tbs_certificate_bytes == root.tbs_certificate_bytes:
                trusted = True
                break
            try:
                _require_certificate_authority(root)
                _verify_certificate_signature(top_of_chain, root)
                trusted = True
                break
            except Exception:
                continue
        if not trusted:
            raise InvalidReceipt('x5c chain does not terminate at a pinned root')

        leaf_certificate = chain[0]
        signing_input = f'{header_segment}.{payload_segment}'.encode()
        raw_signature = _base64url_decode(signature_segment)
        half = len(raw_signature) // 2
        der_signature = encode_dss_signature(
            int.from_bytes(raw_signature[:half], 'big'),
            int.from_bytes(raw_signature[half:], 'big'))
        leaf_certificate.public_key().verify(
            der_signature, signing_input, ec.ECDSA(hashes.SHA256()))
    except InvalidReceipt:
        raise
    except Exception as verification_error:
        raise InvalidReceipt(f'signature verification failed: {verification_error}')


def _require_certificate_currently_valid(certificate):
    """Raise InvalidReceipt when `certificate` is outside its validity window."""
    now = utcnow()  # aware UTC
    try:  # cryptography >= 42 exposes aware-UTC properties
        not_before = certificate.not_valid_before_utc
        not_after = certificate.not_valid_after_utc
    except AttributeError:  # older cryptography returns naive UTC
        not_before = certificate.not_valid_before.replace(tzinfo=timezone.utc)
        not_after = certificate.not_valid_after.replace(tzinfo=timezone.utc)
    if now < not_before or now > not_after:
        raise InvalidReceipt('certificate in x5c chain is expired or not yet valid')


def _require_certificate_authority(certificate):
    """Raise InvalidReceipt unless `certificate` is marked as a CA."""
    from cryptography import x509

    try:
        basic_constraints = certificate.extensions.get_extension_for_class(
            x509.BasicConstraints).value
    except Exception:
        raise InvalidReceipt('issuer certificate lacks basicConstraints')
    if not basic_constraints.ca:
        raise InvalidReceipt('issuer certificate is not a CA')


def _verify_certificate_signature(certificate, issuer):
    """Raise unless `certificate` was signed by `issuer`'s key (EC or RSA)."""
    from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

    issuer_public_key = issuer.public_key()
    if isinstance(issuer_public_key, rsa.RSAPublicKey):
        issuer_public_key.verify(
            certificate.signature, certificate.tbs_certificate_bytes,
            padding.PKCS1v15(), certificate.signature_hash_algorithm)
    elif isinstance(issuer_public_key, ec.EllipticCurvePublicKey):
        issuer_public_key.verify(
            certificate.signature, certificate.tbs_certificate_bytes,
            ec.ECDSA(certificate.signature_hash_algorithm))
    else:
        raise InvalidReceipt('unsupported issuer key type in x5c chain')


def has_active_team_entitlement(device_id):
    """True when the device holds an unexpired subscription to one of OUR
    team products (exact product-id match — never a substring test)."""
    if not device_id:
        return False
    now = utcnow()
    team_products = allowed_team_product_ids()
    for entitlement in Entitlement.objects(device_id=device_id.lower()):
        if entitlement.product_id in team_products:
            if entitlement.expires_at and entitlement.expires_at > now:
                return True
    return False


def require_team_entitlement():
    """None when the caller may use team write features; else (response, 402)."""
    if not current_app.config.get('ENTITLEMENTS_ENFORCED', True):
        return None
    principal = current_principal()
    if principal and principal.get('admin'):
        return None
    if has_active_team_entitlement(effective_device_id()):
        return None
    return jsonify({'reason': 'entitlement_required'}), 402


def _team_entitlement_json(device_id):
    now = utcnow()
    team_products = allowed_team_product_ids()
    newest_expiry = None
    for entitlement in Entitlement.objects(device_id=device_id):
        if entitlement.product_id in team_products:
            if newest_expiry is None or (entitlement.expires_at
                                         and entitlement.expires_at > newest_expiry):
                newest_expiry = entitlement.expires_at
    return {
        'active': bool(newest_expiry and newest_expiry > now),
        'expires_at': newest_expiry.astimezone(timezone.utc).strftime(TIMESTAMP_FORMAT) if newest_expiry else None,
    }


# SUBMIT a StoreKit receipt
@entitlements_blueprint.route('/devices/<identifier>/receipt', methods=['POST'])
@auth.login_required
def submit_receipt(identifier):
    denial = actor_denial(identifier)
    if denial:
        return denial

    json_data = request.get_json(silent=True) or {}
    jws_string = json_data.get('jws')
    if not jws_string:
        return jsonify({'reason': 'jws_required'}), 400

    verifier = current_app.config.get('ENTITLEMENT_VERIFIER') or default_jws_verifier
    try:
        verified = verifier(jws_string)
    except VerifierUnavailable:
        return jsonify({'reason': 'verifier_unavailable'}), 503
    except Exception:
        return jsonify({'reason': 'invalid_receipt'}), 400

    # Store only products this app sells; a foreign product id — even inside a
    # legitimately signed receipt — grants nothing and is rejected outright.
    if verified['product_id'] not in allowed_team_product_ids():
        return jsonify({'reason': 'invalid_receipt'}), 400

    device_id = str(identifier).lower()
    Entitlement.objects(device_id=device_id,
                        product_id=verified['product_id']).update_one(
        set__expires_at=verified['expires_at'],
        set__environment=verified.get('environment'),
        upsert=True)

    return jsonify({'entitlements': {'team': _team_entitlement_json(device_id)}})


# READ entitlements
@entitlements_blueprint.route('/devices/<identifier>/entitlements', methods=['GET'])
@auth.login_required
def get_entitlements(identifier):
    denial = actor_denial(identifier)
    if denial:
        return denial

    return jsonify({'team': _team_entitlement_json(str(identifier).lower())})
